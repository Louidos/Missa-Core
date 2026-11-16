"""Ereignisse aus Missa Control von sich aus melden (über Nextcloud Talk).

Ein Hintergrund-Auftrag hält den Ereignis-Stream des Panels offen und stellt jedes
Ereignis über die vorhandenen Alarm-Stufen zu (mit Ruhezeit und Ruhefenster):

====================  =================================================================
``dringlichkeit``     Zustellung
====================  =================================================================
``niedrig``           nur ins Log
``normal``            Alarm-Stufe ``info``
``hoch``              Alarm-Stufe ``wichtig``
``dringend``          Alarm-Stufe ``notfall`` (kommt auch in der Ruhezeit)
====================  =================================================================

Besonderheiten: Eine wartende **Freigabe** kommt sofort – mit dem Text des Panels und dem
Hinweis, sie im Panel zu erlauben. In der Ruhezeit wird sie nur ins Log geschrieben (sie
läuft nach 5 Minuten ab; am Morgen wäre die Nachricht wertlos), ausser sie ist ``dringend``.
Ein **Test**-Ereignis kommt als kurze Bestätigung sofort an.

Der Text kommt wörtlich aus ``titel`` und ``nachricht`` – ohne Umweg über das Sprachmodell.
Die letzte Ereignis-Nummer steht in der Datenbank: Nach einem Neustart setzt der Stream genau
dort fort (``Last-Event-ID``), nichts kommt doppelt und nichts geht verloren. Beim allerersten
Start beginnt er bei „jetzt“ – alte Ereignisse werden nicht nachgeliefert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from missa.core.alarme import AlarmStufe
from missa.core.datenbank import stand_lesen, stand_setzen
from missa.integrationen.missa_control import (
    MissaControl,
    PanelEreignis,
    PanelFehler,
    PanelTokenUngueltig,
)

if TYPE_CHECKING:
    from pathlib import Path

    from missa.core.alarme import Alarme

log = logging.getLogger(__name__)

#: Name des Merkwerts in der Datenbank.
LETZTE_ID = "missa_control.letzte_ereignis_id"
QUELLE = "Missa Control"
STUFEN: dict[str, AlarmStufe | None] = {
    "niedrig": None,
    "normal": AlarmStufe.INFO,
    "hoch": AlarmStufe.WICHTIG,
    "dringend": AlarmStufe.NOTFALL,
}
FREIGABE_HINWEIS = "👉 Im Panel unter „Freigaben“ erlauben (oder ablehnen)."
#: Wartezeit vor dem Neuverbinden: wächst bis höchstens so viele Sekunden.
ERSTE_WARTEZEIT = 2.0
MAX_WARTEZEIT = 300.0
#: Bleibt eine Verbindung so lange stehen, beginnt die Wartezeit danach wieder klein.
STABIL_NACH = 60.0


class PanelEreignisse:
    """Empfängt die Ereignisse des Panels und meldet sie Louis."""

    def __init__(
        self,
        verbindung: MissaControl,
        alarme: Alarme,
        datei: Path,
        *,
        schlafen: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        uhr: Callable[[], float] = time.monotonic,
    ) -> None:
        self.verbindung = verbindung
        self.alarme = alarme
        self.datei = datei
        self._schlafen = schlafen
        self._uhr = uhr
        self.verbunden = False
        self.gemeldet = 0
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ Stand

    def letzte_id(self) -> int | None:
        wert = stand_lesen(self.datei, LETZTE_ID)
        try:
            return int(wert) if wert is not None else None
        except ValueError:
            log.warning("Missa Control: gespeicherte Ereignis-Nummer %r ist ungültig", wert)
            return None

    def _merken(self, nummer: int) -> None:
        stand_setzen(self.datei, LETZTE_ID, str(nummer))

    async def startpunkt(self) -> int:
        """Ab hier wird gemeldet: die gespeicherte Nummer oder – beim ersten Start – „jetzt“."""
        letzte = self.letzte_id()
        if letzte is not None:
            return letzte
        letzte = await self.verbindung.letzte_ereignis_id()
        self._merken(letzte)
        log.info(
            "Missa Control: erster Start der Ereignisse – beginne nach Nummer %d "
            "(ältere Ereignisse werden nicht nachgeliefert)",
            letzte,
        )
        return letzte

    # ------------------------------------------------------------------ Melden

    def melden(self, ereignis: PanelEreignis) -> None:
        """Stellt ein Ereignis zu – wörtlich, ohne Sprachmodell."""
        titel = ereignis.titel or ereignis.art or "Ereignis"
        if ereignis.art == "test":
            self.alarme.zeitplaner.anlegen(
                f"Test-Ereignis aus Missa Control: {titel}",
                self.alarme.jetzt(),
                nachricht="✅ Test-Ereignis aus Missa Control ist angekommen."
                + (f"\n{ereignis.nachricht}" if ereignis.nachricht else ""),
                schluessel=f"missa-control:{ereignis.id}",
                herkunft="alarm",
            )
            log.info("Missa Control: Test-Ereignis %d zugestellt", ereignis.id)
            return
        stufe = STUFEN.get(ereignis.dringlichkeit, AlarmStufe.INFO)
        if ereignis.art == "freigabe":
            stufe = stufe or AlarmStufe.INFO
            if stufe is not AlarmStufe.NOTFALL and self.alarme.in_ruhezeit():
                log.info(
                    "Missa Control: Freigabe %d in der Ruhezeit nicht gemeldet (läuft im Panel "
                    "nach 5 Minuten ab): %s",
                    ereignis.id,
                    titel,
                )
                return
            text = "\n".join(t for t in (ereignis.nachricht, FREIGABE_HINWEIS) if t)
            freigabe = ereignis.daten.get("freigabe_id") or ereignis.id
            schluessel = f"missa-control:freigabe:{freigabe}"
        else:
            if stufe is None:
                log.info(
                    "Missa Control: Ereignis %d (%s, niedrig) nur im Log: %s – %s",
                    ereignis.id,
                    ereignis.art,
                    titel,
                    ereignis.nachricht,
                )
                return
            text = ereignis.nachricht
            # Gleiches Ereignis derselben Quelle: Das Ruhefenster verhindert Wiederholungen.
            schluessel = f"missa-control:{ereignis.art}:{ereignis.quelle}:{titel}"
        quelle = f"{QUELLE}/{ereignis.quelle}" if ereignis.quelle else QUELLE
        self.alarme.melden(stufe, titel, text, schluessel=schluessel, quelle=quelle)

    def verarbeiten(self, ereignis: PanelEreignis, letzte: int) -> int:
        """Meldet ein Ereignis höchstens einmal und merkt sich seine Nummer."""
        if ereignis.id <= letzte:
            return letzte
        try:
            self.melden(ereignis)
            self.gemeldet += 1
        except Exception:
            # Ein kaputtes Ereignis darf den Stream nicht anhalten.
            log.exception("Missa Control: Ereignis %d konnte nicht gemeldet werden", ereignis.id)
        self._merken(ereignis.id)
        return ereignis.id

    # ------------------------------------------------------------------ Stream

    async def einmal(self) -> None:
        """Eine Verbindung: holt den Startpunkt, liest den Stream bis zum Abbruch."""
        letzte = await self.startpunkt()
        async for ereignis in self.verbindung.ereignis_stream(letzte):
            self.verbunden = True
            if ereignis is not None:
                letzte = self.verarbeiten(ereignis, letzte)

    async def laufen(self) -> None:
        """Hält den Stream offen und verbindet nach Abbrüchen mit wachsender Wartezeit neu."""
        wartezeit = ERSTE_WARTEZEIT
        while True:
            beginn = self._uhr()
            try:
                await self.einmal()
                grund = "Panel hat die Verbindung beendet"
            except PanelTokenUngueltig:
                grund = "Token ungültig (401)"
                log.error(
                    "Missa Control: Ereignisse – Token ungültig (401). Im Panel unter „Clients“ "
                    "einen neuen Token anlegen und in MISSA_CONTROL_TOKEN eintragen."
                )
            except PanelFehler as fehler:
                grund = str(fehler)
            except asyncio.CancelledError:
                self.verbunden = False
                raise
            except Exception as fehler:
                log.exception("Missa Control: unerwarteter Fehler im Ereignis-Stream")
                grund = type(fehler).__name__
            self.verbunden = False
            if self._uhr() - beginn >= STABIL_NACH:
                wartezeit = ERSTE_WARTEZEIT
            log.info(
                "Missa Control: Ereignis-Stream getrennt (%s) – neuer Versuch in %.0f s",
                grund,
                wartezeit,
            )
            await self._schlafen(wartezeit)
            wartezeit = min(wartezeit * 2, MAX_WARTEZEIT)

    def starten(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.laufen(), name="missa-control-ereignisse")

    async def stoppen(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def status(self) -> dict[str, Any]:
        return {"verbunden": self.verbunden, "letzte_id": self.letzte_id()}
