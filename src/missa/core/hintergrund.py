"""Hintergrund-Aufträge: Routinen und Nachtschicht.

Lange Arbeiten (das Modell läuft auf der CPU) sollen den Chat nicht blockieren.
Darum stehen solche Aufträge in der Tabelle ``hintergrund_auftraege`` und werden
von einem eigenen Hintergrund-Task **einer nach dem anderen** an Missa übergeben.

- ``sofort``: läuft, sobald der Task frei ist – z. B. eine fällige Routine. Das
  Ergebnis geht über den Zeitplaner (mit Wiederholversuchen) an Louis.
- ``nacht``: läuft nur im Nachtfenster (Standard 01:00–06:00, im Gespräch
  änderbar). Das Ergebnis wird gespeichert, am Ende des Fensters als
  Morgenbericht geschickt und im nächsten Gespräch erwähnt.

Aufträge überleben Neustarts: Ein unterbrochener Auftrag läuft nach dem Start
erneut (höchstens :data:`MAX_VERSUCHE` Mal). Fällige Routinen kommen vom
:class:`~missa.core.zeitplaner.Zeitplaner` – alle nutzen denselben Zeitplaner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.vorgaben import Vorgaben
from missa.core.werkzeuge import WerkzeugFehler, im_hintergrund
from missa.core.zeitangaben import wandzeit
from missa.core.zeitplaner import Erinnerung, Zeitplaner

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hintergrund_auftraege (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titel TEXT NOT NULL,
    anweisung TEXT NOT NULL,
    art TEXT NOT NULL CHECK (art IN ('sofort', 'nacht')),
    herkunft TEXT NOT NULL DEFAULT 'chat',
    routine_id INTEGER,
    status TEXT NOT NULL DEFAULT 'wartet'
        CHECK (status IN ('wartet', 'laeuft', 'fertig', 'fehler', 'geloescht')),
    erstellt TEXT NOT NULL,
    gestartet TEXT,
    beendet TEXT,
    ergebnis TEXT,
    fehler TEXT,
    versuche INTEGER NOT NULL DEFAULT 0,
    naechster_versuch TEXT,
    zustellen INTEGER NOT NULL DEFAULT 0,
    verspaetet INTEGER NOT NULL DEFAULT 0,
    gemeldet TEXT,
    erwaehnt TEXT
);
CREATE INDEX IF NOT EXISTS hintergrund_status ON hintergrund_auftraege (status, art);
"""

#: So oft wird ein Auftrag höchstens versucht (Fehler oder Neustart mittendrin).
MAX_VERSUCHE = 3
#: Wartezeit vor einem neuen Versuch (wird mit der Zahl der Versuche multipliziert).
WARTEN_NACH_FEHLER = timedelta(minutes=5)
#: Längster Schlaf am Stück (Sekunden).
MAX_SCHLAF = 30.0
#: Ergebnisse so lange im nächsten Gespräch erwähnen.
ERWAEHNEN_TAGE = 3
#: Länge eines Ergebnisses im Morgenbericht und im Gesprächskontext.
KURZ = 700

NACHT_VON = "nachtschicht.von"
NACHT_BIS = "nachtschicht.bis"
BERICHT_AUFTRAG = "nachtschicht-bericht"

#: Übergibt einen Auftrag an Missa und liefert ihre fertige Antwort.
Ausfuehrer = Callable[["HintergrundAuftrag"], Awaitable[str]]


def _utc_text(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _aus_utc(text: str | None, zone: ZoneInfo) -> datetime | None:
    return datetime.fromisoformat(text).astimezone(zone) if text else None


def kuerzen(text: str, laenge: int = KURZ) -> str:
    text = text.strip()
    return text if len(text) <= laenge else text[: laenge - 1].rstrip() + "…"


def im_fenster(jetzt: datetime, von: time, bis: time) -> bool:
    """Liegt die Wanduhrzeit von ``jetzt`` im Fenster ``von``–``bis`` (auch über Mitternacht)?"""
    uhr = jetzt.time().replace(tzinfo=None)
    if von < bis:
        return von <= uhr < bis
    return uhr >= von or uhr < bis


def naechster_beginn(jetzt: datetime, von: time, bis: time) -> datetime:
    """Beginn des laufenden oder nächsten Fensters (Sommer-/Winterzeit beachtet)."""
    if im_fenster(jetzt, von, bis):
        return jetzt
    zone = jetzt.tzinfo
    assert isinstance(zone, ZoneInfo)
    heute = wandzeit(jetzt.date(), von, zone)
    return heute if heute > jetzt else wandzeit(jetzt.date() + timedelta(days=1), von, zone)


@dataclass(frozen=True)
class HintergrundAuftrag:
    id: int
    titel: str
    anweisung: str
    art: str
    herkunft: str
    routine_id: int | None
    status: str
    erstellt: datetime
    gestartet: datetime | None
    beendet: datetime | None
    ergebnis: str | None
    fehler: str | None
    versuche: int
    zustellen: bool
    verspaetet: bool
    gemeldet: datetime | None
    erwaehnt: datetime | None

    def auftrag_fuer_missa(self) -> str:
        """Die Nachricht, mit der der Auftrag an Missa geht."""
        grenzen = (
            "Lösche, verschiebe und sende nichts und lege keine Routinen oder Nachtaufgaben an "
            "– dafür brauchst du Louis' ausdrückliches Ja. Schlag so etwas stattdessen vor."
        )
        if self.art == "nacht":
            return (
                f"(Interner Auftrag aus deiner Nachtschicht: „{self.titel}“. Louis schläft und "
                "kann nicht antworten. Erledige die Aufgabe gründlich und selbstständig mit "
                f"deinen Werkzeugen. {grenzen} Antworte mit dem fertigen Ergebnis – Louis liest "
                f"es morgen früh.)\n\nAufgabe: {self.anweisung}"
            )
        spaet = " (verspätet, Missa-Core war zur geplanten Zeit nicht erreichbar)"
        return (
            f"(Interner Auftrag: Deine Routine „{self.titel}“ ist fällig"
            f"{spaet if self.verspaetet else ''}. Louis ist gerade nicht im Gespräch und kann "
            f"nicht antworten. Erledige die Anweisung selbstständig mit deinen Werkzeugen. "
            f"{grenzen} Deine Antwort geht direkt als Nachricht an Louis.)\n\n"
            f"Anweisung: {self.anweisung}"
        )

    def zeile(self) -> str:
        teile = f"- #{self.id} „{self.titel}“ – "
        if self.status == "wartet":
            teile += "wartet" + (f" (Versuch {self.versuche + 1})" if self.versuche else "")
        elif self.status == "laeuft":
            teile += "läuft gerade"
        elif self.status == "fertig" and self.beendet:
            teile += f"erledigt am {self.beendet:%d.%m. um %H:%M} Uhr"
        elif self.status == "fehler":
            teile += f"fehlgeschlagen ({self.fehler or 'unbekannter Fehler'})"
        if self.status == "fertig" and self.ergebnis:
            teile += f"\n  Ergebnis: {kuerzen(self.ergebnis, 200)}"
        return teile

    def als_dict(self) -> dict[str, Any]:
        def iso(wert: datetime | None) -> str | None:
            return wert.isoformat(timespec="seconds") if wert else None

        return {
            "id": self.id,
            "titel": self.titel,
            "anweisung": self.anweisung,
            "art": self.art,
            "herkunft": self.herkunft,
            "routine_id": self.routine_id,
            "status": self.status,
            "erstellt": iso(self.erstellt),
            "gestartet": iso(self.gestartet),
            "beendet": iso(self.beendet),
            "ergebnis": self.ergebnis,
            "fehler": self.fehler,
            "versuche": self.versuche,
            "gemeldet": iso(self.gemeldet),
        }


class Hintergrund:
    """Warteschlange für Routinen und Nachtaufgaben mit eigenem Hintergrund-Task."""

    def __init__(
        self,
        datei: Path,
        zone: ZoneInfo,
        zeitplaner: Zeitplaner,
        vorgaben: Vorgaben | None = None,
        nacht_von: time = time(1, 0),
        nacht_bis: time = time(6, 0),
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.zone = zone
        self.zeitplaner = zeitplaner
        self.vorgaben = vorgaben or Vorgaben(datei)
        self.standard_von = nacht_von
        self.standard_bis = nacht_bis
        self._uhr = uhr or zeitplaner.jetzt
        #: Übergibt Aufträge an Missa (setzt die App: ChatDienst.hintergrund_ausfuehren).
        self.ausfuehrer: Ausfuehrer | None = None
        #: Eigene Ausführung je Herkunft (z. B. „zusammenfassung“ für lange Dokumente).
        self.handler: dict[str, Ausfuehrer] = {}
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wecker: asyncio.Event | None = None
        self._sperre = threading.Lock()
        with verbinden(datei) as db:
            db.executescript(SCHEMA)
        zeitplaner.routine_ausloeser = self.routine_einreihen
        self._bericht_planen()

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Nachtfenster

    def fenster(self) -> tuple[time, time]:
        return (
            self.vorgaben.uhrzeit(NACHT_VON, self.standard_von),
            self.vorgaben.uhrzeit(NACHT_BIS, self.standard_bis),
        )

    def fenster_text(self) -> str:
        von, bis = self.fenster()
        return f"{von:%H:%M}–{bis:%H:%M} Uhr"

    def im_fenster(self, jetzt: datetime | None = None) -> bool:
        return im_fenster(jetzt or self.jetzt(), *self.fenster())

    def naechster_fensterbeginn(self) -> datetime:
        return naechster_beginn(self.jetzt(), *self.fenster())

    def fenster_einstellen(self, von: time, bis: time) -> None:
        if von == bis:
            raise WerkzeugFehler("Beginn und Ende der Nachtschicht dürfen nicht gleich sein.")
        self.vorgaben.setzen(NACHT_VON, f"{von:%H:%M}")
        self.vorgaben.setzen(NACHT_BIS, f"{bis:%H:%M}")
        self._bericht_planen()
        self.wecken()

    def _bericht_planen(self) -> None:
        """Der Morgenbericht läuft täglich am Ende des Nachtfensters."""
        self.zeitplaner.taeglich(BERICHT_AUFTRAG, self.fenster()[1], self.morgenbericht)

    # -------------------------------------------------------------- Speichern

    def _auftrag(self, zeile: sqlite3.Row) -> HintergrundAuftrag:
        z = self.zone
        return HintergrundAuftrag(
            id=zeile["id"],
            titel=zeile["titel"],
            anweisung=zeile["anweisung"],
            art=zeile["art"],
            herkunft=zeile["herkunft"],
            routine_id=zeile["routine_id"],
            status=zeile["status"],
            erstellt=_aus_utc(zeile["erstellt"], z),  # type: ignore[arg-type]
            gestartet=_aus_utc(zeile["gestartet"], z),
            beendet=_aus_utc(zeile["beendet"], z),
            ergebnis=zeile["ergebnis"],
            fehler=zeile["fehler"],
            versuche=zeile["versuche"],
            zustellen=bool(zeile["zustellen"]),
            verspaetet=bool(zeile["verspaetet"]),
            gemeldet=_aus_utc(zeile["gemeldet"], z),
            erwaehnt=_aus_utc(zeile["erwaehnt"], z),
        )

    def einreihen(
        self,
        titel: str,
        anweisung: str,
        art: str = "nacht",
        herkunft: str = "chat",
        routine_id: int | None = None,
        zustellen: bool = False,
        verspaetet: bool = False,
    ) -> HintergrundAuftrag:
        titel = " ".join(str(titel).split())
        anweisung = str(anweisung).strip()
        if not anweisung:
            raise WerkzeugFehler("Was soll ich erledigen? Die Anweisung fehlt.")
        titel = titel or kuerzen(" ".join(anweisung.split()), 60)
        with self._sperre, verbinden(self.datei) as db:
            cursor = db.execute(
                """
                INSERT INTO hintergrund_auftraege
                    (titel, anweisung, art, herkunft, routine_id, erstellt, zustellen, verspaetet)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    titel,
                    anweisung,
                    art,
                    herkunft,
                    routine_id,
                    _utc_text(self.jetzt()),
                    int(zustellen),
                    int(verspaetet),
                ),
            )
            neu = db.execute(
                "SELECT * FROM hintergrund_auftraege WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        auftrag = self._auftrag(neu)
        log.info("Hintergrund-Auftrag #%s (%s) eingereiht: „%s“", auftrag.id, art, titel)
        self.wecken()
        return auftrag

    def routine_einreihen(self, routine: Erinnerung, verspaetet: bool = False) -> None:
        """Vom Zeitplaner aufgerufen, wenn eine Routine fällig ist."""
        with verbinden(self.datei) as db:
            wartet = db.execute(
                "SELECT id FROM hintergrund_auftraege WHERE routine_id = ? AND status = 'wartet'",
                (routine.id,),
            ).fetchone()
        if wartet:
            log.warning(
                "Routine #%s: der letzte Lauf (#%s) wartet noch – kein zweiter Lauf",
                routine.id,
                wartet["id"],
            )
            return
        self.einreihen(
            routine.text,
            routine.anweisung or routine.text,
            art="sofort",
            herkunft="routine",
            routine_id=routine.id,
            zustellen=True,
            verspaetet=verspaetet,
        )

    def holen(self, auftrag_id: int) -> HintergrundAuftrag | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM hintergrund_auftraege WHERE id = ? AND status != 'geloescht'",
                (auftrag_id,),
            ).fetchone()
        return self._auftrag(zeile) if zeile else None

    def liste(self, art: str = "nacht", erledigte_tage: int = 7) -> list[HintergrundAuftrag]:
        """Wartende und laufende Aufträge sowie die der letzten ``erledigte_tage`` Tage."""
        grenze = _utc_text(self.jetzt() - timedelta(days=erledigte_tage))
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                """
                SELECT * FROM hintergrund_auftraege
                WHERE art = ? AND (status IN ('wartet', 'laeuft')
                    OR (status IN ('fertig', 'fehler') AND beendet >= ?))
                ORDER BY id
                """,
                (art, grenze),
            ).fetchall()
        return [self._auftrag(z) for z in zeilen]

    def anzahl_wartend(self) -> int:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT COUNT(*) AS n FROM hintergrund_auftraege WHERE status = 'wartet'"
            ).fetchone()
        return int(zeile["n"])

    def loeschen(self, auftrag_id: int) -> HintergrundAuftrag:
        auftrag = self.holen(auftrag_id)
        if auftrag is None:
            raise WerkzeugFehler(f"Einen Auftrag #{auftrag_id} gibt es nicht (mehr).")
        if auftrag.status == "laeuft":
            raise WerkzeugFehler(
                f"Auftrag #{auftrag_id} läuft gerade – ich kann ihn erst danach löschen."
            )
        with self._sperre, verbinden(self.datei) as db:
            db.execute(
                "UPDATE hintergrund_auftraege SET status = 'geloescht' WHERE id = ?",
                (auftrag_id,),
            )
        log.info("Hintergrund-Auftrag #%s gelöscht: „%s“", auftrag.id, auftrag.titel)
        return auftrag

    def _setzen(self, auftrag_id: int, **felder: Any) -> None:
        zuweisung = ", ".join(f"{name} = ?" for name in felder)
        with self._sperre, verbinden(self.datei) as db:
            db.execute(
                f"UPDATE hintergrund_auftraege SET {zuweisung} WHERE id = ?",
                (*felder.values(), auftrag_id),
            )

    # -------------------------------------------------------------- Ausführen

    def naechster(self, jetzt: datetime | None = None) -> HintergrundAuftrag | None:
        """Der nächste Auftrag, der jetzt laufen darf (``sofort`` vor ``nacht``)."""
        jetzt = jetzt or self.jetzt()
        nacht = self.im_fenster(jetzt)
        with verbinden(self.datei) as db:
            zeile = db.execute(
                """
                SELECT * FROM hintergrund_auftraege
                WHERE status = 'wartet' AND COALESCE(naechster_versuch, erstellt) <= ?
                    AND (art = 'sofort' OR ?)
                ORDER BY art = 'nacht', id LIMIT 1
                """,
                (_utc_text(jetzt), int(nacht)),
            ).fetchone()
        return self._auftrag(zeile) if zeile else None

    async def ausfuehren(self, auftrag: HintergrundAuftrag) -> HintergrundAuftrag:
        """Übergibt einen Auftrag an Missa und speichert das Ergebnis."""
        versuche = auftrag.versuche + 1
        self._setzen(
            auftrag.id,
            status="laeuft",
            gestartet=_utc_text(self.jetzt()),
            versuche=versuche,
            naechster_versuch=None,
        )
        log.info("Hintergrund-Auftrag #%s startet (Versuch %d)", auftrag.id, versuche)
        try:
            ausfuehrer = self.handler.get(auftrag.herkunft, self.ausfuehrer)
            if ausfuehrer is None:
                raise RuntimeError("keine Ausführung eingerichtet")
            marke = im_hintergrund.set(True)
            try:
                ergebnis = (await ausfuehrer(auftrag)).strip()
            finally:
                im_hintergrund.reset(marke)
            if not ergebnis:
                raise RuntimeError("Missa hat keine Antwort geliefert")
        except asyncio.CancelledError:
            # Dienst wird beendet: beim nächsten Start noch einmal.
            self._setzen(auftrag.id, status="wartet")
            raise
        except Exception as fehler:
            grund = str(fehler) or type(fehler).__name__
            jetzt = self.jetzt()
            if versuche >= MAX_VERSUCHE:
                log.error("Hintergrund-Auftrag #%s endgültig fehlgeschlagen: %s", auftrag.id, grund)
                self._setzen(auftrag.id, status="fehler", fehler=grund, beendet=_utc_text(jetzt))
            else:
                warte = WARTEN_NACH_FEHLER * versuche
                log.warning(
                    "Hintergrund-Auftrag #%s fehlgeschlagen (%s) – neuer Versuch in %s",
                    auftrag.id,
                    grund,
                    warte,
                )
                self._setzen(
                    auftrag.id,
                    status="wartet",
                    fehler=grund,
                    naechster_versuch=_utc_text(jetzt + warte),
                )
        else:
            log.info("Hintergrund-Auftrag #%s fertig (%d Zeichen)", auftrag.id, len(ergebnis))
            self._setzen(
                auftrag.id,
                status="fertig",
                ergebnis=ergebnis,
                fehler=None,
                beendet=_utc_text(self.jetzt()),
            )
        neu = self.holen(auftrag.id)
        assert neu is not None
        if neu.status in ("fertig", "fehler"):
            self._abschliessen(neu)
        return neu

    def _abschliessen(self, auftrag: HintergrundAuftrag) -> None:
        """Ergebnis melden: Routinen sofort, Nachtaufgaben im Morgenbericht."""
        if auftrag.zustellen:
            zeichen = "📄" if auftrag.herkunft == "zusammenfassung" else "🔁"
            if auftrag.status == "fertig":
                text = f"{zeichen} {auftrag.titel}\n\n{auftrag.ergebnis}"
            elif auftrag.herkunft == "zusammenfassung":
                text = (
                    f"{zeichen} „{auftrag.titel}“ konnte ich leider nicht zusammenfassen "
                    f"({auftrag.fehler})."
                )
            else:
                text = (
                    f"{zeichen} Deine Routine „{auftrag.titel}“ konnte ich leider nicht ausführen "
                    f"({auftrag.fehler}). Beim nächsten Mal versuche ich es wieder."
                )
            self.zeitplaner.anlegen(
                auftrag.titel,
                self.jetzt(),
                nachricht=text,
                schluessel=f"hintergrund:{auftrag.id}",
                herkunft=auftrag.herkunft,
            )
            self._setzen(auftrag.id, gemeldet=_utc_text(self.jetzt()))
        elif auftrag.art == "nacht" and not self.im_fenster():
            # Nach dem Fenster fertig geworden: nicht bis morgen warten.
            self.morgenbericht()

    def morgenbericht(self) -> str | None:
        """Schickt alle noch nicht gemeldeten Ergebnisse der Nachtschicht in einer Nachricht."""
        with self._sperre, verbinden(self.datei) as db:
            zeilen = db.execute(
                """
                SELECT * FROM hintergrund_auftraege
                WHERE art = 'nacht' AND status IN ('fertig', 'fehler') AND gemeldet IS NULL
                ORDER BY id
                """
            ).fetchall()
            if not zeilen:
                return None
            db.execute(
                f"UPDATE hintergrund_auftraege SET gemeldet = ? "
                f"WHERE id IN ({','.join('?' * len(zeilen))})",
                (_utc_text(self.jetzt()), *(z["id"] for z in zeilen)),
            )
        auftraege = [self._auftrag(z) for z in zeilen]
        teile = ["🌙 Guten Morgen! Das habe ich heute Nacht erledigt:"]
        for a in auftraege:
            if a.status == "fertig":
                teile.append(f"\n• #{a.id} „{a.titel}“:\n{kuerzen(a.ergebnis or '')}")
            else:
                teile.append(f"\n• #{a.id} „{a.titel}“ hat leider nicht geklappt ({a.fehler}).")
        if any(a.status == "fertig" and len(a.ergebnis or "") > KURZ for a in auftraege):
            teile.append("\nDas ganze Ergebnis bekommst du, wenn du nach der Nummer fragst.")
        text = "\n".join(teile)
        self.zeitplaner.anlegen(
            "Nachtschicht",
            self.jetzt(),
            nachricht=text,
            schluessel="nachtbericht:" + ",".join(str(a.id) for a in auftraege),
            herkunft="nachtschicht",
        )
        log.info("Morgenbericht der Nachtschicht mit %d Auftrag/Aufträgen", len(auftraege))
        return text

    # -------------------------------------------------------------- Gespräch

    def kontext(self, _frage: str) -> str | None:
        """Erwähnt neue Ergebnisse der Nachtschicht einmal im nächsten Gespräch."""
        if im_hintergrund.get():
            return None
        grenze = _utc_text(self.jetzt() - timedelta(days=ERWAEHNEN_TAGE))
        with self._sperre, verbinden(self.datei) as db:
            zeilen = db.execute(
                """
                SELECT * FROM hintergrund_auftraege
                WHERE art = 'nacht' AND status IN ('fertig', 'fehler') AND erwaehnt IS NULL
                    AND beendet >= ?
                ORDER BY id
                """,
                (grenze,),
            ).fetchall()
            if not zeilen:
                return None
            db.execute(
                f"UPDATE hintergrund_auftraege SET erwaehnt = ? "
                f"WHERE id IN ({','.join('?' * len(zeilen))})",
                (_utc_text(self.jetzt()), *(z["id"] for z in zeilen)),
            )
        zeilen_text = []
        for a in (self._auftrag(z) for z in zeilen):
            if a.status == "fertig":
                zeilen_text.append(f"- #{a.id} „{a.titel}“: {kuerzen(a.ergebnis or '')}")
            else:
                zeilen_text.append(f"- #{a.id} „{a.titel}“: fehlgeschlagen ({a.fehler})")
        return (
            "## Aus deiner Nachtschicht\n"
            "Das hast du in der Nacht für Louis erledigt. Erwähne es zu Beginn deiner Antwort "
            "kurz von dir aus (Details mit nachtaufgabe_ergebnis):\n" + "\n".join(zeilen_text)
        )

    # -------------------------------------------------------------- Hintergrund-Task

    def wecken(self) -> None:
        if self._loop is None or self._wecker is None or self._loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._wecker.set)

    def _unterbrochene_zuruecksetzen(self) -> None:
        with self._sperre, verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT id, versuche FROM hintergrund_auftraege WHERE status = 'laeuft'"
            ).fetchall()
            for zeile in zeilen:
                if zeile["versuche"] >= MAX_VERSUCHE:
                    db.execute(
                        "UPDATE hintergrund_auftraege SET status = 'fehler', beendet = ?, "
                        "fehler = 'mehrmals durch einen Neustart unterbrochen' WHERE id = ?",
                        (_utc_text(self.jetzt()), zeile["id"]),
                    )
                else:
                    db.execute(
                        "UPDATE hintergrund_auftraege SET status = 'wartet' WHERE id = ?",
                        (zeile["id"],),
                    )
        if zeilen:
            log.warning("%d unterbrochene(r) Hintergrund-Auftrag/Aufträge", len(zeilen))

    async def _schleife(self) -> None:
        assert self._wecker is not None
        while True:
            self._wecker.clear()
            try:
                auftrag = self.naechster()
                if auftrag is not None:
                    await self.ausfuehren(auftrag)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Hintergrund: Fehler in der Schleife")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wecker.wait(), timeout=MAX_SCHLAF)

    def starten(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._unterbrochene_zuruecksetzen()
        self._loop = asyncio.get_running_loop()
        self._wecker = asyncio.Event()
        log.info(
            "Hintergrund-Aufträge laufen, Nachtschicht %s, %d wartend",
            self.fenster_text(),
            sum(
                1 for a in self.liste("nacht", 0) + self.liste("sofort", 0) if a.status == "wartet"
            ),
        )
        self._task = asyncio.create_task(self._schleife(), name="missa-hintergrund")

    async def stoppen(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._loop = None
        self._wecker = None
