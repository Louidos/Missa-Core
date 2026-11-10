"""Dauerhafter Zeitplaner für Erinnerungen.

Erinnerungen stehen in der SQLite-Datenbank (Tabelle ``erinnerungen``) und
überleben damit Neustarts und Updates. Ein Hintergrund-Task schläft genau bis
zur nächsten fälligen Erinnerung (höchstens 30 Sekunden am Stück, damit auch
eine verstellte Uhr schnell bemerkt wird) und stellt sie über die
:class:`~missa.core.zustellung.Zustellung` zu.

- Pünktlich: Der Task wird beim Anlegen oder Ändern sofort geweckt und wartet
  dann auf die Sekunde genau.
- Nachholen: War der Dienst zum Zeitpunkt aus, wird die Erinnerung direkt nach
  dem Start zugestellt – mit dem Hinweis, dass sie verspätet ist.
- Wiederkehrend: Nach der Zustellung wird der nächste Termin nach der
  Wanduhrzeit berechnet (7:00 bleibt 7:00, auch über die Zeitumstellung).
- Fehlschlag: Klappt die Zustellung nicht (z. B. Nextcloud nicht erreichbar),
  wird es mit wachsendem Abstand (bis 15 Minuten) erneut versucht.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.prompt import datum_lang
from missa.core.werkzeuge import WerkzeugFehler
from missa.core.zeitangaben import Wiederholung
from missa.core.zustellung import ZustellFehler, Zustellung

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS erinnerungen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    nachricht TEXT,
    faellig TEXT NOT NULL,
    uhrzeit TEXT NOT NULL,
    regel TEXT,
    status TEXT NOT NULL DEFAULT 'offen'
        CHECK (status IN ('offen', 'zugestellt', 'geloescht')),
    erstellt TEXT NOT NULL,
    zugestellt TEXT,
    naechster_versuch TEXT,
    versuche INTEGER NOT NULL DEFAULT 0,
    schluessel TEXT UNIQUE,
    kanal TEXT,
    herkunft TEXT NOT NULL DEFAULT 'chat'
);
CREATE INDEX IF NOT EXISTS erinnerungen_faellig ON erinnerungen (status, faellig);
"""

#: Ab so viel Verspätung bekommt die Nachricht einen Hinweis.
TOLERANZ = timedelta(seconds=60)
#: Längster Schlaf am Stück (Sekunden).
MAX_SCHLAF = 30.0
#: Wartezeiten zwischen erneuten Zustellversuchen (Sekunden).
WIEDERHOLUNGEN = (30, 60, 120, 300, 600, 900)


def _utc_text(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _aus_utc(text: str | None, zone: ZoneInfo) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text).astimezone(zone)


def zeitpunkt_text(wert: datetime) -> str:
    """z. B. ``Mittwoch, 11. November 2026 um 06:30 Uhr``."""
    return f"{datum_lang(wert.date())} um {wert:%H:%M} Uhr"


def abstand_text(von: datetime, bis: datetime) -> str:
    """Grobe Dauer bis zu einem Zeitpunkt, z. B. ``in 2 Stunden 5 Minuten``."""
    sekunden = int((bis - von).total_seconds())
    if sekunden < 60:
        return "in weniger als einer Minute"
    minuten = (sekunden + 59) // 60
    tage, rest = divmod(minuten, 24 * 60)
    stunden, minuten = divmod(rest, 60)
    teile = []
    if tage:
        teile.append(f"{tage} Tag{'en' if tage != 1 else ''}")
    if stunden and tage < 3:
        teile.append(f"{stunden} Stunde{'n' if stunden != 1 else ''}")
    if minuten and not tage:
        teile.append(f"{minuten} Minute{'n' if minuten != 1 else ''}")
    return "in " + " ".join(teile)


@dataclass(frozen=True)
class Erinnerung:
    id: int
    text: str
    nachricht: str | None
    faellig: datetime
    uhrzeit: time
    wiederholung: Wiederholung | None
    status: str
    erstellt: str
    zugestellt: datetime | None
    versuche: int
    schluessel: str | None
    kanal: str | None
    herkunft: str

    def zeile(self) -> str:
        teile = f"- #{self.id} „{self.text}“ – {zeitpunkt_text(self.faellig)}"
        if self.wiederholung:
            teile += f" (wiederholt sich {self.wiederholung.text()})"
        if self.versuche:
            teile += f" – Zustellung bisher {self.versuche}× fehlgeschlagen"
        return teile

    def als_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "nachricht": self.nachricht,
            "faellig": self.faellig.isoformat(timespec="seconds"),
            "wiederholung": self.wiederholung.speichern() if self.wiederholung else None,
            "wiederholung_text": self.wiederholung.text() if self.wiederholung else None,
            "status": self.status,
            "erstellt": self.erstellt,
            "zugestellt": self.zugestellt.isoformat(timespec="seconds")
            if self.zugestellt
            else None,
            "fehlversuche": self.versuche,
            "kanal": self.kanal,
            "herkunft": self.herkunft,
        }


class Zeitplaner:
    """Speichert Erinnerungen und stellt sie pünktlich zu."""

    def __init__(
        self,
        datei: Path,
        zone: ZoneInfo,
        zustellung: Zustellung,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.zone = zone
        self.zustellung = zustellung
        self._uhr = uhr or (lambda: datetime.now(zone))
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wecker: asyncio.Event | None = None
        self._sperre = threading.Lock()
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Speichern

    def _erinnerung(self, zeile: sqlite3.Row) -> Erinnerung:
        stunde, minute = (int(t) for t in zeile["uhrzeit"].split(":"))
        return Erinnerung(
            id=zeile["id"],
            text=zeile["text"],
            nachricht=zeile["nachricht"],
            faellig=_aus_utc(zeile["faellig"], self.zone),  # type: ignore[arg-type]
            uhrzeit=time(stunde, minute),
            wiederholung=Wiederholung.lesen(zeile["regel"]),
            status=zeile["status"],
            erstellt=zeile["erstellt"],
            zugestellt=_aus_utc(zeile["zugestellt"], self.zone),
            versuche=zeile["versuche"],
            schluessel=zeile["schluessel"],
            kanal=zeile["kanal"],
            herkunft=zeile["herkunft"],
        )

    def anlegen(
        self,
        text: str,
        zeitpunkt: datetime,
        wiederholung: Wiederholung | None = None,
        nachricht: str | None = None,
        schluessel: str | None = None,
        herkunft: str = "chat",
        kanal: str | None = None,
    ) -> Erinnerung:
        """Legt eine Erinnerung an. Mit ``schluessel`` wird sie nie doppelt angelegt."""
        text = " ".join(str(text).split())
        if not text:
            raise WerkzeugFehler("Woran soll ich dich erinnern? Der Text fehlt.")
        zeitpunkt = zeitpunkt.astimezone(self.zone)
        nachricht = (nachricht or "").strip() or None
        with self._sperre, verbinden(self.datei) as db:
            if schluessel:
                vorhanden = db.execute(
                    "SELECT * FROM erinnerungen WHERE schluessel = ?", (schluessel,)
                ).fetchone()
                if vorhanden:
                    return self._erinnerung(vorhanden)
            cursor = db.execute(
                """
                INSERT INTO erinnerungen
                    (text, nachricht, faellig, uhrzeit, regel, erstellt, schluessel,
                     herkunft, kanal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text,
                    nachricht,
                    _utc_text(zeitpunkt),
                    f"{zeitpunkt:%H:%M}",
                    wiederholung.speichern() if wiederholung else None,
                    self.jetzt().isoformat(timespec="seconds"),
                    schluessel,
                    herkunft,
                    kanal,
                ),
            )
            neu = db.execute(
                "SELECT * FROM erinnerungen WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        self.wecken()
        return self._erinnerung(neu)

    def holen(self, erinnerung_id: int) -> Erinnerung | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM erinnerungen WHERE id = ? AND status != 'geloescht'",
                (erinnerung_id,),
            ).fetchone()
        return self._erinnerung(zeile) if zeile else None

    def offene(self) -> list[Erinnerung]:
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT * FROM erinnerungen WHERE status = 'offen' ORDER BY faellig, id"
            ).fetchall()
        return [self._erinnerung(z) for z in zeilen]

    def zuletzt_zugestellt(self, innerhalb: timedelta = timedelta(hours=12)) -> Erinnerung | None:
        grenze = _utc_text(self.jetzt() - innerhalb)
        with verbinden(self.datei) as db:
            zeile = db.execute(
                """
                SELECT * FROM erinnerungen
                WHERE zugestellt IS NOT NULL AND zugestellt >= ? AND status != 'geloescht'
                ORDER BY zugestellt DESC, id DESC LIMIT 1
                """,
                (grenze,),
            ).fetchone()
        return self._erinnerung(zeile) if zeile else None

    def aendern(
        self,
        erinnerung_id: int,
        text: str | None = None,
        zeitpunkt: datetime | None = None,
        wiederholung: Wiederholung | bool | None = False,
        nachricht: str | None = None,
    ) -> Erinnerung:
        """Ändert eine Erinnerung. ``wiederholung=False`` lässt die Regel, wie sie ist."""
        alt = self.holen(erinnerung_id)
        if alt is None:
            raise WerkzeugFehler(
                f"Eine Erinnerung #{erinnerung_id} gibt es nicht (mehr). "
                "Zeig die Erinnerungen mit erinnerungen_anzeigen an."
            )
        felder: dict[str, Any] = {}
        if text is not None and " ".join(str(text).split()):
            felder["text"] = " ".join(str(text).split())
            if nachricht is None and alt.nachricht:
                felder["nachricht"] = None  # alte Formulierung passt nicht mehr
        if nachricht is not None:
            felder["nachricht"] = nachricht.strip() or None
        if zeitpunkt is not None:
            zeitpunkt = zeitpunkt.astimezone(self.zone)
            felder.update(
                faellig=_utc_text(zeitpunkt),
                uhrzeit=f"{zeitpunkt:%H:%M}",
                status="offen",
                naechster_versuch=None,
                versuche=0,
            )
        if wiederholung is not False:
            felder["regel"] = wiederholung.speichern() if wiederholung else None  # type: ignore[union-attr]
        if felder:
            zuweisung = ", ".join(f"{name} = ?" for name in felder)
            with self._sperre, verbinden(self.datei) as db:
                db.execute(
                    f"UPDATE erinnerungen SET {zuweisung} WHERE id = ?",
                    (*felder.values(), erinnerung_id),
                )
            self.wecken()
        neu = self.holen(erinnerung_id)
        assert neu is not None
        return neu

    def loeschen(self, erinnerung_id: int) -> Erinnerung:
        alt = self.holen(erinnerung_id)
        if alt is None:
            raise WerkzeugFehler(f"Eine Erinnerung #{erinnerung_id} gibt es nicht (mehr).")
        with self._sperre, verbinden(self.datei) as db:
            db.execute(
                "UPDATE erinnerungen SET status = 'geloescht' WHERE id = ?", (erinnerung_id,)
            )
        self.wecken()
        return alt

    def schlummern(self, minuten: float, erinnerung_id: int | None = None) -> Erinnerung:
        """Stellt eine Erinnerung ``minuten`` später noch einmal zu.

        Ohne ID gilt die zuletzt zugestellte. Bei wiederkehrenden Erinnerungen
        bleibt die Regel unverändert; es kommt eine einmalige Kopie dazu.
        """
        if minuten <= 0:
            raise WerkzeugFehler("Die Schlummerzeit muss größer als 0 Minuten sein.")
        if erinnerung_id is None:
            ziel = self.zuletzt_zugestellt()
            if ziel is None:
                raise WerkzeugFehler(
                    "Ich habe dir in letzter Zeit keine Erinnerung geschickt, die ich "
                    "verschieben könnte. Welche meinst du?"
                )
        else:
            ziel = self.holen(erinnerung_id)
            if ziel is None:
                raise WerkzeugFehler(f"Eine Erinnerung #{erinnerung_id} gibt es nicht (mehr).")
        neu_faellig = self.jetzt() + timedelta(minutes=minuten)
        if ziel.wiederholung is not None:
            return self.anlegen(ziel.text, neu_faellig, nachricht=ziel.nachricht, kanal=ziel.kanal)
        return self.aendern(ziel.id, zeitpunkt=neu_faellig)

    # -------------------------------------------------------------- Zustellen

    def _faellige(self, jetzt: datetime) -> list[Erinnerung]:
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                """
                SELECT * FROM erinnerungen
                WHERE status = 'offen' AND COALESCE(naechster_versuch, faellig) <= ?
                ORDER BY faellig, id
                """,
                (_utc_text(jetzt),),
            ).fetchall()
        return [self._erinnerung(z) for z in zeilen]

    def naechste_faelligkeit(self) -> datetime | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT MIN(COALESCE(naechster_versuch, faellig)) AS naechste "
                "FROM erinnerungen WHERE status = 'offen'"
            ).fetchone()
        return _aus_utc(zeile["naechste"], self.zone)

    def nachricht_fuer(self, erinnerung: Erinnerung, jetzt: datetime) -> str:
        text = erinnerung.nachricht or f"⏰ Erinnerung: {erinnerung.text}"
        if jetzt - erinnerung.faellig > TOLERANZ:
            text += (
                f"\n\n_(Verspätet: Eigentlich war das für {zeitpunkt_text(erinnerung.faellig)} "
                "geplant – da war ich gerade nicht erreichbar.)_"
            )
        return text

    async def faellige_zustellen(self) -> int:
        """Stellt alle fälligen Erinnerungen zu. Gibt die Zahl der zugestellten zurück."""
        jetzt = self.jetzt()
        anzahl = 0
        for erinnerung in self._faellige(jetzt):
            text = self.nachricht_fuer(erinnerung, jetzt)
            try:
                kanal = await self.zustellung.senden(text, erinnerung.kanal)
            except ZustellFehler as fehler:
                self._fehlschlag(erinnerung, jetzt, str(fehler))
                continue
            except Exception:
                log.exception("Erinnerung #%s: unerwarteter Fehler", erinnerung.id)
                self._fehlschlag(erinnerung, jetzt, "unerwarteter Fehler")
                continue
            self._zugestellt(erinnerung, jetzt, kanal)
            anzahl += 1
        return anzahl

    def _fehlschlag(self, erinnerung: Erinnerung, jetzt: datetime, grund: str) -> None:
        warte = WIEDERHOLUNGEN[min(erinnerung.versuche, len(WIEDERHOLUNGEN) - 1)]
        log.warning(
            "Erinnerung #%s nicht zugestellt (%s) – neuer Versuch in %s s",
            erinnerung.id,
            grund,
            warte,
        )
        with self._sperre, verbinden(self.datei) as db:
            db.execute(
                "UPDATE erinnerungen SET versuche = versuche + 1, naechster_versuch = ? "
                "WHERE id = ? AND status = 'offen'",
                (_utc_text(jetzt + timedelta(seconds=warte)), erinnerung.id),
            )

    def _zugestellt(self, erinnerung: Erinnerung, jetzt: datetime, kanal: str) -> None:
        verspaetung = (jetzt - erinnerung.faellig).total_seconds()
        log.info(
            "Erinnerung #%s über %s zugestellt (%.1f s nach Fälligkeit)",
            erinnerung.id,
            kanal,
            verspaetung,
        )
        with self._sperre, verbinden(self.datei) as db:
            if erinnerung.wiederholung is not None:
                # Verpasste Wiederholungen nicht nachliefern: nächster Termin nach jetzt.
                naechster = erinnerung.wiederholung.naechster(
                    max(jetzt, erinnerung.faellig), erinnerung.uhrzeit, self.zone
                )
                db.execute(
                    "UPDATE erinnerungen SET faellig = ?, zugestellt = ?, versuche = 0, "
                    "naechster_versuch = NULL WHERE id = ? AND status = 'offen'",
                    (_utc_text(naechster), _utc_text(jetzt), erinnerung.id),
                )
            else:
                db.execute(
                    "UPDATE erinnerungen SET status = 'zugestellt', zugestellt = ?, "
                    "versuche = 0, naechster_versuch = NULL WHERE id = ? AND status = 'offen'",
                    (_utc_text(jetzt), erinnerung.id),
                )

    # -------------------------------------------------------------- Hintergrund-Task

    def wecken(self) -> None:
        """Weckt den Hintergrund-Task (aus jedem Thread aufrufbar)."""
        if self._loop is None or self._wecker is None or self._loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._wecker.set)

    def _schlafzeit(self) -> float:
        naechste = self.naechste_faelligkeit()
        if naechste is None:
            return MAX_SCHLAF
        return max(0.0, min(MAX_SCHLAF, (naechste - self.jetzt()).total_seconds()))

    async def _schleife(self) -> None:
        assert self._wecker is not None
        while True:
            self._wecker.clear()
            try:
                await self.faellige_zustellen()
                schlaf = self._schlafzeit()
            except Exception:
                log.exception("Zeitplaner: Fehler in der Schleife")
                schlaf = MAX_SCHLAF
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wecker.wait(), timeout=schlaf)

    def starten(self) -> None:
        """Startet den Hintergrund-Task im laufenden Event-Loop."""
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._wecker = asyncio.Event()
        offen = len(self.offene())
        verpasst = len(self._faellige(self.jetzt()))
        log.info(
            "Zeitplaner läuft: %d offene Erinnerung(en), davon %d verpasst und werden nachgeholt",
            offen,
            verpasst,
        )
        self._task = asyncio.create_task(self._schleife(), name="missa-zeitplaner")

    async def stoppen(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._loop = None
        self._wecker = None
