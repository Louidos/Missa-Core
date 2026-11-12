"""Notizen in Nextcloud Notes: anlegen, ergänzen, durchsuchen, lesen.

Nutzt die Notes-API (``/index.php/apps/notes/api/v1/notes``) mit Benutzer und
App-Passwort aus der Konfiguration. Neue Notizen landen in der Kategorie
``NEXTCLOUD_NOTIZEN_KATEGORIE`` (Standard ``Missa``). Gelöscht wird nie etwas.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler

log = logging.getLogger(__name__)

API = "/index.php/apps/notes/api/v1/notes"
#: So viele Zeichen einer Notiz zeigt notiz_lesen höchstens.
MAX_ZEICHEN = 8000
MAX_TREFFER = 10


def _woerter(text: str) -> list[str]:
    return [w for w in str(text).casefold().split() if len(w) > 1]


class Notizen:
    """Zugriff auf Nextcloud Notes eines Benutzers."""

    def __init__(
        self,
        url: str,
        benutzer: str,
        app_passwort: str,
        zone: ZoneInfo,
        kategorie: str = "Missa",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.basis = url.rstrip("/") + API
        self.zone = zone
        self.kategorie = kategorie.strip()
        self._client = httpx.Client(
            auth=(benutzer, app_passwort),
            timeout=30,
            transport=transport,
            headers={"Accept": "application/json", "OCS-APIRequest": "true"},
        )

    def _anfrage(self, methode: str, pfad: str = "", **weiter: Any) -> Any:
        try:
            antwort = self._client.request(methode, self.basis + pfad, **weiter)
        except httpx.HTTPError as fehler:
            raise WerkzeugFehler(f"Nextcloud Notes nicht erreichbar ({fehler})") from fehler
        if antwort.status_code == 401:
            raise WerkzeugFehler("Nextcloud Notes: Anmeldung abgelehnt (App-Passwort prüfen)")
        if antwort.status_code == 404:
            raise WerkzeugFehler(
                "Nextcloud Notes: nicht gefunden – ist die App „Notes“ installiert, und gibt es "
                "die Notiz noch?"
            )
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"Nextcloud Notes antwortet mit {antwort.status_code}")
        try:
            return antwort.json()
        except ValueError as fehler:
            raise WerkzeugFehler("Nextcloud Notes lieferte keine gültige Antwort") from fehler

    def _alle(self) -> list[dict[str, Any]]:
        daten = self._anfrage("GET")
        return [n for n in daten if isinstance(n, dict)] if isinstance(daten, list) else []

    def _zeile(self, notiz: dict[str, Any]) -> str:
        zeile = f"- #{notiz.get('id')} „{notiz.get('title') or '(ohne Titel)'}“"
        if notiz.get("category"):
            zeile += f" | Kategorie: {notiz['category']}"
        if geaendert := notiz.get("modified"):
            zeile += f" | geändert {datetime.fromtimestamp(int(geaendert), self.zone):%d.%m.%Y}"
        return zeile

    def _finden(self, notiz: str | int) -> dict[str, Any]:
        text = str(notiz).strip().lstrip("#")
        if text.isdigit():
            return self._anfrage("GET", f"/{int(text)}")
        gesucht = text.casefold()
        treffer = [n for n in self._alle() if str(n.get("title", "")).casefold() == gesucht]
        if not treffer:
            treffer = [n for n in self._alle() if gesucht in str(n.get("title", "")).casefold()]
        if len(treffer) != 1:
            raise WerkzeugFehler(
                f"Keine eindeutige Notiz „{text}“ gefunden – such sie mit notizen_suchen und "
                "nimm die Nummer."
            )
        return treffer[0]

    # -------------------------------------------------------------- Werkzeuge

    def anlegen(self, titel: str, text: str, kategorie: str | None = None) -> str:
        titel = " ".join(str(titel).split())
        if not titel:
            raise WerkzeugFehler("Die Notiz braucht einen Titel.")
        inhalt = f"# {titel}\n\n{str(text).strip()}\n"
        notiz = self._anfrage(
            "POST",
            json={
                "title": titel,
                "content": inhalt,
                "category": (kategorie or self.kategorie or "").strip(),
            },
        )
        log.info("Notiz #%s angelegt: „%s“", notiz.get("id"), titel)
        return "Notiz in Nextcloud Notes angelegt:\n" + self._zeile(notiz)

    def ergaenzen(self, notiz: str | int, text: str) -> str:
        zusatz = str(text).strip()
        if not zusatz:
            raise WerkzeugFehler("Was soll ich ergänzen? Der Text fehlt.")
        alt = self._finden(notiz)
        jetzt = datetime.now(self.zone)
        inhalt = (
            str(alt.get("content", "")).rstrip() + f"\n\n**{jetzt:%d.%m.%Y %H:%M}:** {zusatz}\n"
        )
        neu = self._anfrage("PUT", f"/{alt['id']}", json={"content": inhalt})
        log.info("Notiz #%s ergänzt", alt.get("id"))
        return "Notiz ergänzt:\n" + self._zeile(neu or alt)

    def suchen(self, suchbegriff: str) -> str:
        woerter = _woerter(suchbegriff)
        if not woerter:
            raise WerkzeugFehler("Wonach soll ich suchen?")
        treffer = []
        for notiz in self._alle():
            titel = str(notiz.get("title", "")).casefold()
            inhalt = str(notiz.get("content", "")).casefold()
            punkte = sum(3 * (w in titel) + (w in inhalt) for w in woerter)
            if punkte:
                treffer.append((punkte, notiz))
        if not treffer:
            return f"Keine Notiz zu „{suchbegriff}“ gefunden."
        treffer.sort(key=lambda t: (-t[0], -int(t[1].get("modified") or 0)))
        zeilen = []
        for _, notiz in treffer[:MAX_TREFFER]:
            auszug = " ".join(str(notiz.get("content", "")).split())[:160]
            zeilen.append(self._zeile(notiz) + (f"\n  {auszug}" if auszug else ""))
        return f"Notizen zu „{suchbegriff}“:\n" + "\n".join(zeilen)

    def lesen(self, notiz: str | int) -> str:
        daten = self._finden(notiz)
        inhalt = str(daten.get("content", ""))
        if len(inhalt) > MAX_ZEICHEN:
            inhalt = inhalt[:MAX_ZEICHEN] + "\n… (gekürzt)"
        return self._zeile(daten) + "\n\n" + inhalt


def notizen_werkzeuge(notizen: Notizen) -> list[Werkzeug]:
    notiz_param = {"type": "string", "description": "Nummer (#12) oder Titel der Notiz"}
    return [
        Werkzeug(
            "notiz_anlegen",
            "Schreibt eine Notiz in Nextcloud Notes („Schreib dir auf, dass …“, „Notier …“).",
            {
                "type": "object",
                "properties": {
                    "titel": {"type": "string", "description": "Kurzer Titel"},
                    "text": {"type": "string", "description": "Inhalt (Markdown)"},
                    "kategorie": {"type": "string", "description": "Optional"},
                },
                "required": ["titel", "text"],
            },
            notizen.anlegen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "notiz_ergaenzen",
            "Hängt Text mit Datum an eine bestehende Notiz an.",
            {
                "type": "object",
                "properties": {
                    "notiz": notiz_param,
                    "text": {"type": "string", "description": "Was dazukommt"},
                },
                "required": ["notiz", "text"],
            },
            notizen.ergaenzen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "notizen_suchen",
            "Durchsucht Louis' Notizen in Nextcloud Notes nach Wörtern (Titel und Inhalt).",
            {
                "type": "object",
                "properties": {"suchbegriff": {"type": "string", "description": "Wörter"}},
                "required": ["suchbegriff"],
            },
            notizen.suchen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "notiz_lesen",
            "Zeigt den ganzen Inhalt einer Notiz.",
            {"type": "object", "properties": {"notiz": notiz_param}, "required": ["notiz"]},
            notizen.lesen,
            stufe=Stufe.LESEN,
        ),
    ]
