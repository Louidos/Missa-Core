"""Gemeinsamer Zugriff auf die SQLite-Datenbank ``MISSA_DATEN/missa.db``."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def verbinden(datei: Path) -> Iterator[sqlite3.Connection]:
    """Öffnet eine Verbindung, schreibt bei Erfolg (commit) und schließt sie wieder."""
    datei.parent.mkdir(parents=True, exist_ok=True)
    verbindung = sqlite3.connect(datei, timeout=10)
    verbindung.row_factory = sqlite3.Row
    try:
        verbindung.execute("PRAGMA journal_mode=WAL")
        yield verbindung
        verbindung.commit()
    except Exception:
        verbindung.rollback()
        raise
    finally:
        verbindung.close()


STAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS stand (
    name TEXT PRIMARY KEY,
    wert TEXT NOT NULL,
    geaendert TEXT NOT NULL
);
"""


def stand_lesen(datei: Path, name: str) -> str | None:
    """Liest einen internen Merkwert (z. B. die letzte Ereignis-Nummer); ``None`` = nie gesetzt.

    Anders als die Vorgaben ändert Louis diese Werte nie – Missa-Core merkt sich damit nur,
    wie weit sie gekommen ist, damit nach einem Neustart nichts doppelt kommt.
    """
    with verbinden(datei) as db:
        db.executescript(STAND_SCHEMA)
        zeile = db.execute("SELECT wert FROM stand WHERE name = ?", (name,)).fetchone()
    return zeile["wert"] if zeile else None


def stand_setzen(datei: Path, name: str, wert: str) -> None:
    """Speichert einen internen Merkwert dauerhaft (überlebt Neustarts)."""
    from datetime import UTC, datetime

    with verbinden(datei) as db:
        db.executescript(STAND_SCHEMA)
        db.execute(
            "INSERT INTO stand (name, wert, geaendert) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET wert = excluded.wert, "
            "geaendert = excluded.geaendert",
            (name, wert, datetime.now(UTC).isoformat(timespec="seconds")),
        )
