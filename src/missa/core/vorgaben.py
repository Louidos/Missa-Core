"""Einstellungen, die Louis im Gespräch ändern kann („Nachtschicht ab 2 Uhr“).

Die Werte stehen in der Datenbank (Tabelle ``vorgaben``) und überleben damit
Neustarts und Updates. Ist ein Wert nicht gesetzt, gilt der Standard aus der
Konfiguration (``/etc/missa/missa.env``).
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path

from missa.core.datenbank import verbinden
from missa.core.werkzeuge import WerkzeugFehler

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vorgaben (
    name TEXT PRIMARY KEY,
    wert TEXT NOT NULL,
    geaendert TEXT NOT NULL
);
"""


def uhrzeit_lesen(text: str) -> time:
    """``6``, ``06:30``, ``6.30``, ``6:30 Uhr`` → :class:`time`."""
    roh = str(text).strip().lower().removeprefix("um ").removesuffix("uhr").strip()
    stunde, _, minute = roh.replace(".", ":").replace("h", ":").partition(":")
    try:
        return time(int(stunde), int(minute or 0))
    except ValueError as fehler:
        raise WerkzeugFehler(f"„{text}“ verstehe ich nicht als Uhrzeit (z. B. 01:00).") from fehler


class Vorgaben:
    """Dauerhaft gespeicherte, im Gespräch änderbare Einstellungen."""

    def __init__(self, datei: Path) -> None:
        self.datei = datei
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def lesen(self, name: str, standard: str) -> str:
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT wert FROM vorgaben WHERE name = ?", (name,)).fetchone()
        return zeile["wert"] if zeile else standard

    def setzen(self, name: str, wert: str) -> None:
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO vorgaben (name, wert, geaendert) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET wert = excluded.wert, "
                "geaendert = excluded.geaendert",
                (name, wert, datetime.now().astimezone().isoformat(timespec="seconds")),
            )
        log.info("Einstellung %s = %s (im Gespräch geändert)", name, wert)

    def uhrzeit(self, name: str, standard: time) -> time:
        text = self.lesen(name, f"{standard:%H:%M}")
        try:
            return uhrzeit_lesen(text)
        except WerkzeugFehler:
            log.warning(
                "Gespeicherte Einstellung %s=%r ist ungültig, nutze %s", name, text, standard
            )
            return standard

    def alle(self) -> dict[str, str]:
        with verbinden(self.datei) as db:
            return {z["name"]: z["wert"] for z in db.execute("SELECT name, wert FROM vorgaben")}
