"""Langzeitgedächtnis: Fakten über Louis in SQLite mit lokaler FTS5-Volltextsuche.

- **Merken** mit Datum (Vorlieben, Entscheidungen, Projekte …). Ein neuer Fakt, der einem
  alten sehr ähnlich ist („Kaffee ohne Zucker“ statt „Kaffee mit Zucker“), *ersetzt* ihn
  statt doppelt geführt zu werden.
- **Korrekturen** („das war falsch, …“) werden als eigene Kategorie gespeichert, ersetzen
  widersprüchliche alte Einträge und kommen bei ähnlichen Fragen bevorzugt in den Prompt.
- **Frühere Gespräche** (Tabelle ``nachrichten``) sind durchsuchbar – auch nach Neustarts.
- **Vergessen** löscht wirklich: Fakten und passende Gesprächsnachrichten, mit
  ``secure_delete``. Nichts davon verlässt den Rechner (keine externen Dienste).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen

log = logging.getLogger(__name__)

KATEGORIEN = ("termine", "vorlieben", "projekte", "entscheidungen", "korrekturen", "sonstiges")
#: So viele Fakten kommen höchstens als Kontext in den Systemprompt.
KONTEXT_ANZAHL = 5
#: Ab dieser Wort-Überschneidung (Jaccard) gilt ein neuer Fakt als neue Fassung eines alten.
AEHNLICH = 0.7
#: So viele frühere Aussagen von Louis kommen höchstens in den Kontext.
GESPRAECH_ANZAHL = 3

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS fakten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kategorie TEXT NOT NULL CHECK (kategorie IN ({", ".join(f"'{k}'" for k in KATEGORIEN)})),
    erstellt TEXT NOT NULL,
    geaendert TEXT,
    vorher TEXT
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
    geaendert: str | None = None

    def zeile(self) -> str:
        datum = f"gemerkt am {self.erstellt[:10]}"
        if self.geaendert:
            datum += f", zuletzt geändert {self.geaendert[:10]}"
        return f"- {self.text} [ID {self.id}, {self.kategorie}, {datum}]"

    def als_dict(self) -> dict[str, Any]:
        return asdict(self)


class Gedaechtnis:
    """Speichert und findet Fakten in ``missa.db`` (Tabelle ``fakten``)."""

    def __init__(self, datei: Path, zone: ZoneInfo) -> None:
        self.datei = datei
        self.zone = zone
        try:
            with verbinden(datei) as db:
                self._migrieren(db)
                db.executescript(SCHEMA)
        except sqlite3.OperationalError as fehler:
            if "fts5" in str(fehler).lower():
                raise RuntimeError(
                    "Das installierte SQLite kann keine FTS5-Volltextsuche – "
                    "für das Gedächtnis wird SQLite mit FTS5 benötigt"
                ) from fehler
            raise

    @staticmethod
    def _migrieren(db: sqlite3.Connection) -> None:
        """Baut ``fakten`` aus älteren Versionen um (neue Kategorien, Spalten); Daten bleiben."""
        zeile = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fakten'"
        ).fetchone()
        if zeile is None or "korrekturen" in zeile["sql"]:
            return
        log.info("Migration: Tabelle fakten bekommt Kategorien und Spalten für Korrekturen")
        spalten = {z["name"] for z in db.execute("PRAGMA table_info(fakten)")}
        db.executescript(
            """
            DROP TRIGGER IF EXISTS fakten_neu;
            DROP TRIGGER IF EXISTS fakten_weg;
            DROP TRIGGER IF EXISTS fakten_geaendert;
            DROP TABLE IF EXISTS fakten_fts;
            ALTER TABLE fakten RENAME TO fakten_alt;
            """
        )
        db.executescript(SCHEMA)
        uebernehmen = ", ".join(s for s in ("id", "text", "kategorie", "erstellt") if s in spalten)
        db.execute(f"INSERT INTO fakten ({uebernehmen}) SELECT {uebernehmen} FROM fakten_alt")
        db.execute("DROP TABLE fakten_alt")

    @staticmethod
    def _fakt(zeile: sqlite3.Row) -> Fakt:
        return Fakt(
            zeile["id"], zeile["text"], zeile["kategorie"], zeile["erstellt"], zeile["geaendert"]
        )

    def aehnlicher(self, text: str, kategorie: str | None = None) -> Fakt | None:
        """Ein gespeicherter Fakt, der fast dasselbe sagt (für Ersetzen statt Doppeln)."""
        neu = _vergleichswoerter(text)
        if not neu:
            return None
        bester: tuple[float, Fakt] | None = None
        for fakt in self.suchen(text, 10):
            if kategorie and fakt.kategorie != kategorie:
                continue
            alt = _vergleichswoerter(fakt.text)
            wert = len(neu & alt) / len(neu | alt) if alt else 0.0
            if wert >= AEHNLICH and (bester is None or wert > bester[0]):
                bester = (wert, fakt)
        return bester[1] if bester else None

    def ersetzen(self, fakt_id: int, text: str, kategorie: str | None = None) -> Fakt:
        alt = self.holen(fakt_id)
        if alt is None:
            raise WerkzeugFehler(f"Keinen Fakt mit der ID {fakt_id} gefunden.")
        text = " ".join(str(text).split())
        jetzt = datetime.now(self.zone).isoformat(timespec="seconds")
        with verbinden(self.datei) as db:
            db.execute(
                "UPDATE fakten SET text = ?, kategorie = ?, geaendert = ?, vorher = ? WHERE id = ?",
                (text, kategorie or alt.kategorie, jetzt, alt.text, fakt_id),
            )
        log.info("Fakt #%s ersetzt", fakt_id)
        neu = self.holen(fakt_id)
        assert neu is not None
        return neu

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
        if (alt := self.aehnlicher(text, kategorie)) is not None:
            return self.ersetzen(alt.id, text, kategorie), False
        with verbinden(self.datei) as db:
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
            db.execute("PRAGMA secure_delete = ON")
            db.execute("DELETE FROM fakten WHERE id = ?", (fakt_id,))
        log.info("Fakt #%s vergessen", fakt_id)

    # -------------------------------------------------------------- Korrekturen

    def korrigieren(
        self, richtig: str, falsch: str | None = None, fakt_id: int | None = None
    ) -> tuple[Fakt, list[Fakt]]:
        """Speichert eine Korrektur und ersetzt widersprüchliche alte Einträge.

        Gibt (Korrektur, ersetzte alte Fakten) zurück. Ein Fakt mit ``fakt_id`` (oder der
        zu ``falsch`` ähnlichste) bekommt den richtigen Text, statt doppelt zu bleiben.
        """
        richtig = " ".join(str(richtig).split())
        if not richtig:
            raise WerkzeugFehler("Was ist richtig? Der Text fehlt.")
        ersetzt: list[Fakt] = []
        ziel = self.holen(fakt_id) if fakt_id is not None else None
        if ziel is None and falsch:
            ziel = self.aehnlicher(falsch) or next(
                (f for f in self.suchen(falsch, 3) if f.kategorie != "korrekturen"), None
            )
        if ziel is not None and ziel.kategorie != "korrekturen":
            ersetzt.append(ziel)
            self.ersetzen(ziel.id, richtig)
        text = f"Korrektur: {richtig}" + (f" (nicht: {' '.join(falsch.split())})" if falsch else "")
        alte = self._alte_korrektur(richtig, falsch)
        if alte is not None:
            korrektur = self.ersetzen(alte.id, text, "korrekturen")
        else:
            korrektur, _ = self.hinzufuegen(text, "korrekturen")
        log.info("Korrektur #%s gespeichert, %d alter Eintrag ersetzt", korrektur.id, len(ersetzt))
        return korrektur, ersetzt

    def _alte_korrektur(self, richtig: str, falsch: str | None) -> Fakt | None:
        """Eine frühere Korrektur zum selben Punkt (wird ersetzt statt doppelt geführt)."""
        neu = _vergleichswoerter(richtig)
        falsch_norm = " ".join(str(falsch or "").split()).casefold()
        for fakt in self.alle("korrekturen"):
            alt_richtig = fakt.text.removeprefix("Korrektur: ").split(" (nicht: ")[0]
            alt = _vergleichswoerter(alt_richtig)
            wert = len(neu & alt) / len(neu | alt) if neu | alt else 0.0
            if wert >= 0.5 or (falsch_norm and alt_richtig.casefold() == falsch_norm):
                return fakt
        return None

    # -------------------------------------------------------------- Gespräche

    def gespraeche_suchen(
        self, text: str, anzahl: int = 10, tage: int = 365, ohne_sitzung: str | None = None
    ) -> list[tuple[str, str]]:
        """Frühere Aussagen von Louis (Datum, Text), die zu ``text`` passen – neueste zuerst."""
        begriffe = suchbegriffe(text)
        if not begriffe:
            return []
        grenze = (datetime.now(self.zone) - timedelta(days=tage)).astimezone(UTC)
        with verbinden(self.datei) as db:
            try:
                zeilen = db.execute(
                    "SELECT sitzung, inhalt, erstellt FROM nachrichten "
                    "WHERE rolle = 'user' AND erstellt >= ? ORDER BY id DESC LIMIT 2000",
                    (grenze.isoformat(timespec="seconds"),),
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # noch kein Verlauf
        treffer = []
        for zeile in zeilen:
            if ohne_sitzung is not None and zeile["sitzung"] == ohne_sitzung:
                continue
            klein = zeile["inhalt"].lower()
            punkte = sum(b in klein for b in begriffe)
            if punkte and punkte >= min(2, len(begriffe)):
                tag = datetime.fromisoformat(zeile["erstellt"]).astimezone(self.zone)
                treffer.append((punkte, f"{tag:%d.%m.%Y}", " ".join(zeile["inhalt"].split())))
        treffer.sort(key=lambda t: -t[0])
        return [(datum, inhalt[:300]) for _, datum, inhalt in treffer[:anzahl]]

    def vergessen_thema(self, thema: str, nur_zaehlen: bool = False) -> tuple[list[Fakt], int]:
        """Löscht alle Fakten und Gesprächsnachrichten zu ``thema`` (oder zählt sie nur)."""
        fakten = [f for f in self.suchen(thema, 50) if _passt(thema, f.text)]
        muster = f"%{' '.join(str(thema).split()).lower()}%"
        with verbinden(self.datei) as db:
            try:
                anzahl = db.execute(
                    "SELECT COUNT(*) AS n FROM nachrichten WHERE lower(inhalt) LIKE ?", (muster,)
                ).fetchone()["n"]
            except sqlite3.OperationalError:
                anzahl = 0
            if nur_zaehlen:
                return fakten, int(anzahl)
            db.execute("PRAGMA secure_delete = ON")
            for fakt in fakten:
                db.execute("DELETE FROM fakten WHERE id = ?", (fakt.id,))
            if anzahl:
                db.execute("DELETE FROM nachrichten WHERE lower(inhalt) LIKE ?", (muster,))
        log.info("Thema vergessen: %d Fakt(en), %d Nachricht(en)", len(fakten), anzahl)
        return fakten, int(anzahl)

    # -------------------------------------------------------------- Kontext

    def kontext(self, frage: str) -> str | None:
        """Block für den Systemprompt: passende Fakten, Korrekturen und frühere Aussagen."""
        from missa.core.werkzeuge import im_hintergrund

        fakten = self.suchen(frage, KONTEXT_ANZAHL + 3)
        korrekturen = [f for f in fakten if f.kategorie == "korrekturen"][:3]
        uebrige = [f for f in fakten if f.kategorie != "korrekturen"][:KONTEXT_ANZAHL]
        bloecke = []
        if korrekturen:
            bloecke.append(
                "## Korrekturen von Louis (unbedingt beachten)\n"
                "Hier hat Louis dich früher korrigiert – antworte in ähnlichen Fällen so:\n"
                + "\n".join(f.zeile() for f in korrekturen)
            )
        if uebrige:
            bloecke.append(
                "## Was du über Louis weißt\n"
                "Aus deinem Gedächtnis (nutze es, wenn es zur Frage passt):\n"
                + "\n".join(f.zeile() for f in uebrige)
            )
        if not im_hintergrund.get():
            frueher = self.gespraeche_suchen(frage, GESPRAECH_ANZAHL, tage=90)
            if frueher:
                bloecke.append(
                    "## Frühere Gespräche (passend zur Frage)\n"
                    + "\n".join(f"- {datum} Louis: „{text}“" for datum, text in frueher)
                )
            if KORREKTUR.search(frage):
                bloecke.append(
                    "## Louis korrigiert dich\n"
                    "Es klingt, als hätte Louis dich gerade korrigiert. Speichere das Richtige mit "
                    "korrigieren (richtig, falsch) – dann machst du es beim nächsten Mal richtig."
                )
            elif MERKENSWERT.search(frage):
                bloecke.append(
                    "## Vielleicht merkenswert\n"
                    "Louis erzählt vielleicht etwas Dauerhaftes (Vorliebe, Entscheidung, Projekt). "
                    "Wenn ja, speichere es mit merken – kurz als Satz in der dritten Person."
                )
        return "\n\n".join(bloecke) or None


KORREKTUR = re.compile(
    r"\b(?:das (?:war|ist|stimmt) (?:falsch|nicht)|stimmt (?:so )?nicht|falsch[,!.]|"
    r"nein,? (?:ich meinte|richtig ist|das heißt|es ist)|korrigier|das ist nicht richtig)",
    re.IGNORECASE,
)
MERKENSWERT = re.compile(
    r"\b(?:ich mag|ich liebe|ich hasse|ich bevorzuge|ich habe mich entschieden|ich hab mich "
    r"entschieden|ab jetzt|ab sofort|mein (?:neues )?projekt|ich arbeite (?:gerade )?an|"
    r"lieblings\w+ ist|ich trinke|ich esse kein)",
    re.IGNORECASE,
)


def _vergleichswoerter(text: str) -> set[str]:
    """Wortstämme plus Zahlen – „Projekt 1“ und „Projekt 7“ sind verschiedene Fakten."""
    return set(suchbegriffe(text)) | set(re.findall(r"\d+", text))


def _passt(thema: str, text: str) -> bool:
    """Gehört ``text`` zum Thema? Alle Suchbegriffe des Themas müssen vorkommen."""
    begriffe = suchbegriffe(thema)
    klein = text.lower()
    return bool(begriffe) and all(b in klein for b in begriffe)


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
        if neu:
            return f"Gemerkt:\n{eintrag.zeile()}"
        if eintrag.geaendert and eintrag.text == " ".join(str(fakt).split()):
            return f"Alten Eintrag ersetzt statt doppelt gespeichert:\n{eintrag.zeile()}"
        return f"Das weißt du schon:\n{eintrag.zeile()}"

    def korrigieren(
        richtig: str, falsch: str | None = None, fakt_id: int | str | None = None
    ) -> str:
        nummer = _id_lesen(fakt_id) if fakt_id not in (None, "") else None
        korrektur, ersetzt = gedaechtnis.korrigieren(richtig, falsch, nummer)
        text = f"Korrektur gespeichert:\n{korrektur.zeile()}"
        if ersetzt:
            text += "\nErsetzt (nicht mehr doppelt): " + "; ".join(f"„{f.text}“" for f in ersetzt)
        return text + "\nSag Louis kurz, dass du es dir gemerkt hast."

    def frueher_gesagt(suche: str) -> str:
        treffer = gedaechtnis.gespraeche_suchen(suche)
        if not treffer:
            return f"In früheren Gesprächen finde ich nichts zu „{suche}“."
        return f"Das hat Louis früher zu „{suche}“ gesagt:\n" + "\n".join(
            f"- {datum}: „{text}“" for datum, text in treffer
        )

    def erinnern(suche: str) -> str:
        fakten = gedaechtnis.suchen(suche, 10)
        if not fakten:
            return f"Zu „{suche}“ ist nichts gespeichert."
        return f"Gespeichert zu „{suche}“:\n" + "\n".join(f.zeile() for f in fakten)

    def vergessen(
        fakt_id: int | str | None = None, thema: str | None = None, bestaetigt: bool = False
    ) -> str:
        if thema and fakt_id in (None, ""):
            fakten, nachrichten = gedaechtnis.vergessen_thema(thema, nur_zaehlen=True)
            if not fakten and not nachrichten:
                return f"Zu „{thema}“ ist nichts gespeichert – da gibt es nichts zu vergessen."
            beschreibung = (
                f"Alles zu „{thema}“: {len(fakten)} Fakt(en) und {nachrichten} Nachricht(en) aus "
                "früheren Gesprächen werden endgültig gelöscht.\n"
                + "\n".join(f.zeile() for f in fakten)
            )
            schluessel = f"thema:{thema.casefold()}"
            sperre = loeschen_pruefen(
                bestaetigungen,
                schluessel,
                bool(bestaetigt),
                beschreibung,
                "vergessen",
                f"thema={thema}",
            )
            if sperre:
                return sperre
            fakten, nachrichten = gedaechtnis.vergessen_thema(thema)
            bestaetigungen.erledigt(schluessel)
            return (
                f"Vergessen: {len(fakten)} Fakt(en) und {nachrichten} Nachricht(en) zu „{thema}“ "
                "– das ist endgültig gelöscht."
            )
        if fakt_id in (None, ""):
            raise WerkzeugFehler("Was soll ich vergessen? fakt_id oder thema angeben.")
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
            "Merkt sich einen wichtigen Fakt über Louis dauerhaft mit Datum (Vorlieben, "
            "Entscheidungen, Projekte – über Gespräche und Neustarts hinweg). Formuliere ihn als "
            "vollständigen Satz in der dritten Person, z. B. „Louis trinkt Kaffee ohne Zucker“. "
            "Ein sehr ähnlicher alter Eintrag wird ersetzt.",
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
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "erinnern",
            "Sucht im Gedächtnis nach gespeicherten Fakten über Louis („Was weißt du über …?“).",
            {
                "type": "object",
                "properties": {"suche": {"type": "string", "description": "Suchbegriffe"}},
                "required": ["suche"],
            },
            erinnern,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "korrigieren",
            "Lernt aus einer Korrektur („das war falsch, …“): speichert, was richtig ist, und "
            "ersetzt den falschen alten Eintrag, statt ihn doppelt zu führen. Bei ähnlichen "
            "Fragen bekommst du die Korrektur wieder angezeigt.",
            {
                "type": "object",
                "properties": {
                    "richtig": {"type": "string", "description": "Was richtig ist, als Satz"},
                    "falsch": {"type": "string", "description": "Was falsch war, optional"},
                    "fakt_id": {
                        "type": "integer",
                        "description": "ID des falschen Fakts, falls bekannt",
                    },
                },
                "required": ["richtig"],
            },
            korrigieren,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "frueher_gesagt",
            "Durchsucht frühere Gespräche nach dem, was Louis gesagt hat (mit Datum) – "
            "z. B. „Was hab ich dir letzte Woche über den Server erzählt?“.",
            {
                "type": "object",
                "properties": {"suche": {"type": "string", "description": "Suchbegriffe"}},
                "required": ["suche"],
            },
            frueher_gesagt,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "vergessen",
            "Löscht endgültig: einen Fakt (fakt_id) oder alles zu einem Thema (thema, auch "
            "aus früheren Gesprächen) – „Vergiss das mit dem Kaffee“. Läuft erst nach Louis' "
            "ausdrücklichem Ja.",
            {
                "type": "object",
                "properties": {
                    "fakt_id": {"type": "integer", "description": "ID des Fakts"},
                    "thema": {"type": "string", "description": "Oder: Thema, z. B. Kaffee"},
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true, wenn Louis das Vergessen bestätigt hat",
                    },
                },
            },
            vergessen,
            stufe=Stufe.ZERSTOERERISCH,
        ),
    ]
