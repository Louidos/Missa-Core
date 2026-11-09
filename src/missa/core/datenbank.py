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
