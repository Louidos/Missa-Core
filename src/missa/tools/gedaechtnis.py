"""Gedächtnis: Fakten über Louis in SQLite mit FTS5-Volltextsuche."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Werkzeug, WerkzeugFehler
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen

KATEGORIEN = ("termine", "vorlieben", "projekte", "sonstiges")
#: So viele Fakten kommen höchstens als Kontext in den Systemprompt.
KONTEXT_ANZAHL = 5

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS fakten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kategorie TEXT NOT NULL CHECK (kategorie IN ({", ".join(f"'{k}'" for k in KATEGORIEN)})),
    erstellt TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fakten_fts USING fts5(
    text,
    content='fakten',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS fakten_neu AFTER INSERT ON fakten BEGIN
    INSERT INTO fakten_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS fakten_weg AFTER DELETE ON fakten BEGIN
    INSERT INTO fakten_fts (fakten_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS fakten_geaendert AFTER UPDATE ON fakten BEGIN
    INSERT INTO fakten_fts (fakten_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO fakten_fts (rowid, text) VALUES (new.id, new.text);
END;
"""

#: Häufige Wörter, die bei der Suche nichts bringen.
STOPPWOERTER = frozenset(
    """
    aber alle als also am an auch auf aus bei bin bis bist da damit dann das dass dein deine
    dem den denn der des dich die dir doch du ein eine einem einen einer es etwas für hab habe
    hast hat hatte ich ihr im in ist ja jetzt kann kannst mal man mein meine meinem meinen
    meiner mich mir mit muss nach nein nicht noch nun nur ob oder sein sich sie sind so soll
    über um und uns unser vom von vor war was weißt welche wenn wer wie wieder wir wird wo zu
    zum zur missa louis merk merke merken dir bitte weiß erinner erinnere
    """.split()  # noqa: SIM905 – als Wortliste lesbarer
)
#: Einfache deutsche Endungen, damit „trinke“ auch „trinkt“ findet.
ENDUNGEN = ("ungen", "ung", "en", "er", "es", "em", "st", "e", "n", "s", "t")


def suchbegriffe(text: str) -> list[str]:
    """Zerlegt einen Text in Wortstämme für die Volltextsuche."""
    begriffe: list[str] = []
    # lower() statt casefold(): casefold macht aus „ß“ ein „ss“, der FTS-Index nicht.
    for wort in re.findall(r"\w+", text.lower()):
        if len(wort) < 3 or wort in STOPPWOERTER:
            continue
        stamm = wort
        for endung in ENDUNGEN:
            if stamm.endswith(endung) and len(stamm) - len(endung) >= 4:
                stamm = stamm[: -len(endung)]
                break
        if stamm not in begriffe:
            begriffe.append(stamm)
    return begriffe


@dataclass(frozen=True)
class Fakt:
    id: int
    text: str
    kategorie: str
    erstellt: str

    def zeile(self) -> str:
        return f"- {self.text} [ID {self.id}, {self.kategorie}, gemerkt am {self.erstellt[:10]}]"

    def als_dict(self) -> dict[str, Any]:
        return asdict(self)


class Gedaechtnis:
    """Speichert und findet Fakten in ``missa.db`` (Tabelle ``fakten``)."""

    def __init__(self, datei: Path, zone: ZoneInfo) -> None:
        self.datei = datei
        self.zone = zone
        try:
            with verbinden(datei) as db:
                db.executescript(SCHEMA)
        except sqlite3.OperationalError as fehler:
            if "fts5" in str(fehler).lower():
                raise RuntimeError(
                    "Das installierte SQLite kann keine FTS5-Volltextsuche – "
                    "für das Gedächtnis wird SQLite mit FTS5 benötigt"
                ) from fehler
            raise

    @staticmethod
    def _fakt(zeile: sqlite3.Row) -> Fakt:
        return Fakt(zeile["id"], zeile["text"], zeile["kategorie"], zeile["erstellt"])

    def hinzufuegen(self, text: str, kategorie: str) -> tuple[Fakt, bool]:
        """Speichert einen Fakt. Gibt (Fakt, neu?) zurück; Doppelte werden nicht neu angelegt."""
        text = " ".join(str(text).split())
        if not text:
            raise WerkzeugFehler("Der Fakt ist leer.")
        kategorie = str(kategorie or "sonstiges").strip().casefold()
        if kategorie not in KATEGORIEN:
            raise WerkzeugFehler(
                f"Unbekannte Kategorie „{kategorie}“. Erlaubt: {', '.join(KATEGORIEN)}"
            )
        with verbinden(self.datei) as db:
            vorhanden = db.execute(
                "SELECT * FROM fakten WHERE lower(text) = lower(?)", (text,)
            ).fetchone()
            if vorhanden:
                return self._fakt(vorhanden), False
            erstellt = datetime.now(self.zone).isoformat(timespec="seconds")
            cursor = db.execute(
                "INSERT INTO fakten (text, kategorie, erstellt) VALUES (?, ?, ?)",
                (text, kategorie, erstellt),
            )
            return Fakt(int(cursor.lastrowid), text, kategorie, erstellt), True

    def suchen(self, text: str, anzahl: int = KONTEXT_ANZAHL) -> list[Fakt]:
        """Die bis zu ``anzahl`` passendsten Fakten (FTS5, nach bm25 sortiert)."""
        begriffe = suchbegriffe(text)
        if not begriffe:
            return []
        abfrage = " OR ".join(f'"{begriff}"*' for begriff in begriffe)
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                """
                SELECT f.* FROM fakten_fts
                JOIN fakten AS f ON f.id = fakten_fts.rowid
                WHERE fakten_fts MATCH ?
                ORDER BY bm25(fakten_fts), f.id DESC
                LIMIT ?
                """,
                (abfrage, anzahl),
            ).fetchall()
        return [self._fakt(z) for z in zeilen]

    def alle(self, kategorie: str | None = None) -> list[Fakt]:
        with verbinden(self.datei) as db:
            if kategorie:
                zeilen = db.execute(
                    "SELECT * FROM fakten WHERE kategorie = ? ORDER BY id", (kategorie,)
                ).fetchall()
            else:
                zeilen = db.execute("SELECT * FROM fakten ORDER BY id").fetchall()
        return [self._fakt(z) for z in zeilen]

    def holen(self, fakt_id: int) -> Fakt | None:
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT * FROM fakten WHERE id = ?", (fakt_id,)).fetchone()
        return self._fakt(zeile) if zeile else None

    def entfernen(self, fakt_id: int) -> None:
        with verbinden(self.datei) as db:
            db.execute("DELETE FROM fakten WHERE id = ?", (fakt_id,))

    def kontext(self, frage: str) -> str | None:
        """Block für den Systemprompt mit den passendsten Fakten zur letzten Nachricht."""
        fakten = self.suchen(frage, KONTEXT_ANZAHL)
        if not fakten:
            return None
        zeilen = "\n".join(f.zeile() for f in fakten)
        return (
            "## Was du über Louis weißt\n"
            "Aus deinem Gedächtnis (nutze es, wenn es zur Frage passt):\n" + zeilen
        )


def _id_lesen(fakt_id: Any) -> int:
    try:
        return int(str(fakt_id).strip().removeprefix("#"))
    except ValueError as fehler:
        raise WerkzeugFehler(f"fakt_id „{fakt_id}“ ist keine Zahl.") from fehler


def gedaechtnis_werkzeuge(
    gedaechtnis: Gedaechtnis, bestaetigungen: Bestaetigungen
) -> list[Werkzeug]:
    def merken(fakt: str, kategorie: str = "sonstiges") -> str:
        eintrag, neu = gedaechtnis.hinzufuegen(fakt, kategorie)
        if not neu:
            return f"Das weißt du schon:\n{eintrag.zeile()}"
        return f"Gemerkt:\n{eintrag.zeile()}"

    def erinnern(suche: str) -> str:
        fakten = gedaechtnis.suchen(suche, 10)
        if not fakten:
            return f"Zu „{suche}“ ist nichts gespeichert."
        return f"Gespeichert zu „{suche}“:\n" + "\n".join(f.zeile() for f in fakten)

    def vergessen(fakt_id: int | str, bestaetigt: bool = False) -> str:
        nummer = _id_lesen(fakt_id)
        eintrag = gedaechtnis.holen(nummer)
        if eintrag is None:
            raise WerkzeugFehler(f"Keinen Fakt mit der ID {nummer} gefunden.")
        sperre = loeschen_pruefen(
            bestaetigungen,
            f"fakt:{nummer}",
            bool(bestaetigt),
            eintrag.zeile(),
            "vergessen",
            f"fakt_id={nummer}",
        )
        if sperre:
            return sperre
        gedaechtnis.entfernen(nummer)
        bestaetigungen.erledigt(f"fakt:{nummer}")
        return f"Vergessen:\n{eintrag.zeile()}"

    return [
        Werkzeug(
            "merken",
            "Merkt sich einen wichtigen Fakt über Louis dauerhaft (über Gespräche hinweg). "
            "Formuliere ihn als vollständigen Satz in der dritten Person, z. B. "
            "„Louis trinkt Kaffee ohne Zucker“.",
            {
                "type": "object",
                "properties": {
                    "fakt": {"type": "string", "description": "Der Fakt als ganzer Satz"},
                    "kategorie": {
                        "type": "string",
                        "enum": list(KATEGORIEN),
                        "description": "Art des Fakts",
                    },
                },
                "required": ["fakt", "kategorie"],
            },
            merken,
        ),
        Werkzeug(
            "erinnern",
            "Sucht im Gedächtnis nach gespeicherten Fakten über Louis.",
            {
                "type": "object",
                "properties": {"suche": {"type": "string", "description": "Suchbegriffe"}},
                "required": ["suche"],
            },
            erinnern,
        ),
        Werkzeug(
            "vergessen",
            "Löscht einen gespeicherten Fakt. Zuerst ohne bestaetigt aufrufen: dann wird nur "
            "gemeldet, was vergessen würde. Frag Louis und rufe erst nach seinem "
            "ausdrücklichen Ja nochmals mit bestaetigt=true auf.",
            {
                "type": "object",
                "properties": {
                    "fakt_id": {"type": "integer", "description": "ID des Fakts"},
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true, wenn Louis das Vergessen bestätigt hat",
                    },
                },
                "required": ["fakt_id"],
            },
            vergessen,
        ),
    ]
