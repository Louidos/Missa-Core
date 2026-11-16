"""Gemeinsamer Zugriff auf die SQLite-Datenbank ``MISSA_DATEN/missa.db``."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Iterator, Mapping
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


#: Erlaubte Form für Tabellen- und Spaltennamen in zusammengesetztem SQL.
_SQL_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def zeile_aendern(
    db: sqlite3.Connection,
    tabelle: str,
    felder: Mapping[str, object],
    *,
    wo: str,
    wert: object,
    erlaubt: Collection[str],
    zusatz: str = "",
) -> int:
    """``UPDATE tabelle SET a = ?, b = ? WHERE wo = ?`` – gemeinsam für alle Module.

    Spaltennamen kommen nur aus ``erlaubt`` (einer festen Liste im Code); alle Werte gehen
    als Parameter. ``zusatz`` ist ein fester Ausdruck aus dem Code (z. B.
    ``version = version + 1``). Gibt die Anzahl geänderter Zeilen zurück.
    """
    unbekannt = sorted(set(felder) - set(erlaubt))
    if unbekannt:
        raise ValueError(f"Unbekannte Spalten für {tabelle}: {', '.join(unbekannt)}")
    if not felder and not zusatz:
        raise ValueError("Nichts zu ändern")
    for name in (tabelle, wo, *felder):
        if not _SQL_NAME.fullmatch(name):
            raise ValueError(f"Ungültiger Name in SQL: {name!r}")
    zuweisung = ", ".join([f"{name} = ?" for name in felder] + ([zusatz] if zusatz else []))
    sql = f"UPDATE {tabelle} SET {zuweisung} WHERE {wo} = ?"  # noqa: S608 – Namen geprüft
    return db.execute(sql, (*felder.values(), wert)).rowcount


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
