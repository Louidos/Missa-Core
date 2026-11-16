"""Client für llama-server (OpenAI-kompatible Schnittstelle)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


#: Feste Fehler-Codes für ``error.code`` (siehe docs/api.md, Abschnitt „Fehler“).
ZEITLIMIT = "zeitlimit"
ANFRAGE_ZU_GROSS = "anfrage_zu_gross"
MODELL_NICHT_ERREICHBAR = "modell_nicht_erreichbar"
UNERWARTETE_ANTWORT = "unerwartete_antwort"
MODELL_FEHLER = "modell_fehler"

#: Woran man erkennt, dass llama-server die Anfrage als zu gross ablehnt
#: (z. B. ``exceed_context_size_error`` oder „the request exceeds the available context size“).
_ZU_GROSS_MERKMALE = (
    "exceed_context_size",
    "exceeds the available context",
    "context size",
    "context length",
    "context window",
    "too many tokens",
    "input is too large",
    "prompt is too long",
)


class LlamaFehler(Exception):
    """llama-server ist nicht erreichbar oder hat mit einem Fehler geantwortet.

    ``code`` ist einer der festen Codes oben, ``original`` der Originaltext von
    llama-server (fürs Log), ``anfrage_id`` setzt der Chat-Dienst.
    """

    def __init__(
        self,
        nachricht: str,
        status: int = 502,
        code: str = MODELL_FEHLER,
        original: str = "",
    ) -> None:
        super().__init__(nachricht)
        self.status = status
        self.code = code
        self.original = original
        self.anfrage_id = ""


def ist_zu_gross(status: int, text: str) -> bool:
    """``True``, wenn llama-server (oder ein Proxy davor) die Anfrage als zu gross ablehnt."""
    if status == 413:
        return True
    if status < 400:
        return False
    klein = text.lower()
    return any(merkmal in klein for merkmal in _ZU_GROSS_MERKMALE)


def fehler_aus_antwort(status: int, text: str) -> LlamaFehler:
    """Macht aus einer Fehlerantwort von llama-server einen verständlichen ``LlamaFehler``."""
    original = f"{status}: {text[:2000]}"
    if ist_zu_gross(status, text):
        return LlamaFehler(
            "Die Anfrage ist zu gross für das Sprachmodell: Verlauf, Werkzeug-Ergebnisse und "
            "Werkzeug-Beschreibungen passen nicht in seinen Kontext. Bitte die Aufgaben einzeln "
            "schicken oder eine neue Sitzung beginnen; dauerhaft hilft ein grösserer Kontext "
            "beim llama-server (Option -c).",
            502,
            ANFRAGE_ZU_GROSS,
            original,
        )
    if status in (502, 503):
        return LlamaFehler(
            f"Das Sprachmodell ist gerade nicht bereit (llama-server antwortete mit {status}, "
            "z. B. weil das Modell noch lädt). Bitte in einer Minute nochmal versuchen.",
            502,
            MODELL_NICHT_ERREICHBAR,
            original,
        )
    return LlamaFehler(
        f"Das Sprachmodell hat die Anfrage mit Fehler {status} abgelehnt. Bitte nochmal "
        "versuchen; bleibt es so, steht der genaue Grund im Log von missa-core "
        "(journalctl -u missa-core).",
        502,
        MODELL_FEHLER,
        original,
    )


def unerwartet(original: str) -> LlamaFehler:
    """llama-server hat geantwortet, aber nicht im erwarteten Format."""
    return LlamaFehler(
        "Das Sprachmodell hat eine Antwort geliefert, mit der Missa nichts anfangen kann. "
        "Bitte nochmal versuchen; bleibt es so, steht der Originaltext im Log von missa-core "
        "(journalctl -u missa-core).",
        502,
        UNERWARTETE_ANTWORT,
        original,
    )


class LlamaClient:
    """Spricht mit llama-server über ``/v1/chat/completions``."""

    def __init__(
        self,
        basis_url: str,
        api_key_datei: Path | None = None,
        timeout: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key_datei = api_key_datei
        self.basis_url = basis_url
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=basis_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    def _kopfzeilen(self) -> dict[str, str]:
        """Liest den API-Key von llama-server bei jedem Aufruf neu (falls vorhanden)."""
        if self._api_key_datei is None:
            return {}
        try:
            schluessel = self._api_key_datei.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {}
        except OSError as fehler:
            log.warning(
                "API-Key für llama-server nicht lesbar (%s): %s", self._api_key_datei, fehler
            )
            return {}
        return {"Authorization": f"Bearer {schluessel}"} if schluessel else {}

    async def chat(self, nutzlast: dict[str, Any]) -> dict[str, Any]:
        """Eine Anfrage ohne Streaming; gibt die JSON-Antwort zurück."""
        try:
            antwort = await self._client.post(
                "/v1/chat/completions",
                json={**nutzlast, "stream": False},
                headers=self._kopfzeilen(),
            )
        except httpx.TimeoutException as fehler:
            raise self._zeitlimit(fehler) from fehler
        except httpx.HTTPError as fehler:
            raise self._nicht_erreichbar(fehler) from fehler
        if antwort.status_code >= 400:
            raise fehler_aus_antwort(antwort.status_code, antwort.text)
        try:
            daten = antwort.json()
        except ValueError as fehler:
            raise unerwartet(f"kein gültiges JSON: {antwort.text[:2000]}") from fehler
        if not isinstance(daten, dict):
            raise unerwartet(f"kein JSON-Objekt: {antwort.text[:2000]}")
        return daten

    async def chat_stream(self, nutzlast: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Eine Anfrage mit Streaming; liefert die einzelnen Chunks als dict."""
        try:
            async with self._client.stream(
                "POST",
                "/v1/chat/completions",
                json={**nutzlast, "stream": True},
                headers=self._kopfzeilen(),
            ) as antwort:
                if antwort.status_code >= 400:
                    text = (await antwort.aread()).decode("utf-8", "replace")
                    raise fehler_aus_antwort(antwort.status_code, text)
                async for zeile in antwort.aiter_lines():
                    zeile = zeile.strip()
                    if not zeile.startswith("data:"):
                        continue
                    daten = zeile.removeprefix("data:").strip()
                    if daten == "[DONE]":
                        break
                    try:
                        yield json.loads(daten)
                    except ValueError:
                        log.warning("Ungültiger Stream-Chunk von llama-server: %.200s", daten)
        except httpx.TimeoutException as fehler:
            raise self._zeitlimit(fehler) from fehler
        except httpx.HTTPError as fehler:
            raise self._nicht_erreichbar(fehler) from fehler

    def _zeitlimit(self, fehler: Exception) -> LlamaFehler:
        return LlamaFehler(
            f"Das Sprachmodell hat nicht innerhalb von {self.timeout:g} Sekunden geantwortet "
            "(LLAMA_TIMEOUT). Die Anfrage war vermutlich zu aufwendig für den Server – bitte "
            "die Aufgaben einzeln schicken oder LLAMA_TIMEOUT in /etc/missa/missa.env erhöhen.",
            504,
            ZEITLIMIT,
            f"{type(fehler).__name__}: {fehler}",
        )

    def _nicht_erreichbar(self, fehler: Exception) -> LlamaFehler:
        return LlamaFehler(
            f"Das Sprachmodell ist nicht erreichbar (llama-server unter {self.basis_url}). "
            "Bitte prüfen, ob der llama-server auf ai01 läuft, und dann nochmal versuchen.",
            502,
            MODELL_NICHT_ERREICHBAR,
            f"{type(fehler).__name__}: {fehler}",
        )

    async def gesund(self) -> bool:
        """``True``, wenn llama-server auf ``/health`` mit 200 antwortet."""
        try:
            antwort = await self._client.get("/health", headers=self._kopfzeilen(), timeout=5.0)
        except httpx.HTTPError:
            return False
        return antwort.status_code == 200

    async def schliessen(self) -> None:
        await self._client.aclose()
