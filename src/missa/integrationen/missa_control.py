"""Verbindung zu Missa Control (Panel auf ai01): MCP-Gateway unter ``<URL>/mcp``.

Das Panel bündelt die Funktionen aller dort eingerichteten Integrationen an einem
MCP-Endpunkt (Streamable HTTP, zustandslos). Hier steckt ein kleiner eigener Client mit
httpx, der genau das kann, was Missa-Core braucht: ``tools/list`` und ``tools/call`` –
Antworten als JSON oder als Server-Sent Events.

Warum nicht das offizielle MCP-SDK? ``mcp`` 2.x bringt einen eigenen HTTP-Stack
(``httpx2``) und Server-Abhängigkeiten mit, die neben dem vorhandenen ``httpx`` laufen
würden. Für zwei zustandslose JSON-RPC-Aufrufe ist der eigene Client kleiner, und die
Tests können das Panel wie die anderen Gegenstellen mit ``httpx.MockTransport`` nachbauen.

Über Rechte entscheidet das Panel: Funktionen mit „erst nachfragen“ bleiben offen, bis
Louis im Panel entschieden hat (höchstens 5 Minuten). Ablehnung und Fehler kommen als
Ergebnis mit ``isError: true`` und einem fertigen Satz.
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Zeitlimit für ``tools/list`` (Aufrufe nutzen ``MISSA_CONTROL_ZEITLIMIT``).
LISTE_ZEITLIMIT = 15.0
#: Höchstens so viele Seiten ``tools/list`` (Schutz vor einer Endlosschleife).
MAX_SEITEN = 20


class PanelFehler(Exception):
    """Missa Control hat mit einem Fehler geantwortet."""


class PanelNichtErreichbar(PanelFehler):
    """Missa Control ist nicht erreichbar (Verbindung, Zeitlimit, 5xx)."""


class PanelTokenUngueltig(PanelFehler):
    """Missa Control lehnt den Token ab (401)."""


@dataclass(frozen=True)
class PanelFunktion:
    """Eine Funktion aus ``tools/list``."""

    name: str
    beschreibung: str
    schema: dict[str, Any]
    nur_lesen: bool


@dataclass(frozen=True)
class PanelErgebnis:
    """Ergebnis von ``tools/call``: zusammengefügter Text und ob es ein Fehler war."""

    text: str
    fehler: bool


def _sse_daten(text: str) -> list[dict[str, Any]]:
    """Alle JSON-Nachrichten aus einer SSE-Antwort (``data:``-Zeilen, je Ereignis verbunden)."""
    nachrichten: list[dict[str, Any]] = []
    puffer: list[str] = []

    def abschliessen() -> None:
        if not puffer:
            return
        roh = "\n".join(puffer)
        puffer.clear()
        try:
            daten = json.loads(roh)
        except ValueError:
            log.debug("Missa Control: SSE-Daten sind kein JSON: %.200s", roh)
            return
        if isinstance(daten, dict):
            nachrichten.append(daten)

    for zeile in text.splitlines():
        if not zeile.strip():
            abschliessen()
        elif zeile.startswith("data:"):
            puffer.append(zeile.removeprefix("data:").removeprefix(" "))
    abschliessen()
    return nachrichten


def ergebnis_lesen(ergebnis: dict[str, Any], max_zeichen: int) -> PanelErgebnis:
    """Fügt die Text-Inhalte zusammen, lässt anderes mit Hinweis aus und kürzt lange Texte."""
    teile: list[str] = []
    ausgelassen: list[str] = []
    for block in ergebnis.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            teile.append(str(block.get("text", "")))
        elif isinstance(block, dict):
            ausgelassen.append(str(block.get("type") or "Inhalt"))
    text = "\n".join(t for t in teile if t).strip()
    if not text and isinstance(ergebnis.get("structuredContent"), dict):
        text = json.dumps(ergebnis["structuredContent"], ensure_ascii=False)
    if len(text) > max_zeichen:
        text = (
            text[:max_zeichen].rstrip()
            + f"\n[… gekürzt: {len(text) - max_zeichen} weitere Zeichen ausgelassen]"
        )
    if ausgelassen:
        arten = ", ".join(sorted(set(ausgelassen)))
        text = f"{text}\n[Hinweis: {len(ausgelassen)} Inhalt(e) ohne Text ausgelassen ({arten})]"
    fehler = bool(ergebnis.get("isError"))
    return PanelErgebnis(text.strip() or ("Fehler ohne Text." if fehler else "Erledigt."), fehler)


class MissaControl:
    """Spricht mit dem MCP-Gateway von Missa Control."""

    def __init__(
        self,
        url: str,
        token: str = "",
        token_datei: Path | None = None,
        zeitlimit: float = 330.0,
        max_zeichen: int = 6000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._token_datei = token_datei
        self.zeitlimit = zeitlimit
        self.max_zeichen = max_zeichen
        self._nummer = itertools.count(1)
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(zeitlimit, connect=10.0),
            transport=transport,
            # z. B. /mcp → /mcp/ hinter einem Proxy; 307/308 behalten POST und Inhalt.
            follow_redirects=True,
        )

    def token(self) -> str:
        """Token aus ``MISSA_CONTROL_TOKEN`` oder (bei jedem Aufruf neu) aus der Datei."""
        if self._token:
            return self._token
        if self._token_datei is None:
            return ""
        try:
            return self._token_datei.read_text(encoding="utf-8").strip()
        except OSError as fehler:
            log.warning("Missa Control: Token-Datei %s nicht lesbar: %s", self._token_datei, fehler)
            return ""

    def kopfzeilen(self) -> dict[str, str]:
        kopf = {"Accept": "application/json, text/event-stream"}
        if token := self.token():
            kopf["Authorization"] = f"Bearer {token}"
        return kopf

    async def _rpc(
        self, methode: str, parameter: dict[str, Any], zeitlimit: float | None = None
    ) -> dict[str, Any]:
        nummer = next(self._nummer)
        anfrage = {"jsonrpc": "2.0", "id": nummer, "method": methode, "params": parameter}
        try:
            antwort = await self._client.post(
                "/mcp",
                json=anfrage,
                headers=self.kopfzeilen(),
                timeout=httpx.Timeout(zeitlimit, connect=10.0)
                if zeitlimit
                else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as fehler:
            raise PanelNichtErreichbar(
                f"Missa Control hat nicht rechtzeitig geantwortet ({self.url})"
            ) from fehler
        except httpx.HTTPError as fehler:
            raise PanelNichtErreichbar(
                f"Missa Control ist nicht erreichbar ({self.url}): {fehler}"
            ) from fehler
        if antwort.status_code == 401:
            raise PanelTokenUngueltig(
                "Missa Control lehnt den Token ab (401) – im Panel unter „Clients“ einen "
                "gültigen Token anlegen und in MISSA_CONTROL_TOKEN eintragen"
            )
        if antwort.status_code >= 500:
            raise PanelNichtErreichbar(
                f"Missa Control antwortete mit {antwort.status_code}: {antwort.text[:300]}"
            )
        if antwort.status_code >= 400:
            raise PanelFehler(
                f"Missa Control antwortete mit {antwort.status_code}: {antwort.text[:300]}"
            )
        art = antwort.headers.get("content-type", "")
        if "text/event-stream" in art:
            nachrichten = _sse_daten(antwort.text)
        else:
            try:
                daten = antwort.json()
            except ValueError as fehler:
                raise PanelFehler("Missa Control lieferte kein gültiges JSON") from fehler
            nachrichten = daten if isinstance(daten, list) else [daten]
        passend = next(
            (n for n in nachrichten if isinstance(n, dict) and n.get("id") == nummer),
            None,
        )
        if passend is None:
            raise PanelFehler(f"Missa Control lieferte keine Antwort auf {methode}")
        if "error" in passend:
            fehler = passend["error"] or {}
            text = fehler.get("message") if isinstance(fehler, dict) else str(fehler)
            raise PanelFehler(f"Missa Control meldet bei {methode}: {text}")
        ergebnis = passend.get("result")
        if not isinstance(ergebnis, dict):
            raise PanelFehler(f"Missa Control lieferte kein Ergebnis auf {methode}")
        return ergebnis

    async def funktionen(self) -> list[PanelFunktion]:
        """Alle Funktionen, die das Panel gerade für Missa freigibt (``tools/list``)."""
        funktionen: list[PanelFunktion] = []
        cursor: str | None = None
        for _ in range(MAX_SEITEN):
            ergebnis = await self._rpc(
                "tools/list", {"cursor": cursor} if cursor else {}, LISTE_ZEITLIMIT
            )
            for eintrag in ergebnis.get("tools") or []:
                if not isinstance(eintrag, dict) or not eintrag.get("name"):
                    continue
                schema = eintrag.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                hinweise = eintrag.get("annotations") or {}
                funktionen.append(
                    PanelFunktion(
                        name=str(eintrag["name"]),
                        beschreibung=str(eintrag.get("description") or eintrag["name"]),
                        schema=schema,
                        nur_lesen=bool(
                            isinstance(hinweise, dict) and hinweise.get("readOnlyHint") is True
                        ),
                    )
                )
            cursor = ergebnis.get("nextCursor")
            if not cursor:
                break
        return funktionen

    async def aufrufen(self, name: str, argumente: dict[str, Any]) -> PanelErgebnis:
        """Ruft eine Funktion auf (``tools/call``); wartet ggf. auf die Freigabe im Panel."""
        ergebnis = await self._rpc("tools/call", {"name": name, "arguments": argumente})
        return ergebnis_lesen(ergebnis, self.max_zeichen)

    async def schliessen(self) -> None:
        await self._client.aclose()
