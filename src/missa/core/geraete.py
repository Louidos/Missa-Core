"""Geräte-Anschluss v1: ein Gerät (z. B. der Laptop) schliesst sich an Missa an.

Das Gerät baut die Verbindung immer selbst auf und meldet sich mit einem **eigenen Token**
an (nicht dem API-Schlüssel). Drei Endpunkte (nur mit ``MISSA_GERAETE_AKTIV=true``):

1. ``POST /api/geraete/anmelden`` – Gerät, Sitzungen und Werkzeuge anmelden.
2. ``GET /api/geraete/auftraege`` – Server-Sent Events: ``auftrag``, ``meldung``, ``abbruch``,
   alle 15 Sekunden ein Kommentar ``: ping``.
3. ``POST /api/geraete/ergebnis`` – Ergebnis eines Auftrags zurückgeben.

Regeln: Bricht der Stream ab, gilt das Gerät nach 30 Sekunden als offline. Ein Auftrag ohne
Ergebnis bis ``frist_s`` gilt als Zeitlimit. Jede Auftrags-ID wird höchstens einmal
ausgeführt. Nach einem Neustart des Dienstes meldet sich das Gerät neu an (die Anmeldung
lebt nur im Speicher, die Tokens in ``missa.db``).

Tokens legt Louis auf ai01 an: ``scripts/einrichten/geraet-anlegen.sh <name>``. Gespeichert
wird nur der SHA-256-Hash; der Vergleich läuft in konstanter Zeit über alle Geräte.

Von Hand (für das Script): ``python -m missa.core.geraete anlegen|sperren|liste``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden

log = logging.getLogger(__name__)

#: Version des Vertrags „Geräte-Anschluss v1“.
PROTOKOLL = 1
#: Alle so viele Sekunden ein ``: ping`` im Stream.
PING_SEKUNDEN = 15.0
#: So lange nach einem Abbruch des Streams gilt das Gerät noch als online.
OFFLINE_NACH_SEKUNDEN = 30.0
#: Höchstlänge eines Ergebnisses (Zeichen); Längeres wird gekürzt.
MAX_ERGEBNIS = 20_000
#: Höchstlänge einer Fehlerbeschreibung des Geräts.
MAX_FEHLER = 500
#: Höchstzahl gemerkter, schon erledigter Auftrags-IDs (gegen doppelte Ergebnisse).
MAX_ERLEDIGT = 5000
#: Höchstzahl angemeldeter Werkzeuge, die überhaupt angeschaut werden.
MAX_ANGEMELDET = 200
MAX_SITZUNGEN = 20
MAX_SITZUNG_LAENGE = 200
#: Erlaubte Gerätenamen: klein, kurz, ohne Leerzeichen.
NAME_MUSTER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOKEN_VORSILBE = "mg_"  # noqa: S105 – Vorsilbe, kein Passwort
ERGEBNIS_ARTEN = ("abgelehnt", "zeitlimit", "fehler")
MELDUNG_STUFEN = ("info", "wichtig", "notfall")

SCHEMA = """
CREATE TABLE IF NOT EXISTS geraete (
    name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    angelegt TEXT NOT NULL,
    gesperrt TEXT
);
"""


class GeraeteFehler(ValueError):
    """Ungültige Angabe (z. B. Name) beim Anlegen oder Anmelden."""


def token_hash(token: str | bytes) -> str:
    """SHA-256 des Tokens (hex). Tokens sind zufällig und lang – ein Salz ist nicht nötig."""
    roh = token.encode() if isinstance(token, str) else token
    return hashlib.sha256(roh.strip()).hexdigest()


def name_pruefen(name: str) -> str:
    name = (name or "").strip()
    if not NAME_MUSTER.fullmatch(name):
        raise GeraeteFehler(
            "Gerätename: 1–32 Zeichen, nur Kleinbuchstaben, Ziffern, - und _ "
            "(erstes Zeichen Buchstabe oder Ziffer), z. B. laptop"
        )
    return name


# ----------------------------------------------------------------------------- Tokens


class GeraeteTokens:
    """Tokens der Geräte in ``missa.db`` (Tabelle ``geraete``) – nur der Hash steht dort."""

    def __init__(self, datei: Path, zone: ZoneInfo | None = None) -> None:
        self.datei = datei
        self.zone = zone
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def _jetzt(self) -> str:
        return datetime.now(self.zone).isoformat(timespec="seconds")

    def anlegen(self, name: str) -> str:
        """Legt ein Gerät an und gibt den Token zurück (nur dieses eine Mal sichtbar).

        Ein gesperrtes Gerät bekommt so einen neuen Token und ist wieder frei.
        """
        name = name_pruefen(name)
        token = TOKEN_VORSILBE + secrets.token_urlsafe(32)
        with verbinden(self.datei) as db:
            alt = db.execute("SELECT gesperrt FROM geraete WHERE name = ?", (name,)).fetchone()
            if alt is not None and not alt["gesperrt"]:
                raise GeraeteFehler(
                    f"Das Gerät „{name}“ gibt es schon. Für einen neuen Token erst "
                    f"sperren: geraet-anlegen.sh --sperren {name}"
                )
            db.execute(
                "INSERT INTO geraete (name, token_hash, angelegt, gesperrt) VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(name) DO UPDATE SET token_hash = excluded.token_hash, "
                "angelegt = excluded.angelegt, gesperrt = NULL",
                (name, token_hash(token), self._jetzt()),
            )
        return token

    def sperren(self, name: str) -> bool:
        """Sperrt das Gerät (der Token gilt sofort nicht mehr). ``False``, wenn unbekannt."""
        with verbinden(self.datei) as db:
            cursor = db.execute(
                "UPDATE geraete SET gesperrt = ? WHERE name = ? AND gesperrt IS NULL",
                (self._jetzt(), (name or "").strip()),
            )
        return cursor.rowcount > 0

    def liste(self) -> list[dict[str, Any]]:
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT name, angelegt, gesperrt FROM geraete ORDER BY name"
            ).fetchall()
        return [
            {"name": z["name"], "angelegt": z["angelegt"], "gesperrt": z["gesperrt"]}
            for z in zeilen
        ]

    def aktiv(self, name: str) -> bool:
        """``True``, solange das Gerät angelegt und nicht gesperrt ist."""
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT 1 FROM geraete WHERE name = ? AND gesperrt IS NULL", (name,)
            ).fetchone()
        return zeile is not None

    def pruefen(self, token: str | bytes | None) -> str | None:
        """Name des Geräts zu diesem Token, sonst ``None``.

        Vergleicht in konstanter Zeit mit allen gültigen Hashes (kein frühes Ende).
        """
        if not token:
            return None
        gesucht = token_hash(token).encode()
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT name, token_hash FROM geraete WHERE gesperrt IS NULL"
            ).fetchall()
        treffer = None
        for zeile in zeilen:
            if secrets.compare_digest(gesucht, str(zeile["token_hash"]).encode()):
                treffer = zeile["name"]
        return treffer


def bearer_token(kopf: str | bytes | None) -> bytes | None:
    """Der Token aus ``Authorization: Bearer …`` (oder ``None``)."""
    if not kopf:
        return None
    roh = kopf.encode() if isinstance(kopf, str) else kopf
    art, _, token = roh.partition(b" ")
    if art.lower() != b"bearer" or not token.strip():
        return None
    return token.strip()


# ----------------------------------------------------------------------------- Laufzeit


@dataclass(frozen=True)
class Ergebnis:
    """Antwort des Geräts auf einen Auftrag."""

    ok: bool
    text: str = ""
    art: str = ""
    fehler: str = ""


class GeraetNichtErreichbar(Exception):
    """Das Gerät ist offline (oder ging offline, während der Auftrag lief)."""


class GeraetZeitlimit(Exception):
    """Das Gerät hat innerhalb der Frist kein Ergebnis geliefert."""


@dataclass
class Auftrag:
    id: str
    geraet: str
    werkzeug: str
    argumente: dict[str, Any]
    frist_s: int
    zukunft: asyncio.Future[Ergebnis]


@dataclass
class Geraet:
    """Ein angeschlossenes Gerät (nur im Speicher)."""

    name: str
    sitzungen: tuple[str, ...] = ()
    #: Angemeldete Werkzeuge, wie sie vom Gerät kamen (nur die angenommenen).
    werkzeuge: list[dict[str, Any]] = field(default_factory=list)
    abgelehnt: list[dict[str, str]] = field(default_factory=list)
    angemeldet: bool = False
    #: Ist gerade ein Stream offen?
    verbunden: bool = False
    online: bool = False
    seit: datetime | None = None
    verbindung: int = 0
    warteschlange: asyncio.Queue[tuple[str, dict[str, Any]] | None] | None = None
    offline_timer: asyncio.TimerHandle | None = None

    def werkzeug_namen(self) -> list[str]:
        return [str(w["name"]) for w in self.werkzeuge]


def ereignis_text(art: str, daten: dict[str, Any]) -> str:
    """Ein Ereignis als Server-Sent Event (eine Zeile ``data``)."""
    return f"event: {art}\ndata: {json.dumps(daten, ensure_ascii=False)}\n\n"


#: Prüft angemeldete Werkzeuge: (Gerät, Liste) → (angenommen, abgelehnt).
WerkzeugPruefer = Callable[
    [str, list[dict[str, Any]]], tuple[list[dict[str, Any]], list[dict[str, str]]]
]


def werkzeuge_grob_pruefen(
    _geraet: str, werkzeuge: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Nur die Form: Name, Beschreibung, Parameter (JSON-Schema-Objekt), Stufe als Text."""
    angenommen: list[dict[str, Any]] = []
    abgelehnt: list[dict[str, str]] = []
    gesehen: set[str] = set()
    for eintrag in werkzeuge[:MAX_ANGEMELDET]:
        name = str(eintrag.get("name", "")) if isinstance(eintrag, dict) else ""
        if not isinstance(eintrag, dict) or not name:
            abgelehnt.append({"name": name or "?", "grund": "Eintrag ohne Namen"})
            continue
        if name in gesehen:
            abgelehnt.append({"name": name, "grund": "doppelt angemeldet"})
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            abgelehnt.append(
                {"name": name, "grund": "Name: nur a–z, 0–9 und _, höchstens 64 Zeichen"}
            )
            continue
        parameter = eintrag.get("parameter", {"type": "object", "properties": {}})
        if not isinstance(parameter, dict) or parameter.get("type", "object") != "object":
            abgelehnt.append({"name": name, "grund": "parameter muss ein JSON-Schema-Objekt sein"})
            continue
        beschreibung = eintrag.get("beschreibung", "")
        if not isinstance(beschreibung, str) or not beschreibung.strip():
            abgelehnt.append({"name": name, "grund": "beschreibung fehlt"})
            continue
        stufe = eintrag.get("stufe")
        if stufe not in ("lesen", "schreiben", "zerstoererisch"):
            abgelehnt.append(
                {"name": name, "grund": "stufe muss lesen, schreiben oder zerstoererisch sein"}
            )
            continue
        gesehen.add(name)
        angenommen.append(
            {
                "name": name,
                "beschreibung": beschreibung.strip()[:1000],
                "parameter": {"type": "object", "properties": {}, **parameter},
                "stufe": stufe,
            }
        )
    for eintrag in werkzeuge[MAX_ANGEMELDET:]:
        name = str(eintrag.get("name", "?")) if isinstance(eintrag, dict) else "?"
        abgelehnt.append({"name": name, "grund": f"mehr als {MAX_ANGEMELDET} Einträge"})
    return angenommen, abgelehnt


class Geraete:
    """Verwaltet angeschlossene Geräte: Anmeldung, Stream, Aufträge, online/offline."""

    def __init__(
        self,
        tokens: GeraeteTokens,
        zone: ZoneInfo,
        *,
        frist_s: int = 60,
        ping_sekunden: float = PING_SEKUNDEN,
        offline_nach: float = OFFLINE_NACH_SEKUNDEN,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.tokens = tokens
        self.zone = zone
        self.frist_s = max(1, int(frist_s))
        self.ping_sekunden = ping_sekunden
        self.offline_nach = offline_nach
        self._uhr = uhr or (lambda: datetime.now(zone))
        self._geraete: dict[str, Geraet] = {}
        self._auftraege: dict[str, Auftrag] = {}
        self._erledigt: dict[str, None] = {}
        #: Prüft angemeldete Werkzeuge (wird von den Rechner-Werkzeugen ersetzt).
        self.pruefer: WerkzeugPruefer = werkzeuge_grob_pruefen
        #: Wird bei jedem Wechsel online/offline und jeder neuen Anmeldung aufgerufen.
        self.bei_aenderung: list[Callable[[Geraet], None]] = []

    # -------------------------------------------------------------- Zustand

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def geraet(self, name: str) -> Geraet | None:
        return self._geraete.get(name)

    def __iter__(self):
        return iter(list(self._geraete.values()))

    def ist_online(self, name: str) -> bool:
        geraet = self._geraete.get(name)
        return geraet is not None and geraet.online

    def _melden(self, geraet: Geraet) -> None:
        for beobachter in list(self.bei_aenderung):
            try:
                beobachter(geraet)
            except Exception:
                log.exception("Geräte: Beobachter für %s fehlgeschlagen", geraet.name)

    def _online_setzen(self, geraet: Geraet, online: bool) -> None:
        if geraet.online == online:
            return
        geraet.online = online
        geraet.seit = self.jetzt()
        log.info("Gerät %s ist %s", geraet.name, "online" if online else "offline")
        if not online:
            for auftrag in [a for a in self._auftraege.values() if a.geraet == geraet.name]:
                if not auftrag.zukunft.done():
                    auftrag.zukunft.set_exception(GeraetNichtErreichbar(geraet.name))
        self._melden(geraet)

    def _geraet_holen(self, name: str) -> Geraet:
        geraet = self._geraete.get(name)
        if geraet is None:
            geraet = Geraet(name=name, seit=self.jetzt())
            self._geraete[name] = geraet
        return geraet

    def _timer_stoppen(self, geraet: Geraet) -> None:
        if geraet.offline_timer is not None:
            geraet.offline_timer.cancel()
            geraet.offline_timer = None

    def _offline_planen(self, geraet: Geraet) -> None:
        """Ohne Stream gilt das Gerät nach ``offline_nach`` Sekunden als offline."""
        self._timer_stoppen(geraet)
        verbindung = geraet.verbindung

        def pruefen() -> None:
            geraet.offline_timer = None
            if not geraet.verbunden and geraet.verbindung == verbindung:
                self._online_setzen(geraet, False)

        geraet.offline_timer = asyncio.get_running_loop().call_later(self.offline_nach, pruefen)

    # -------------------------------------------------------------- Anmelden

    def anmelden(
        self,
        name: str,
        *,
        protokoll: int,
        geraet_name: str,
        sitzungen: Iterable[str] = (),
        werkzeuge: Iterable[Any] = (),
    ) -> dict[str, Any]:
        """Meldet Gerät, Sitzungen und Werkzeuge an. ``name`` kommt aus dem Token."""
        if protokoll != PROTOKOLL:
            raise GeraeteFehler(f"Unbekanntes Protokoll {protokoll} – erwartet wird {PROTOKOLL}")
        if (geraet_name or "").strip() != name:
            raise GeraeteFehler(f"geraet „{geraet_name}“ passt nicht zum Token (Gerät „{name}“)")
        sitzungen = tuple(
            dict.fromkeys(
                str(s).strip()[:MAX_SITZUNG_LAENGE]
                for s in list(sitzungen)[:MAX_SITZUNGEN]
                if str(s).strip()
            )
        )
        angenommen, abgelehnt = self.pruefer(name, list(werkzeuge))
        geraet = self._geraet_holen(name)
        geraet.sitzungen = sitzungen
        geraet.werkzeuge = angenommen
        geraet.abgelehnt = abgelehnt
        geraet.angemeldet = True
        for eintrag in abgelehnt:
            log.warning(
                "Gerät %s: Werkzeug %s abgelehnt – %s", name, eintrag["name"], eintrag["grund"]
            )
        log.info(
            "Gerät %s angemeldet: %d Werkzeug(e), %d abgelehnt, Sitzungen: %s",
            name,
            len(angenommen),
            len(abgelehnt),
            ", ".join(sitzungen) or "keine",
        )
        if not geraet.verbunden:
            # Bis der Stream steht, zählt die Anmeldung als Lebenszeichen.
            self._online_setzen(geraet, True)
            self._offline_planen(geraet)
        self._melden(geraet)
        return {
            "ok": True,
            "geraet": name,
            "angenommen": geraet.werkzeug_namen(),
            "abgelehnt": abgelehnt,
        }

    # -------------------------------------------------------------- Stream

    async def stream(
        self, name: str, noch_gueltig: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]:
        """Server-Sent Events für das Gerät. Eine neue Verbindung ersetzt die alte."""
        geraet = self._geraet_holen(name)
        geraet.verbindung += 1
        meine = geraet.verbindung
        alt = geraet.warteschlange
        neu: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        if alt is not None:
            # Noch nicht gesendete Ereignisse wandern in die neue Verbindung.
            while not alt.empty():
                eintrag = alt.get_nowait()
                if eintrag is not None:
                    neu.put_nowait(eintrag)
            alt.put_nowait(None)  # die alte Verbindung endet
            log.info("Gerät %s: neue Verbindung ersetzt die alte", name)
        geraet.warteschlange = neu
        geraet.verbunden = True
        self._timer_stoppen(geraet)
        self._online_setzen(geraet, True)
        if not geraet.angemeldet:
            log.warning(
                "Gerät %s: Stream ohne Anmeldung – keine Werkzeuge, bis es sich anmeldet", name
            )
        try:
            yield ": verbunden\n\n"
            while True:
                try:
                    eintrag = await asyncio.wait_for(neu.get(), self.ping_sekunden)
                except TimeoutError:
                    if noch_gueltig is not None and not noch_gueltig():
                        log.warning("Gerät %s: Token gesperrt – Verbindung beendet", name)
                        return
                    yield ": ping\n\n"
                    continue
                if eintrag is None:
                    return
                art, daten = eintrag
                yield ereignis_text(art, daten)
        finally:
            if geraet.verbindung == meine:
                geraet.verbunden = False
                geraet.warteschlange = None
                if noch_gueltig is not None and not noch_gueltig():
                    self._online_setzen(geraet, False)
                else:
                    with contextlib.suppress(RuntimeError):
                        self._offline_planen(geraet)

    def senden(self, name: str, art: str, daten: dict[str, Any]) -> bool:
        """Legt ein Ereignis für das Gerät ab. ``False``, wenn es offline ist."""
        geraet = self._geraete.get(name)
        if geraet is None or not geraet.online:
            return False
        if geraet.warteschlange is None:
            geraet.warteschlange = asyncio.Queue()
        geraet.warteschlange.put_nowait((art, daten))
        return True

    # -------------------------------------------------------------- Aufträge

    async def auftrag(
        self,
        name: str,
        werkzeug: str,
        argumente: dict[str, Any],
        frist_s: int | None = None,
    ) -> Ergebnis:
        """Schickt einen Auftrag ans Gerät und wartet höchstens ``frist_s`` auf das Ergebnis.

        Wirft :class:`GeraetNichtErreichbar` (offline) oder :class:`GeraetZeitlimit`.
        """
        frist = max(1, int(frist_s or self.frist_s))
        kennung = str(uuid.uuid4())
        zukunft: asyncio.Future[Ergebnis] = asyncio.get_running_loop().create_future()
        auftrag = Auftrag(kennung, name, werkzeug, dict(argumente), frist, zukunft)
        daten = {"id": kennung, "werkzeug": werkzeug, "argumente": auftrag.argumente}
        self._auftraege[kennung] = auftrag
        try:
            if not self.senden(name, "auftrag", {**daten, "frist_s": frist}):
                raise GeraetNichtErreichbar(name)
            try:
                return await asyncio.wait_for(asyncio.shield(zukunft), frist)
            except TimeoutError as fehler:
                self.senden(name, "abbruch", {"id": kennung})
                raise GeraetZeitlimit(name) from fehler
            except asyncio.CancelledError:
                self.senden(name, "abbruch", {"id": kennung})
                raise
        finally:
            self._auftraege.pop(kennung, None)
            self._erledigt[kennung] = None
            while len(self._erledigt) > MAX_ERLEDIGT:
                del self._erledigt[next(iter(self._erledigt))]

    def ergebnis(self, name: str, daten: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Nimmt ein Ergebnis an. Gibt (HTTP-Status, Antwort) zurück."""
        kennung = str(daten.get("id") or "")
        auftrag = self._auftraege.get(kennung)
        if auftrag is None or auftrag.geraet != name:
            if kennung in self._erledigt and auftrag is None:
                return 409, {"ok": False, "fehler": "Auftrag ist schon erledigt oder abgebrochen"}
            return 404, {"ok": False, "fehler": "Unbekannter Auftrag"}
        if auftrag.zukunft.done():
            return 409, {"ok": False, "fehler": "Auftrag ist schon erledigt oder abgebrochen"}
        if daten.get("ok") is True:
            text = str(daten.get("ergebnis") or "")
            if len(text) > MAX_ERGEBNIS:
                text = text[:MAX_ERGEBNIS] + f"\n[… gekürzt auf {MAX_ERGEBNIS} Zeichen]"
            ergebnis = Ergebnis(True, text)
        else:
            art = str(daten.get("art") or "fehler")
            if art not in ERGEBNIS_ARTEN:
                art = "fehler"
            fehler = " ".join(str(daten.get("fehler") or "").split())[:MAX_FEHLER]
            ergebnis = Ergebnis(False, art=art, fehler=fehler or "ohne Begründung")
        auftrag.zukunft.set_result(ergebnis)
        geraet = self._geraete.get(name)
        if geraet is not None and not geraet.verbunden:
            # Ein Ergebnis ist ein Lebenszeichen.
            self._online_setzen(geraet, True)
            self._offline_planen(geraet)
        return 200, {"ok": True}

    # -------------------------------------------------------------- Ansicht

    def status(self) -> list[dict[str, Any]]:
        """Für ``/health``: Name, online, Werkzeuge, seit."""
        return [
            {
                "name": g.name,
                "online": g.online,
                "werkzeuge": g.werkzeug_namen(),
                "seit": g.seit.isoformat(timespec="seconds") if g.seit else None,
            }
            for g in sorted(self._geraete.values(), key=lambda g: g.name)
        ]

    def uebersicht(self) -> list[dict[str, Any]]:
        """Für ``GET /api/geraete``: alle angelegten Geräte mit Zustand und Werkzeugen."""
        eintraege = {e["name"]: e for e in self.tokens.liste()}
        namen = sorted(set(eintraege) | set(self._geraete))
        liste = []
        for name in namen:
            geraet = self._geraete.get(name)
            token = eintraege.get(name, {})
            liste.append(
                {
                    "name": name,
                    "online": bool(geraet and geraet.online),
                    "verbunden": bool(geraet and geraet.verbunden),
                    "seit": geraet.seit.isoformat(timespec="seconds")
                    if geraet and geraet.seit
                    else None,
                    "angelegt": token.get("angelegt"),
                    "gesperrt": token.get("gesperrt"),
                    "sitzungen": list(geraet.sitzungen) if geraet else [],
                    "werkzeuge": [
                        {"name": w["name"], "stufe": w["stufe"], "beschreibung": w["beschreibung"]}
                        for w in (geraet.werkzeuge if geraet else [])
                    ],
                    "abgelehnt": list(geraet.abgelehnt) if geraet else [],
                }
            )
        return liste

    async def schliessen(self) -> None:
        """Beim Stoppen des Dienstes: Timer weg, offene Aufträge beenden, Streams schliessen."""
        for geraet in self._geraete.values():
            self._timer_stoppen(geraet)
            if geraet.warteschlange is not None:
                geraet.warteschlange.put_nowait(None)
        for auftrag in self._auftraege.values():
            if not auftrag.zukunft.done():
                auftrag.zukunft.set_exception(GeraetNichtErreichbar(auftrag.geraet))


def geraete_bauen(einstellungen: Any, uhr: Callable[[], datetime] | None = None) -> Geraete:
    tokens = GeraeteTokens(einstellungen.datenbank, einstellungen.zone)
    return Geraete(tokens, einstellungen.zone, frist_s=einstellungen.geraete_frist, uhr=uhr)


# ----------------------------------------------------------------------------- Werkzeuge

#: Vorsilbe, die jedes Werkzeug eines Geräts tragen muss.
VORSILBE = "rechner_"
#: Mindeststufe der bekannten Rechner-Werkzeuge. Das Gerät darf höher melden, nie tiefer.
MINDESTSTUFEN: dict[str, str] = {
    "rechner_status": "lesen",
    "rechner_dateien_suchen": "lesen",
    "rechner_datei_lesen": "lesen",
    "rechner_fenster_anzeigen": "lesen",
    "rechner_zwischenablage_lesen": "lesen",
    "rechner_oeffnen": "schreiben",
    "rechner_fenster_steuern": "schreiben",
    "rechner_medien": "schreiben",
    "rechner_benachrichtigen": "schreiben",
    "rechner_oberflaeche": "schreiben",
    "rechner_datei_anlegen": "schreiben",
    "rechner_zwischenablage_setzen": "schreiben",
    "rechner_befehl": "zerstoererisch",
    "rechner_datei_aendern": "zerstoererisch",
    "rechner_prozess_beenden": "zerstoererisch",
    "rechner_energie": "zerstoererisch",
}
#: Unbekannte ``rechner_*``-Werkzeuge sind mindestens ``schreiben``.
STUFE_UNBEKANNT = "schreiben"
_RANG = {"lesen": 0, "schreiben": 1, "zerstoererisch": 2}
#: In der Vorschau wird der Inhalt dieser Angaben wörtlich gezeigt.
BEFEHL_ANGABEN = ("befehl", "kommando", "command", "cmd")

OFFLINE_TEXT = (
    "NICHT AUSGEFÜHRT: Der Rechner „{geraet}“ ist gerade nicht erreichbar (offline) – "
    "„{name}“ lief nicht. Sag Louis das ehrlich und behaupte nicht, es sei passiert."
)
ZEITLIMIT_TEXT = (
    "NICHT BESTÄTIGT: Der Rechner „{geraet}“ hat auf „{name}“ nicht innerhalb von {frist} "
    "Sekunden geantwortet; der Auftrag wurde abgebrochen. Sag Louis, dass du nicht weisst, ob "
    "es geklappt hat – behaupte keinen Erfolg."
)
ABGELEHNT_TEXT = (
    "NICHT AUSGEFÜHRT: Der Rechner „{geraet}“ hat „{name}“ abgelehnt: {fehler}. Sag Louis das "
    "ehrlich."
)
FEHLER_TEXT = (
    "FEHLGESCHLAGEN: Auf dem Rechner „{geraet}“ ist „{name}“ nicht gelungen ({art}): {fehler}. "
    "Sag Louis das ehrlich und behaupte keinen Erfolg."
)
HINTERGRUND_TEXT = (
    "NICHT AUSGEFÜHRT: „{name}“ läuft auf dem Rechner „{geraet}“. Im Hintergrund (Routine, "
    "Nachtschicht) benutzt Missa keine Geräte-Werkzeuge{zusatz}. Schlag es Louis im nächsten "
    "Gespräch vor."
)


def stufe_bestimmen(name: str, gemeldet: str) -> str:
    """Die Stufe, die gilt: die höhere aus Meldung des Geräts und Mindeststufe."""
    mindestens = MINDESTSTUFEN.get(name, STUFE_UNBEKANNT)
    return gemeldet if _RANG.get(gemeldet, 2) >= _RANG[mindestens] else mindestens


class GeraeteWerkzeuge:
    """Macht die angemeldeten Werkzeuge der Geräte zu Werkzeugen von Missa.

    Je Gerät eine Gruppe ``geraet:<name>`` im Werkzeugkasten: Sie ist nur zu sehen, solange
    das Gerät online ist, und nur in den Sitzungen, die es bei der Anmeldung genannt hat
    (``MISSA_GERAETE_ALLE_SITZUNGEN=true``: überall). Stufen, Rückfrage, fremde Inhalte,
    Notfall-Stopp und Aktionsprotokoll übernimmt der Werkzeugkasten wie bei jedem Werkzeug.
    """

    def __init__(
        self,
        geraete: Geraete,
        kasten: Any,
        *,
        alle_sitzungen: bool = False,
        max_werkzeuge: int = 20,
        im_hintergrund: str = "aus",
    ) -> None:
        self.geraete = geraete
        self.kasten = kasten
        self.alle_sitzungen = alle_sitzungen
        self.max_werkzeuge = max(0, max_werkzeuge)
        self.im_hintergrund = im_hintergrund
        geraete.pruefer = self.pruefen
        geraete.bei_aenderung.append(self.aktualisieren)

    @staticmethod
    def gruppe(geraet: str) -> str:
        return f"geraet:{geraet}"

    # -------------------------------------------------------------- Anmeldung

    def pruefen(
        self, geraet: str, werkzeuge: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Regeln für angemeldete Werkzeuge: Vorsilbe, kein eingebauter Name, Stufe, Anzahl."""
        grob, abgelehnt = werkzeuge_grob_pruefen(geraet, werkzeuge)
        eigene = set(self.kasten.gruppe(self.gruppe(geraet)))
        angenommen: list[dict[str, Any]] = []
        for eintrag in grob:
            name = eintrag["name"]
            if not name.startswith(VORSILBE) or name == VORSILBE:
                abgelehnt.append({"name": name, "grund": f"Name muss mit {VORSILBE} beginnen"})
                continue
            if self.kasten.ist_eingebaut(name):
                abgelehnt.append({"name": name, "grund": "so heisst ein eingebautes Werkzeug"})
                continue
            if name in self.kasten and name not in eigene:
                abgelehnt.append({"name": name, "grund": "Name ist schon vergeben"})
                continue
            if len(angenommen) >= self.max_werkzeuge:
                abgelehnt.append(
                    {
                        "name": name,
                        "grund": f"mehr als {self.max_werkzeuge} Werkzeuge "
                        "(MISSA_GERAETE_MAX_WERKZEUGE)",
                    }
                )
                continue
            stufe = stufe_bestimmen(name, eintrag["stufe"])
            if stufe != eintrag["stufe"]:
                log.warning(
                    "Gerät %s: %s als %s gemeldet – gilt als %s (Mindeststufe)",
                    geraet,
                    name,
                    eintrag["stufe"],
                    stufe,
                )
            angenommen.append({**eintrag, "stufe": stufe})
        return angenommen, abgelehnt

    def aktualisieren(self, geraet: Geraet) -> None:
        """Lädt die Gruppe des Geräts neu: online mit seinen Werkzeugen, offline leer."""
        werkzeuge = [self._werkzeug(geraet.name, w) for w in geraet.werkzeuge]
        self.kasten.gruppe_ersetzen(
            self.gruppe(geraet.name),
            werkzeuge if geraet.online else [],
            sichtbar=self._sichtbarkeit(geraet.name),
        )

    # -------------------------------------------------------------- Sichtbarkeit

    def _sichtbarkeit(self, name: str):
        from missa.core.werkzeuge import Stufe, im_hintergrund

        def sichtbar(werkzeug: Any, sitzung: str | None) -> bool:
            geraet = self.geraete.geraet(name)
            if geraet is None or not geraet.online:
                return False
            if im_hintergrund.get():
                return self.im_hintergrund == "nur_lesen" and werkzeug.stufe is Stufe.LESEN
            return self.alle_sitzungen or (sitzung is not None and sitzung in geraet.sitzungen)

        return sichtbar

    # -------------------------------------------------------------- Werkzeug

    def _werkzeug(self, geraet: str, eintrag: dict[str, Any]) -> Any:
        from missa.core.werkzeuge import Stufe, Werkzeug

        name = eintrag["name"]
        stufe = Stufe(eintrag["stufe"])

        async def aufrufen(**argumente: Any) -> str:
            return await self.aufrufen(geraet, name, stufe, argumente)

        aufrufen.__name__ = name

        def vorschau(**argumente: Any) -> str:
            return self.vorschau(geraet, name, argumente)

        return Werkzeug(
            name,
            f"{eintrag['beschreibung']} (läuft auf dem Rechner „{geraet}“)",
            eintrag["parameter"],
            aufrufen,
            stufe=stufe,
            vorschau=vorschau,
            geraet=geraet,
        )

    @staticmethod
    def vorschau(geraet: str, name: str, argumente: dict[str, Any]) -> str:
        """Für die Rückfrage: Gerät und genaue Aktion, ohne etwas zu tun."""
        befehl = next(
            (argumente[k] for k in BEFEHL_ANGABEN if isinstance(argumente.get(k), str)), None
        )
        if befehl is not None:
            rest = {k: v for k, v in argumente.items() if k not in BEFEHL_ANGABEN}
            zusatz = f" ({json.dumps(rest, ensure_ascii=False)})" if rest else ""
            return f"Auf {geraet} wird ausgeführt: `{befehl}`{zusatz}"
        angaben = json.dumps(argumente, ensure_ascii=False) if argumente else "ohne Angaben"
        return f"Auf {geraet} wird ausgeführt: {name} {angaben}"

    async def aufrufen(self, geraet: str, name: str, stufe: Any, argumente: dict[str, Any]) -> str:
        from missa.core.werkzeuge import Stufe, WerkzeugFehler, im_hintergrund

        if im_hintergrund.get() and not (
            self.im_hintergrund == "nur_lesen" and stufe is Stufe.LESEN
        ):
            zusatz = (
                " ausser lesenden (MISSA_GERAETE_IM_HINTERGRUND=nur_lesen)"
                if self.im_hintergrund == "nur_lesen"
                else " (MISSA_GERAETE_IM_HINTERGRUND=aus)"
            )
            raise WerkzeugFehler(HINTERGRUND_TEXT.format(name=name, geraet=geraet, zusatz=zusatz))
        try:
            ergebnis = await self.geraete.auftrag(geraet, name, argumente)
        except GeraetNichtErreichbar as fehler:
            raise WerkzeugFehler(OFFLINE_TEXT.format(geraet=geraet, name=name)) from fehler
        except GeraetZeitlimit as fehler:
            raise WerkzeugFehler(
                ZEITLIMIT_TEXT.format(geraet=geraet, name=name, frist=self.geraete.frist_s)
            ) from fehler
        if ergebnis.ok:
            return ergebnis.text or "(Der Rechner meldet Erfolg ohne weiteren Text.)"
        if ergebnis.art == "abgelehnt":
            raise WerkzeugFehler(
                ABGELEHNT_TEXT.format(geraet=geraet, name=name, fehler=ergebnis.fehler)
            )
        raise WerkzeugFehler(
            FEHLER_TEXT.format(geraet=geraet, name=name, art=ergebnis.art, fehler=ergebnis.fehler)
        )


def geraete_werkzeuge_bauen(einstellungen: Any, geraete: Geraete, kasten: Any) -> GeraeteWerkzeuge:
    return GeraeteWerkzeuge(
        geraete,
        kasten,
        alle_sitzungen=einstellungen.geraete_alle_sitzungen,
        max_werkzeuge=einstellungen.geraete_max_werkzeuge,
        im_hintergrund=einstellungen.geraete_im_hintergrund,
    )


# ----------------------------------------------------------------------------- Kommandozeile


def main(argv: list[str] | None = None) -> int:
    """``python -m missa.core.geraete anlegen NAME | sperren NAME | liste`` (für das Script)."""
    from missa.core.config import STANDARD_ENV_DATEI, Einstellungen, env_datei_laden

    parser = argparse.ArgumentParser(prog="python -m missa.core.geraete")
    parser.add_argument("befehl", choices=["anlegen", "sperren", "liste"])
    parser.add_argument("name", nargs="?", default="")
    argumente = parser.parse_args(argv)

    env_datei_laden(Path(os.environ.get("MISSA_ENV_DATEI", str(STANDARD_ENV_DATEI))))
    einstellungen = Einstellungen.aus_umgebung()
    tokens = GeraeteTokens(einstellungen.datenbank, einstellungen.zone)

    if argumente.befehl == "liste":
        eintraege = tokens.liste()
        if not eintraege:
            print("Noch kein Gerät angelegt.")
        for e in eintraege:
            zustand = f"gesperrt seit {e['gesperrt']}" if e["gesperrt"] else "aktiv"
            print(f"{e['name']}: {zustand} (angelegt {e['angelegt']})")
        return 0
    if not argumente.name:
        print("Fehler: Gerätename fehlt.", file=sys.stderr)
        return 2
    if argumente.befehl == "sperren":
        if tokens.sperren(argumente.name):
            print(f"Gerät „{argumente.name}“ gesperrt – sein Token gilt nicht mehr.")
            return 0
        print(f"Kein aktives Gerät „{argumente.name}“.", file=sys.stderr)
        return 1
    try:
        token = tokens.anlegen(argumente.name)
    except GeraeteFehler as fehler:
        print(f"Fehler: {fehler}", file=sys.stderr)
        return 1
    print(f"Gerät „{argumente.name}“ angelegt. Token (wird nur jetzt angezeigt):")
    print()
    print(f"  {token}")
    print()
    print("Auf dem Gerät eintragen. Gespeichert ist nur der Hash – geht der Token verloren,")
    print(f"das Gerät sperren (--sperren {argumente.name}) und neu anlegen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
