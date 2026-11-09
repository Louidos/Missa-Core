"""Gesprächsverlauf pro Sitzung in SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from missa.core.datenbank import verbinden

SCHEMA = """
CREATE TABLE IF NOT EXISTS nachrichten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sitzung TEXT NOT NULL,
    rolle TEXT NOT NULL CHECK (rolle IN ('user', 'assistant')),
    inhalt TEXT NOT NULL,
    erstellt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nachrichten_sitzung ON nachrichten (sitzung, id);
"""


class Verlauf:
    """Speichert Nutzer- und Missa-Nachrichten je Sitzungs-ID."""

    def __init__(self, datei: Path) -> None:
        self.datei = datei
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def speichern(self, sitzung: str, rolle: str, inhalt: str) -> None:
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO nachrichten (sitzung, rolle, inhalt, erstellt) VALUES (?, ?, ?, ?)",
                (sitzung, rolle, inhalt, datetime.now(UTC).isoformat(timespec="seconds")),
            )

    def austausch_speichern(self, sitzung: str, frage: str, antwort: str) -> None:
        """Speichert eine Frage und die Antwort darauf in einem Rutsch."""
        jetzt = datetime.now(UTC).isoformat(timespec="seconds")
        with verbinden(self.datei) as db:
            db.executemany(
                "INSERT INTO nachrichten (sitzung, rolle, inhalt, erstellt) VALUES (?, ?, ?, ?)",
                [(sitzung, "user", frage, jetzt), (sitzung, "assistant", antwort, jetzt)],
            )

    def laden(self, sitzung: str, anzahl: int = 20) -> list[dict[str, str]]:
        """Die letzten ``anzahl`` Nachrichten der Sitzung, älteste zuerst."""
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT rolle, inhalt FROM nachrichten WHERE sitzung = ? ORDER BY id DESC LIMIT ?",
                (sitzung, anzahl),
            ).fetchall()
        return [{"role": z["rolle"], "content": z["inhalt"]} for z in reversed(zeilen)]
