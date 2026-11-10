"""Protokoll aller Kalenderänderungen von Missa – Grundlage für „mach das rückgängig“.

Jede Änderung wird mit dem Zustand vorher und nachher gespeichert (iCalendar-Text
für Termine, JSON für Kalender-Eigenschaften), in ``missa.db`` (Tabelle
``kalender_aenderungen``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden

SCHEMA = """
CREATE TABLE IF NOT EXISTS kalender_aenderungen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeit TEXT NOT NULL,
    aktion TEXT NOT NULL,
    kalender TEXT NOT NULL,
    uid TEXT,
    beschreibung TEXT NOT NULL,
    vorher TEXT,
    nachher TEXT,
    rueckgaengig INTEGER NOT NULL DEFAULT 0
);
"""

AKTIONEN = {
    "termin_neu": "Termin eingetragen",
    "termin_geaendert": "Termin geändert",
    "termin_geloescht": "Termin gelöscht",
    "kalender_neu": "Kalender angelegt",
    "kalender_geaendert": "Kalender geändert",
}


@dataclass(frozen=True)
class Aenderung:
    id: int
    zeit: str
    aktion: str
    kalender: str
    uid: str | None
    beschreibung: str
    vorher: str | None
    nachher: str | None
    rueckgaengig: bool

    def zeile(self) -> str:
        status = " (schon rückgängig gemacht)" if self.rueckgaengig else ""
        return (
            f"- Änderung #{self.id} um {self.zeit[11:16]} am {self.zeit[8:10]}.{self.zeit[5:7]}.: "
            f"{AKTIONEN.get(self.aktion, self.aktion)} im Kalender „{self.kalender}“ – "
            f"{self.beschreibung}{status}"
        )

    def als_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "zeit": self.zeit,
            "aktion": self.aktion,
            "kalender": self.kalender,
            "uid": self.uid,
            "beschreibung": self.beschreibung,
            "vorher": self.vorher,
            "nachher": self.nachher,
            "rueckgaengig": self.rueckgaengig,
        }

    def json_vorher(self) -> dict[str, Any]:
        return json.loads(self.vorher or "{}")

    def json_nachher(self) -> dict[str, Any]:
        return json.loads(self.nachher or "{}")


class KalenderProtokoll:
    def __init__(self, datei: Path, zone: ZoneInfo) -> None:
        self.datei = datei
        self.zone = zone
        self._bereit = False

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """Verbindung; legt die Tabelle beim ersten Zugriff an."""
        with verbinden(self.datei) as db:
            if not self._bereit:
                db.executescript(SCHEMA)
                self._bereit = True
            yield db

    @staticmethod
    def _aenderung(zeile: sqlite3.Row) -> Aenderung:
        return Aenderung(
            id=zeile["id"],
            zeit=zeile["zeit"],
            aktion=zeile["aktion"],
            kalender=zeile["kalender"],
            uid=zeile["uid"],
            beschreibung=zeile["beschreibung"],
            vorher=zeile["vorher"],
            nachher=zeile["nachher"],
            rueckgaengig=bool(zeile["rueckgaengig"]),
        )

    def eintragen(
        self,
        aktion: str,
        kalender: str,
        beschreibung: str,
        vorher: str | dict[str, Any] | None = None,
        nachher: str | dict[str, Any] | None = None,
        uid: str | None = None,
    ) -> Aenderung:
        def text(wert: str | dict[str, Any] | None) -> str | None:
            if isinstance(wert, dict):
                return json.dumps(wert, ensure_ascii=False)
            return wert

        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO kalender_aenderungen "
                "(zeit, aktion, kalender, uid, beschreibung, vorher, nachher) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(self.zone).isoformat(timespec="seconds"),
                    aktion,
                    kalender,
                    uid,
                    beschreibung,
                    text(vorher),
                    text(nachher),
                ),
            )
            zeile = db.execute(
                "SELECT * FROM kalender_aenderungen WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._aenderung(zeile)

    def holen(self, aenderung_id: int) -> Aenderung | None:
        with self._db() as db:
            zeile = db.execute(
                "SELECT * FROM kalender_aenderungen WHERE id = ?", (aenderung_id,)
            ).fetchone()
        return self._aenderung(zeile) if zeile else None

    def letzte_offene(self) -> Aenderung | None:
        """Die jüngste Änderung, die noch nicht rückgängig gemacht wurde."""
        with self._db() as db:
            zeile = db.execute(
                "SELECT * FROM kalender_aenderungen WHERE rueckgaengig = 0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._aenderung(zeile) if zeile else None

    def liste(self, anzahl: int = 50) -> list[Aenderung]:
        with self._db() as db:
            zeilen = db.execute(
                "SELECT * FROM kalender_aenderungen ORDER BY id DESC LIMIT ?", (anzahl,)
            ).fetchall()
        return [self._aenderung(z) for z in zeilen]

    def als_rueckgaengig_markieren(self, aenderung_id: int) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE kalender_aenderungen SET rueckgaengig = 1 WHERE id = ?", (aenderung_id,)
            )
