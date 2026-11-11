"""Dauerhafter Zeitplaner für Erinnerungen und Routinen.

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
- Routinen (``art = 'routine'``) stehen in derselben Tabelle und laufen über
  dieselbe Schleife. Statt eines Textes wird zur Zeit ihre Anweisung an Missa
  übergeben (:attr:`Zeitplaner.routine_ausloeser`, siehe
  :mod:`missa.core.hintergrund`). Pausierte Einträge werden übersprungen.
- Regelmäßige Aufträge (:meth:`Zeitplaner.regelmaessig`) laufen alle *n* Minuten
  oder Stunden (z. B. Server-Monitoring jede Minute). Ihr letzter Lauf steht in der
  Datenbank: Nach einem Neustart läuft ein Auftrag erst wieder, wenn sein Intervall
  um ist – ein überfälliger sofort.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.prompt import datum_lang
from missa.core.werkzeuge import WerkzeugFehler
from missa.core.zeitangaben import Wiederholung, wandzeit
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
CREATE TABLE IF NOT EXISTS zeitplaner_auftraege (
    name TEXT PRIMARY KEY,
    letzter_lauf TEXT NOT NULL
);
"""

#: Spalten, die nach Version 0.3.0 dazugekommen sind (automatische Migration beim Start).
NEUE_SPALTEN = {
    "art": "TEXT NOT NULL DEFAULT 'erinnerung'",
    "anweisung": "TEXT",
    "pausiert": "INTEGER NOT NULL DEFAULT 0",
}
ARTEN = ("erinnerung", "routine")
ART_NAMEN = {"erinnerung": "Erinnerung", "routine": "Routine"}

#: Ein täglicher Auftrag (z. B. Geburtstage prüfen); darf async sein.
Auftrag = Callable[[], Any]
#: Wird für eine fällige Routine aufgerufen (Routine, verspätet?) und reiht sie ein.
RoutinenAusloeser = Callable[["Erinnerung", bool], Any]


def migrieren(db: sqlite3.Connection) -> None:
    """Ergänzt fehlende Spalten in ``erinnerungen`` (alte Daten bleiben erhalten)."""
    vorhanden = {zeile["name"] for zeile in db.execute("PRAGMA table_info(erinnerungen)")}
    for name, typ in NEUE_SPALTEN.items():
        if name not in vorhanden:
            log.info("Migration: Spalte erinnerungen.%s wird ergänzt", name)
            db.execute(f"ALTER TABLE erinnerungen ADD COLUMN {name} {typ}")


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
    art: str = "erinnerung"
    anweisung: str | None = None
    pausiert: bool = False

    def zeile(self) -> str:
        if self.art == "routine":
            teile = f"- #{self.id} „{self.text}“ – "
            teile += (
                "PAUSIERT" if self.pausiert else f"nächster Lauf {zeitpunkt_text(self.faellig)}"
            )
        else:
            teile = f"- #{self.id} „{self.text}“ – {zeitpunkt_text(self.faellig)}"
        if self.wiederholung:
            teile += f" (wiederholt sich {self.wiederholung.text()} um {self.uhrzeit:%H:%M} Uhr)"
        if self.art == "routine" and self.anweisung:
            teile += f"\n  Anweisung: {self.anweisung}"
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
            "art": self.art,
            "anweisung": self.anweisung,
            "pausiert": self.pausiert,
        }


@dataclass
class TaeglicherAuftrag:
    """Läuft einmal am Tag ab ``uhrzeit`` – auch nachgeholt, wenn der Dienst da aus war."""

    name: str
    uhrzeit: time
    funktion: Auftrag
    laeuft: asyncio.Task[Any] | None = field(default=None, repr=False)


@dataclass
class RegelmaessigerAuftrag:
    """Läuft alle ``intervall`` – der letzte Lauf steht in der Datenbank (überlebt Neustarts)."""

    name: str
    intervall: timedelta
    funktion: Auftrag
    naechster: datetime | None = None
    laeuft: asyncio.Task[Any] | None = field(default=None, repr=False)

    @property
    def db_name(self) -> str:
        return f"intervall:{self.name}"


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
        self._auftraege: dict[str, TaeglicherAuftrag] = {}
        self._intervalle: dict[str, RegelmaessigerAuftrag] = {}
        #: Nimmt fällige Routinen entgegen (setzt die App, siehe missa.core.hintergrund).
        self.routine_ausloeser: RoutinenAusloeser | None = None
        with verbinden(datei) as db:
            db.executescript(SCHEMA)
            migrieren(db)
            db.execute("CREATE INDEX IF NOT EXISTS erinnerungen_art ON erinnerungen (art, status)")

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
            art=zeile["art"],
            anweisung=zeile["anweisung"],
            pausiert=bool(zeile["pausiert"]),
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
        art: str = "erinnerung",
        anweisung: str | None = None,
    ) -> Erinnerung:
        """Legt eine Erinnerung (oder Routine) an. Mit ``schluessel`` nie doppelt."""
        if art not in ARTEN:
            raise ValueError(f"Unbekannte Art: {art}")
        text = " ".join(str(text).split())
        if not text:
            raise WerkzeugFehler("Woran soll ich dich erinnern? Der Text fehlt.")
        anweisung = (anweisung or "").strip() or None
        if art == "routine" and not anweisung:
            raise WerkzeugFehler("Was soll ich bei der Routine tun? Die Anweisung fehlt.")
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
                     herkunft, kanal, art, anweisung)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    art,
                    anweisung,
                ),
            )
            neu = db.execute(
                "SELECT * FROM erinnerungen WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        eintrag = self._erinnerung(neu)
        if herkunft == "chat":
            log.info("%s #%s angelegt: „%s“", ART_NAMEN[art], eintrag.id, eintrag.text)
        self.wecken()
        return eintrag

    def holen(self, erinnerung_id: int, art: str | None = "erinnerung") -> Erinnerung | None:
        """Ein Eintrag nach ID; ``art=None`` liefert Erinnerungen und Routinen."""
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM erinnerungen WHERE id = ? AND status != 'geloescht' "
                "AND (? IS NULL OR art = ?)",
                (erinnerung_id, art, art),
            ).fetchone()
        return self._erinnerung(zeile) if zeile else None

    def offene(self, art: str = "erinnerung") -> list[Erinnerung]:
        """Alle offenen Einträge einer Art (auch pausierte), nächster zuerst."""
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT * FROM erinnerungen WHERE status = 'offen' AND art = ? "
                "ORDER BY faellig, id",
                (art,),
            ).fetchall()
        return [self._erinnerung(z) for z in zeilen]

    def zuletzt_zugestellt(self, innerhalb: timedelta = timedelta(hours=12)) -> Erinnerung | None:
        grenze = _utc_text(self.jetzt() - innerhalb)
        with verbinden(self.datei) as db:
            zeile = db.execute(
                """
                SELECT * FROM erinnerungen
                WHERE zugestellt IS NOT NULL AND zugestellt >= ? AND status != 'geloescht'
                    AND art = 'erinnerung'
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

    def loeschen(self, erinnerung_id: int, art: str = "erinnerung") -> Erinnerung:
        alt = self.holen(erinnerung_id, art)
        if alt is None:
            raise WerkzeugFehler(f"Eine {ART_NAMEN[art]} #{erinnerung_id} gibt es nicht (mehr).")
        with self._sperre, verbinden(self.datei) as db:
            db.execute(
                "UPDATE erinnerungen SET status = 'geloescht' WHERE id = ?", (erinnerung_id,)
            )
        log.info("%s #%s gelöscht: „%s“", ART_NAMEN[art], alt.id, alt.text)
        self.wecken()
        return alt

    def pausieren(
        self, erinnerung_id: int, pausiert: bool = True, art: str = "routine"
    ) -> Erinnerung:
        """Pausiert einen Eintrag oder setzt ihn fort.

        Beim Fortsetzen werden verpasste Läufe nicht nachgeholt: Es geht mit dem
        nächsten regulären Zeitpunkt weiter.
        """
        alt = self.holen(erinnerung_id, art)
        if alt is None or alt.status != "offen":
            raise WerkzeugFehler(f"Eine {ART_NAMEN[art]} #{erinnerung_id} gibt es nicht (mehr).")
        felder: dict[str, Any] = {"pausiert": int(pausiert)}
        jetzt = self.jetzt()
        if not pausiert and alt.faellig <= jetzt and alt.wiederholung is not None:
            felder["faellig"] = _utc_text(alt.wiederholung.naechster(jetzt, alt.uhrzeit, self.zone))
            felder["naechster_versuch"] = None
        zuweisung = ", ".join(f"{name} = ?" for name in felder)
        with self._sperre, verbinden(self.datei) as db:
            db.execute(
                f"UPDATE erinnerungen SET {zuweisung} WHERE id = ?",
                (*felder.values(), erinnerung_id),
            )
        log.info(
            "%s #%s %s: „%s“",
            ART_NAMEN[art],
            alt.id,
            "pausiert" if pausiert else "fortgesetzt",
            alt.text,
        )
        self.wecken()
        neu = self.holen(erinnerung_id, art)
        assert neu is not None
        return neu

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
                WHERE status = 'offen' AND pausiert = 0
                    AND COALESCE(naechster_versuch, faellig) <= ?
                ORDER BY faellig, id
                """,
                (_utc_text(jetzt),),
            ).fetchall()
        return [self._erinnerung(z) for z in zeilen]

    def naechste_faelligkeit(self) -> datetime | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT MIN(COALESCE(naechster_versuch, faellig)) AS naechste "
                "FROM erinnerungen WHERE status = 'offen' AND pausiert = 0"
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
            if erinnerung.art == "routine":
                if self._routine_ausloesen(erinnerung, jetzt):
                    anzahl += 1
                continue
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

    def _routine_ausloesen(self, routine: Erinnerung, jetzt: datetime) -> bool:
        """Übergibt eine fällige Routine an die Hintergrund-Ausführung."""
        if self.routine_ausloeser is None:
            self._fehlschlag(routine, jetzt, "keine Hintergrund-Ausführung eingerichtet")
            return False
        try:
            self.routine_ausloeser(routine, jetzt - routine.faellig > TOLERANZ)
        except Exception:
            log.exception("Routine #%s: konnte nicht eingereiht werden", routine.id)
            self._fehlschlag(routine, jetzt, "Einreihen fehlgeschlagen")
            return False
        self._zugestellt(routine, jetzt, "hintergrund")
        return True

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

    # -------------------------------------------------------------- tägliche Aufträge

    def taeglich(self, name: str, uhrzeit: time, funktion: Auftrag) -> None:
        """Registriert einen Auftrag, der jeden Tag ab ``uhrzeit`` einmal läuft."""
        self._auftraege[name] = TaeglicherAuftrag(name, uhrzeit, funktion)
        self.wecken()

    def _letzter_lauf(self, name: str) -> str | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT letzter_lauf FROM zeitplaner_auftraege WHERE name = ?", (name,)
            ).fetchone()
        return zeile["letzter_lauf"] if zeile else None

    def _auftrag_faellig(self, auftrag: TaeglicherAuftrag, jetzt: datetime) -> bool:
        if jetzt.time() < auftrag.uhrzeit:
            return False
        letzter = self._letzter_lauf(auftrag.name)
        return letzter is None or letzter < jetzt.date().isoformat()

    def regelmaessig(self, name: str, intervall: timedelta, funktion: Auftrag) -> None:
        """Registriert einen Auftrag, der alle ``intervall`` läuft (mindestens 10 Sekunden).

        Der erste Lauf kommt sofort, wenn der Auftrag noch nie lief oder überfällig ist;
        sonst, sobald seit dem gespeicherten letzten Lauf ``intervall`` vergangen ist.
        """
        if intervall < timedelta(seconds=10):
            raise ValueError("Das Intervall muss mindestens 10 Sekunden lang sein")
        self._intervalle[name] = RegelmaessigerAuftrag(name, intervall, funktion)
        self.wecken()

    def _intervall_beginn(self, auftrag: RegelmaessigerAuftrag, jetzt: datetime) -> datetime:
        letzter = self._letzter_lauf(auftrag.db_name)
        if not letzter:
            return jetzt
        try:
            naechster = datetime.fromisoformat(letzter).astimezone(self.zone) + auftrag.intervall
        except ValueError:
            return jetzt
        return min(naechster, jetzt + auftrag.intervall)

    def _vermerken(self, name: str, wert: str) -> None:
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO zeitplaner_auftraege (name, letzter_lauf) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET letzter_lauf = excluded.letzter_lauf",
                (name, wert),
            )

    def _intervalle_starten(self, jetzt: datetime) -> list[str]:
        gestartet = []
        for auftrag in self._intervalle.values():
            if auftrag.laeuft is not None and not auftrag.laeuft.done():
                continue
            if auftrag.naechster is None:
                auftrag.naechster = self._intervall_beginn(auftrag, jetzt)
            if jetzt < auftrag.naechster:
                continue
            self._vermerken(auftrag.db_name, _utc_text(jetzt))
            auftrag.naechster = jetzt + auftrag.intervall
            auftrag.laeuft = asyncio.create_task(self._auftrag(auftrag), name=auftrag.name)
            gestartet.append(auftrag.name)
        return gestartet

    async def auftraege_ausfuehren(self, warten: bool = False) -> list[str]:
        """Startet fällige tägliche und regelmäßige Aufträge. Gibt ihre Namen zurück."""
        jetzt = self.jetzt()
        gestartet = []
        for auftrag in self._auftraege.values():
            if auftrag.laeuft is not None and not auftrag.laeuft.done():
                continue
            if not self._auftrag_faellig(auftrag, jetzt):
                continue
            # Erst vermerken, dann laufen: ein fehlerhafter Auftrag läuft nicht in Schleife.
            self._vermerken(auftrag.name, jetzt.date().isoformat())
            auftrag.laeuft = asyncio.create_task(self._auftrag(auftrag), name=auftrag.name)
            gestartet.append(auftrag.name)
        gestartet += self._intervalle_starten(jetzt)
        if warten:
            alle = [*self._auftraege.values(), *self._intervalle.values()]
            await asyncio.gather(*(a.laeuft for a in alle if a.laeuft is not None))
        return gestartet

    async def _auftrag(self, auftrag: TaeglicherAuftrag | RegelmaessigerAuftrag) -> None:
        if isinstance(auftrag, TaeglicherAuftrag):
            log.info("Täglicher Auftrag „%s“ startet", auftrag.name)
        else:
            log.debug("Regelmäßiger Auftrag „%s“ startet", auftrag.name)
        try:
            if inspect.iscoroutinefunction(auftrag.funktion):
                await auftrag.funktion()
            else:
                await asyncio.to_thread(auftrag.funktion)
        except Exception:
            log.exception("Auftrag „%s“ ist fehlgeschlagen", auftrag.name)
        self.wecken()  # er hat vielleicht neue Erinnerungen angelegt

    def _naechster_auftrag(self) -> datetime | None:
        jetzt = self.jetzt()
        zeiten = []
        for auftrag in self._auftraege.values():
            heute = wandzeit(jetzt.date(), auftrag.uhrzeit, self.zone)
            zeiten.append(heute if heute > jetzt else heute + timedelta(days=1))
        for intervall in self._intervalle.values():
            if intervall.laeuft is not None and not intervall.laeuft.done():
                continue
            zeiten.append(intervall.naechster or jetzt)
        return min(zeiten) if zeiten else None

    # -------------------------------------------------------------- Hintergrund-Task

    def wecken(self) -> None:
        """Weckt den Hintergrund-Task (aus jedem Thread aufrufbar)."""
        if self._loop is None or self._wecker is None or self._loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._wecker.set)

    def _schlafzeit(self) -> float:
        kandidaten = [z for z in (self.naechste_faelligkeit(), self._naechster_auftrag()) if z]
        naechste = min(kandidaten) if kandidaten else None
        if naechste is None:
            return MAX_SCHLAF
        return max(0.0, min(MAX_SCHLAF, (naechste - self.jetzt()).total_seconds()))

    async def _schleife(self) -> None:
        assert self._wecker is not None
        while True:
            self._wecker.clear()
            try:
                await self.faellige_zustellen()
                await self.auftraege_ausfuehren()
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
        for auftrag in [*self._auftraege.values(), *self._intervalle.values()]:
            if auftrag.laeuft is not None and not auftrag.laeuft.done():
                auftrag.laeuft.cancel()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._loop = None
        self._wecker = None
