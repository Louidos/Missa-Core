"""Farben für Termine und Kalender – und Missas Farbsystem.

- Termine bekommen die iCalendar-Eigenschaft ``COLOR`` (RFC 7986). Erlaubt ist
  dort ein CSS3-Farbname (z. B. ``red``); so zeigt es auch die Nextcloud-Kalender-App an.
- Kalender bekommen ``calendar-color`` als Hex-Wert (z. B. ``#E53935``).

Louis darf deutsche Namen („rot“, „hellblau“), CSS-Namen oder Hex-Werte nennen.

Das **Farbsystem** ordnet Kategorien Farben zu (Arbeit = blau, Lehre = grün …).
Hat ein neuer Termin eine Kategorie aus dem Farbsystem und keine eigene Farbe,
bekommt er automatisch deren Farbe. Missa darf das Farbsystem selbst anpassen.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from missa.core.datenbank import verbinden
from missa.core.werkzeuge import WerkzeugFehler

#: CSS3-Name → Hex (Auswahl gut unterscheidbarer Farben).
CSS_FARBEN: dict[str, str] = {
    "red": "#FF0000",
    "darkred": "#8B0000",
    "crimson": "#DC143C",
    "tomato": "#FF6347",
    "orange": "#FFA500",
    "darkorange": "#FF8C00",
    "gold": "#FFD700",
    "yellow": "#FFFF00",
    "khaki": "#F0E68C",
    "green": "#008000",
    "limegreen": "#32CD32",
    "lightgreen": "#90EE90",
    "darkgreen": "#006400",
    "olive": "#808000",
    "turquoise": "#40E0D0",
    "teal": "#008080",
    "cyan": "#00FFFF",
    "skyblue": "#87CEEB",
    "lightblue": "#ADD8E6",
    "blue": "#0000FF",
    "royalblue": "#4169E1",
    "navy": "#000080",
    "purple": "#800080",
    "violet": "#EE82EE",
    "magenta": "#FF00FF",
    "hotpink": "#FF69B4",
    "pink": "#FFC0CB",
    "brown": "#A52A2A",
    "chocolate": "#D2691E",
    "beige": "#F5F5DC",
    "gray": "#808080",
    "lightgray": "#D3D3D3",
    "darkgray": "#A9A9A9",
    "black": "#000000",
    "white": "#FFFFFF",
}

#: Deutsche Namen → CSS3-Name.
DEUTSCH: dict[str, str] = {
    "rot": "red",
    "dunkelrot": "darkred",
    "weinrot": "darkred",
    "karminrot": "crimson",
    "hellrot": "tomato",
    "orange": "orange",
    "dunkelorange": "darkorange",
    "gelb": "gold",
    "hellgelb": "yellow",
    "grün": "green",
    "gruen": "green",
    "hellgrün": "lightgreen",
    "hellgruen": "lightgreen",
    "dunkelgrün": "darkgreen",
    "dunkelgruen": "darkgreen",
    "olivgrün": "olive",
    "oliv": "olive",
    "limette": "limegreen",
    "türkis": "turquoise",
    "tuerkis": "turquoise",
    "petrol": "teal",
    "cyan": "cyan",
    "himmelblau": "skyblue",
    "hellblau": "lightblue",
    "blau": "royalblue",
    "dunkelblau": "navy",
    "marineblau": "navy",
    "lila": "purple",
    "violett": "violet",
    "flieder": "violet",
    "magenta": "magenta",
    "pink": "hotpink",
    "rosa": "pink",
    "braun": "brown",
    "hellbraun": "chocolate",
    "beige": "beige",
    "grau": "gray",
    "hellgrau": "lightgray",
    "dunkelgrau": "darkgray",
    "schwarz": "black",
    "weiß": "white",
    "weiss": "white",
}

#: Missas Standard-Farbsystem (Kategorie → deutscher Farbname).
STANDARD_FARBSYSTEM: dict[str, str] = {
    "Arbeit": "blau",
    "Lehre": "grün",
    "Privat": "lila",
    "Server": "grau",
    "Pausen": "türkis",
    "Familie": "orange",
    "Gesundheit": "rot",
    "Sport": "hellgrün",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kalender_farbsystem (
    kategorie TEXT PRIMARY KEY COLLATE NOCASE,
    farbe TEXT NOT NULL
);
"""


def _hex_normal(text: str) -> str | None:
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", text):
        return "#" + text.lstrip("#").upper()
    if re.fullmatch(r"#?[0-9a-fA-F]{3}", text):
        kurz = text.lstrip("#")
        return "#" + "".join(z * 2 for z in kurz).upper()
    return None


def _naechste_css(hex_wert: str) -> str:
    def rgb(wert: str) -> tuple[int, int, int]:
        return int(wert[1:3], 16), int(wert[3:5], 16), int(wert[5:7], 16)

    ziel = rgb(hex_wert)
    return min(
        CSS_FARBEN,
        key=lambda name: sum(
            (a - b) ** 2 for a, b in zip(rgb(CSS_FARBEN[name]), ziel, strict=True)
        ),
    )


def farbe_lesen(text: str) -> tuple[str, str]:
    """Liest eine Farbangabe und gibt ``(css_name, hex)`` zurück."""
    roh = str(text or "").strip().casefold()
    if not roh:
        raise WerkzeugFehler("Es fehlt die Farbe.")
    if roh in DEUTSCH:
        css = DEUTSCH[roh]
        return css, CSS_FARBEN[css]
    if roh in CSS_FARBEN:
        return roh, CSS_FARBEN[roh]
    if hex_wert := _hex_normal(roh):
        return _naechste_css(hex_wert), hex_wert
    raise WerkzeugFehler(
        f"Die Farbe „{text}“ kenne ich nicht. Möglich sind z. B. "
        + ", ".join(sorted(k for k in DEUTSCH if "ue" not in k and "ss" not in k)[:25])
        + " oder ein Hex-Wert wie #E53935."
    )


def farbe_name(css_oder_hex: str | None) -> str:
    """Deutscher Name für die Anzeige (oder der Wert selbst)."""
    if not css_oder_hex:
        return ""
    wert = str(css_oder_hex).strip()
    roh = wert.casefold()
    if hex_wert := _hex_normal(roh):
        roh = next((n for n, h in CSS_FARBEN.items() if h == hex_wert), "")
        if not roh:
            return hex_wert
    for deutsch, css in DEUTSCH.items():
        if css == roh and "ue" not in deutsch and "ss" not in deutsch:
            return deutsch
    return wert


class Farbsystem:
    """Kategorie → Farbe, gespeichert in ``missa.db``."""

    def __init__(self, datei: Path) -> None:
        self.datei = datei
        self._bereit = False

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """Verbindung; legt Tabelle und Standard-Farbsystem beim ersten Zugriff an."""
        with verbinden(self.datei) as db:
            if not self._bereit:
                db.executescript(SCHEMA)
                if db.execute("SELECT COUNT(*) FROM kalender_farbsystem").fetchone()[0] == 0:
                    db.executemany(
                        "INSERT INTO kalender_farbsystem (kategorie, farbe) VALUES (?, ?)",
                        STANDARD_FARBSYSTEM.items(),
                    )
                self._bereit = True
            yield db

    def alle(self) -> dict[str, str]:
        with self._db() as db:
            zeilen = db.execute(
                "SELECT kategorie, farbe FROM kalender_farbsystem ORDER BY kategorie"
            ).fetchall()
        return {z["kategorie"]: z["farbe"] for z in zeilen}

    def farbe_fuer(self, kategorien: list[str]) -> str | None:
        """Farbe der ersten Kategorie, die im Farbsystem steht (als Eingabe für farbe_lesen)."""
        system = {k.casefold(): v for k, v in self.alle().items()}
        for kategorie in kategorien:
            if farbe := system.get(kategorie.strip().casefold()):
                return farbe
        return None

    def festlegen(self, kategorie: str, farbe: str | None) -> None:
        kategorie = " ".join(str(kategorie).split())
        if not kategorie:
            raise WerkzeugFehler("Es fehlt die Kategorie.")
        with self._db() as db:
            if farbe:
                farbe_lesen(farbe)  # prüfen
                db.execute(
                    "INSERT INTO kalender_farbsystem (kategorie, farbe) VALUES (?, ?) "
                    "ON CONFLICT (kategorie) DO UPDATE SET farbe = excluded.farbe",
                    (kategorie, farbe.strip()),
                )
            else:
                db.execute("DELETE FROM kalender_farbsystem WHERE kategorie = ?", (kategorie,))

    def text(self) -> str:
        return ", ".join(f"{k} = {v}" for k, v in self.alle().items()) or "(leer)"
