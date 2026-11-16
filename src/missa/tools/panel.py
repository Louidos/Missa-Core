"""Funktionen aus Missa Control als Werkzeuge für Missa.

Jede Funktion, die das Panel über sein MCP-Gateway anbietet, wird ein :class:`Werkzeug`:
Name und Beschreibung bleiben, das Parameter-Schema wird übernommen. Die Stufe kommt aus
``readOnlyHint``: ``lesen`` oder ``schreiben`` – bewusst nie ``zerstoererisch``. Über
Rückfragen entscheidet das Panel („erst nachfragen“ → Freigabe im Panel); eine zweite
Bestätigung im Chat gäbe es sonst doppelt. Notfall-Stopp und Aktionsprotokoll greifen
über den Werkzeugkasten wie bei jedem anderen Werkzeug.

Die Liste wird beim Start geholt, danach alle ``MISSA_CONTROL_AKTUALISIEREN`` Sekunden und
sofort, wenn das Panel meldet, dass es eine Funktion nicht (mehr) gibt. Ist das Panel weg,
bleiben die zuletzt bekannten Funktionen sichtbar; ein Aufruf sagt dann ehrlich, dass es
gerade nicht geht.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from missa.core.config import Einstellungen
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler, Werkzeugkasten, im_hintergrund
from missa.integrationen.missa_control import (
    MissaControl,
    PanelFehler,
    PanelFunktion,
    PanelNichtErreichbar,
    PanelTokenUngueltig,
)

log = logging.getLogger(__name__)

#: Name der Gruppe im Werkzeugkasten.
GRUPPE = "missa_control"
NICHT_ERREICHBAR = (
    "Missa Control ist gerade nicht erreichbar – die Funktion „{name}“ wurde nicht "
    "ausgeführt. Sag Louis das ehrlich; sobald das Panel wieder läuft, geht es wieder."
)
TOKEN_UNGUELTIG = (
    "Missa Control lehnt Missas Zugang ab (Token ungültig oder widerrufen) – „{name}“ wurde "
    "nicht ausgeführt. Louis muss im Panel unter „Clients“ einen neuen Token anlegen und in "
    "MISSA_CONTROL_TOKEN eintragen."
)
HINTERGRUND_GESPERRT = (
    "NICHT AUSGEFÜHRT: „{name}“ ist eine schreibende Funktion aus Missa Control. Im "
    "Hintergrund (Routine, Nachtschicht) darf Missa dort nur lesen "
    "(MISSA_CONTROL_IM_HINTERGRUND=nur_lesen). Schlag es Louis im nächsten Gespräch vor."
)
#: Woran man erkennt, dass das Panel eine Funktion nicht (mehr) kennt.
UNBEKANNT_MERKMAL = "gibt es nicht"


class PanelWerkzeuge:
    """Hält die Funktionen aus Missa Control im Werkzeugkasten aktuell und ruft sie auf."""

    def __init__(
        self,
        einstellungen: Einstellungen,
        verbindung: MissaControl,
        kasten: Werkzeugkasten,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.einstellungen = einstellungen
        self.verbindung = verbindung
        self.kasten = kasten
        self._uhr = uhr or (lambda: datetime.now(einstellungen.zone))
        #: Hat die letzte Anfrage ans Panel geklappt?
        self.verbunden = False
        self.letzte_aktualisierung: datetime | None = None
        self.letzter_fehler = ""
        self.ausgelassen: list[str] = []
        self._sperre = asyncio.Lock()
        self._nachladen: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------ Liste

    def _auslassen(self, name: str) -> bool:
        klein = name.casefold()
        return any(
            klein.startswith(vorsilbe) for vorsilbe in self.einstellungen.missa_control_auslassen
        )

    async def aktualisieren(self) -> bool:
        """Holt die Funktionsliste und tauscht die Panel-Werkzeuge aus. ``True`` bei Erfolg."""
        async with self._sperre:
            try:
                funktionen = await self.verbindung.funktionen()
            except PanelTokenUngueltig as fehler:
                self._getrennt(str(fehler))
                log.error(
                    "Missa Control: Token ungültig (401) – im Panel unter „Clients“ einen neuen "
                    "Token anlegen und in MISSA_CONTROL_TOKEN eintragen. Die zuletzt bekannten "
                    "%d Funktion(en) bleiben sichtbar.",
                    len(self.kasten.gruppe(GRUPPE)),
                )
                return False
            except PanelFehler as fehler:
                # Nur beim ersten Mal (oder bei einer neuen Ursache) warnen, nicht jede Minute.
                neu = self.verbunden or self.letzter_fehler != str(fehler)
                self._getrennt(str(fehler))
                (log.warning if neu else log.debug)(
                    "Missa Control nicht erreichbar: %s – die zuletzt bekannten %d Funktion(en) "
                    "bleiben sichtbar",
                    fehler,
                    len(self.kasten.gruppe(GRUPPE)),
                )
                return False
            werkzeuge, ausgelassen = self._werkzeuge(funktionen)
            vorher = set(self.kasten.gruppe(GRUPPE))
            doppelt = self.kasten.gruppe_ersetzen(GRUPPE, werkzeuge)
            for name in doppelt:
                log.warning("Missa Control: Funktion %s ist doppelt und wurde ausgelassen", name)
            self.ausgelassen = ausgelassen + doppelt
            nachher = set(self.kasten.gruppe(GRUPPE))
            if nachher != vorher or not self.verbunden:
                log.info(
                    "Missa Control: %d Funktion(en) geladen%s%s",
                    len(nachher),
                    f", neu: {', '.join(sorted(nachher - vorher))}" if nachher - vorher else "",
                    f", weg: {', '.join(sorted(vorher - nachher))}" if vorher - nachher else "",
                )
            self.verbunden = True
            self.letzter_fehler = ""
            self.letzte_aktualisierung = self._uhr()
            return True

    def _getrennt(self, fehler: str) -> None:
        self.verbunden = False
        self.letzter_fehler = fehler

    def _werkzeuge(self, funktionen: list[PanelFunktion]) -> tuple[list[Werkzeug], list[str]]:
        werkzeuge: list[Werkzeug] = []
        ausgelassen: list[str] = []
        grenze = self.einstellungen.missa_control_max_werkzeuge
        zu_viele: list[str] = []
        for funktion in funktionen:
            if self._auslassen(funktion.name):
                log.info(
                    "Missa Control: Funktion %s ausgelassen (MISSA_CONTROL_AUSLASSEN)",
                    funktion.name,
                )
                ausgelassen.append(funktion.name)
                continue
            if self.kasten.ist_eingebaut(funktion.name):
                log.warning(
                    "Missa Control: Funktion %s ausgelassen – ein eingebautes Werkzeug heisst "
                    "genauso und hat Vorrang",
                    funktion.name,
                )
                ausgelassen.append(funktion.name)
                continue
            if len(werkzeuge) >= grenze:
                zu_viele.append(funktion.name)
                continue
            werkzeuge.append(self._werkzeug(funktion))
        if zu_viele:
            log.warning(
                "Missa Control: mehr als %d Funktionen (MISSA_CONTROL_MAX_WERKZEUGE) – "
                "ausgelassen: %s",
                grenze,
                ", ".join(zu_viele),
            )
            ausgelassen.extend(zu_viele)
        return werkzeuge, ausgelassen

    def _werkzeug(self, funktion: PanelFunktion) -> Werkzeug:
        stufe = Stufe.LESEN if funktion.nur_lesen else Stufe.SCHREIBEN
        return Werkzeug(
            funktion.name,
            funktion.beschreibung,
            funktion.schema,
            self._aufrufer(funktion.name, stufe),
            stufe=stufe,
        )

    # ------------------------------------------------------------------ Aufruf

    def _aufrufer(self, name: str, stufe: Stufe) -> Callable[..., Awaitable[str]]:
        async def aufrufen(**argumente: Any) -> str:
            return await self.aufrufen(name, stufe, argumente)

        aufrufen.__name__ = name
        return aufrufen

    async def aufrufen(self, name: str, stufe: Stufe, argumente: dict[str, Any]) -> str:
        if (
            stufe is not Stufe.LESEN
            and im_hintergrund.get()
            and self.einstellungen.missa_control_im_hintergrund != "alles"
        ):
            raise WerkzeugFehler(HINTERGRUND_GESPERRT.format(name=name))
        try:
            ergebnis = await self.verbindung.aufrufen(name, argumente)
        except PanelTokenUngueltig as fehler:
            self._getrennt(str(fehler))
            log.error("Missa Control: Token ungültig (401) – im Panel unter „Clients“ prüfen")
            raise WerkzeugFehler(TOKEN_UNGUELTIG.format(name=name)) from fehler
        except PanelNichtErreichbar as fehler:
            self._getrennt(str(fehler))
            log.warning("Missa Control: Aufruf von %s fehlgeschlagen: %s", name, fehler)
            raise WerkzeugFehler(NICHT_ERREICHBAR.format(name=name)) from fehler
        except PanelFehler as fehler:
            raise WerkzeugFehler(
                f"Missa Control meldet einen Fehler bei „{name}“: {fehler}"
            ) from fehler
        self.verbunden = True
        if ergebnis.fehler:
            if UNBEKANNT_MERKMAL in ergebnis.text:
                self.sofort_aktualisieren()
            raise WerkzeugFehler(ergebnis.text)
        return ergebnis.text

    def sofort_aktualisieren(self) -> None:
        """Lädt die Liste im Hintergrund neu (z. B. wenn das Panel eine Funktion nicht kennt)."""
        if self._nachladen is not None and not self._nachladen.done():
            return
        with contextlib.suppress(RuntimeError):
            self._nachladen = asyncio.get_running_loop().create_task(
                self.aktualisieren(), name="missa-control-nachladen"
            )

    # ------------------------------------------------------------------ Status

    def status(self) -> dict[str, Any]:
        """Für ``GET /health``: verbunden, Anzahl Funktionen, letzte Aktualisierung."""
        return {
            "verbunden": self.verbunden,
            "funktionen": len(self.kasten.gruppe(GRUPPE)),
            "letzte_aktualisierung": self.letzte_aktualisierung.isoformat(timespec="seconds")
            if self.letzte_aktualisierung
            else None,
            "fehler": self.letzter_fehler or None,
        }

    async def schliessen(self) -> None:
        if self._nachladen is not None and not self._nachladen.done():
            self._nachladen.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._nachladen
        await self.verbindung.schliessen()


def panel_bauen(
    einstellungen: Einstellungen,
    kasten: Werkzeugkasten,
    transport: Any = None,
    uhr: Callable[[], datetime] | None = None,
) -> PanelWerkzeuge | None:
    """Baut die Anbindung an Missa Control – nur mit ``MISSA_CONTROL_URL`` und Token."""
    if not einstellungen.missa_control_konfiguriert:
        if einstellungen.missa_control_url:
            log.warning(
                "MISSA_CONTROL_URL ist gesetzt, aber kein MISSA_CONTROL_TOKEN – Missa Control "
                "bleibt aus"
            )
        return None
    verbindung = MissaControl(
        einstellungen.missa_control_url,
        einstellungen.missa_control_token,
        einstellungen.missa_control_token_file,
        einstellungen.missa_control_zeitlimit,
        transport=transport,
    )
    return PanelWerkzeuge(einstellungen, verbindung, kasten, uhr)
