"""Erreichbarkeit der eigenen Domains: Prüfungen verwalten, Ausfall und Wiederherstellung melden.

Welche Ziele Missa prüft, steht in der Tabelle ``erreichbarkeit_pruefungen`` – nicht in
``missa.env``. Missa legt Prüfungen im Gespräch selbst an, ändert Port, Pfad, Intervall
und Stufe, pausiert und löscht sie (Löschen nur nach Louis' Ja). ``ERREICHBARKEIT_DOMAINS``
ist nur der **Startwert**: Beim ersten Start mit leerer Tabelle übernimmt Missa die Einträge
einmal (Merker ``erreichbarkeit_startwert`` in der Tabelle ``stand``) und liest die Variable
danach für die Prüfung nicht mehr – auch nicht, wenn alle Prüfungen gelöscht sind.

Der :class:`ErreichbarkeitsWaechter` läuft jede Minute und prüft nur die fälligen Ziele
(jede Prüfung hat ihr eigenes Intervall, pausierte laufen nicht): DNS, HTTPS-Antwort und
gültiges Zertifikat über die öffentliche Adresse – Standard Port 443 und Pfad ``/``, sonst
z. B. ``cloud.example.ch:8443``. Schlägt die Prüfung :data:`AUSFALL_NACH` Mal hintereinander
fehl, meldet Missa den Ausfall (mit der Stufe der Prüfung, Standard ``wichtig``); klappt sie
danach wieder, meldet sie die Rückkehr (``info``). Der Zustand steht in der Tabelle
``erreichbarkeit`` und überlebt Neustarts – auch das Ablaufdatum der Zertifikate.
Schlüssel ist der Name des Ziels: bei Port 443 und Pfad ``/`` der reine Domainname (wie in
älteren Versionen), sonst ``domain:port/pfad``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe, stufe_verstehen
from missa.core.datenbank import stand_lesen, stand_setzen, verbinden, zeile_aendern
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.erreichbarkeit import (
    DomainErgebnis,
    ErreichbarkeitsPruefer,
    PruefZiel,
    ZielFehler,
    pfad_pruefen,
    port_pruefen,
    ziel_lesen,
    ziele_lesen,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS erreichbarkeit (
    domain TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('ok', 'gestoert')),
    seit TEXT NOT NULL,
    fehler_in_folge INTEGER NOT NULL DEFAULT 0,
    gemeldet INTEGER NOT NULL DEFAULT 0,
    letzte_pruefung TEXT NOT NULL,
    problem TEXT NOT NULL DEFAULT '',
    adressen TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    zertifikat_bis TEXT
);
CREATE TABLE IF NOT EXISTS erreichbarkeit_pruefungen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 443 CHECK (port BETWEEN 1 AND 65535),
    pfad TEXT NOT NULL DEFAULT '/',
    intervall_minuten INTEGER NOT NULL DEFAULT 5 CHECK (intervall_minuten BETWEEN 1 AND 1440),
    stufe TEXT NOT NULL DEFAULT 'wichtig' CHECK (stufe IN ('info', 'wichtig', 'notfall')),
    pausiert INTEGER NOT NULL DEFAULT 0,
    notiz TEXT NOT NULL DEFAULT '',
    erstellt TEXT NOT NULL,
    geaendert TEXT NOT NULL,
    UNIQUE (domain, port, pfad)
);
"""

#: So viele Fehlschläge hintereinander gelten als Ausfall (kurze Aussetzer zählen nicht).
AUSFALL_NACH = 2
#: Höchstens so viele Prüfungen – mehr braucht ein Homelab nicht.
MAX_PRUEFUNGEN = 50
#: Erlaubtes Intervall einer Prüfung in Minuten (1 Minute bis 1 Tag).
MIN_INTERVALL, MAX_INTERVALL = 1, 24 * 60
#: Längste Notiz zu einer Prüfung.
MAX_NOTIZ = 200
#: So oft schaut der Zeitplaner nach fälligen Prüfungen.
TAKT = timedelta(minutes=1)
#: Eine Prüfung gilt so viel vor Ablauf ihres Intervalls schon als fällig (Takt-Schwankung).
SPIELRAUM = timedelta(seconds=30)
#: Merker in ``stand``: Startwert aus ERREICHBARKEIT_DOMAINS ist übernommen.
MERKER_STARTWERT = "erreichbarkeit_startwert"
#: Merker in ``stand``: Diese env-Einträge fehlen in der Tabelle (schon im Log erwähnt).
MERKER_HINWEIS = "erreichbarkeit_startwert_hinweis"

PRUEFUNG_SPALTEN = frozenset(
    {"domain", "port", "pfad", "intervall_minuten", "stufe", "pausiert", "notiz", "geaendert"}
)


def _dauer_text(von: datetime, bis: datetime) -> str:
    minuten = max(1, round((bis - von).total_seconds() / 60))
    if minuten < 60:
        return f"{minuten} Minute{'n' if minuten != 1 else ''}"
    stunden, rest = divmod(minuten, 60)
    return f"{stunden} Std. {rest} Min." if rest else f"{stunden} Std."


def _intervall_text(minuten: int) -> str:
    if minuten == 1:
        return "jede Minute"
    if minuten % 60 == 0:
        stunden = minuten // 60
        return "jede Stunde" if stunden == 1 else f"alle {stunden} Std."
    return f"alle {minuten} Min."


def intervall_verstehen(wert: Any) -> int:
    """Intervall in Minuten (1 bis 1440)."""
    text = str(wert if wert is not None else "").strip().casefold().removesuffix("min").strip()
    try:
        zahl = float(text.replace(",", "."))
    except ValueError as fehler:
        raise WerkzeugFehler(
            f"„{wert}“ ist kein Intervall in Minuten (z. B. 5, oder 60 für jede Stunde)."
        ) from fehler
    if zahl != int(zahl) or not MIN_INTERVALL <= zahl <= MAX_INTERVALL:
        raise WerkzeugFehler(
            f"Das Intervall muss eine ganze Zahl von {MIN_INTERVALL} bis {MAX_INTERVALL} "
            "Minuten sein."
        )
    return int(zahl)


def notiz_verstehen(wert: Any) -> str:
    text = " ".join(str(wert if wert is not None else "").split())
    if len(text) > MAX_NOTIZ:
        raise WerkzeugFehler(f"Die Notiz ist zu lang (höchstens {MAX_NOTIZ} Zeichen).")
    return text


def ziel_verstehen(ziel: Any, port: Any = None, pfad: Any = None) -> PruefZiel:
    """Ziel aus dem Gespräch, streng geprüft wie ``ERREICHBARKEIT_DOMAINS``."""
    try:
        gelesen = ziel_lesen(ziel)
        return PruefZiel(
            gelesen.domain,
            port_pruefen(port) if port not in (None, "") else gelesen.port,
            pfad_pruefen(pfad) if pfad not in (None, "") else gelesen.pfad,
        )
    except ZielFehler as fehler:
        raise WerkzeugFehler(str(fehler)) from fehler


@dataclass(frozen=True)
class Pruefung:
    """Eine Erreichbarkeits-Prüfung aus der Tabelle ``erreichbarkeit_pruefungen``."""

    id: int
    ziel: PruefZiel
    intervall_minuten: int
    stufe: AlarmStufe
    pausiert: bool = False
    notiz: str = ""

    @property
    def schluessel(self) -> str:
        return self.ziel.schluessel

    def zeile(self) -> str:
        text = (
            f"#{self.id}: {self.schluessel} – {_intervall_text(self.intervall_minuten)}, "
            f"Ausfall → {self.stufe}"
        )
        if self.pausiert:
            text += ", PAUSIERT"
        if self.notiz:
            text += f" (Notiz: {self.notiz})"
        return text

    def als_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ziel": self.schluessel,
            "domain": self.ziel.domain,
            "port": self.ziel.port,
            "pfad": self.ziel.pfad,
            "intervall_minuten": self.intervall_minuten,
            "stufe": str(self.stufe),
            "pausiert": self.pausiert,
            "notiz": self.notiz,
        }


class ErreichbarkeitsWaechter:
    """Verwaltet die Prüfungen, merkt sich den Zustand jedes Ziels und meldet Ausfall und Rückkehr.

    ``startwert`` sind die Ziele aus ``ERREICHBARKEIT_DOMAINS`` (ohne Angabe: die Ziele des
    ``pruefer``); sie werden nur beim allerersten Mal in die Tabelle übernommen.
    """

    def __init__(
        self,
        pruefer: ErreichbarkeitsPruefer,
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        *,
        stufe: AlarmStufe = AlarmStufe.WICHTIG,
        intervall_minuten: int = 5,
        startwert: Iterable[str | PruefZiel] | None = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.pruefer = pruefer
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self.stufe = stufe
        self.intervall_minuten = min(MAX_INTERVALL, max(MIN_INTERVALL, int(intervall_minuten)))
        self._uhr = uhr or alarme.jetzt
        with verbinden(datei) as db:
            db.executescript(SCHEMA)
        self.startwert_uebernehmen(pruefer.ziele if startwert is None else startwert)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def _zeitstempel(self) -> str:
        return self.jetzt().astimezone(UTC).isoformat(timespec="seconds")

    # ------------------------------------------------------------------ Startwert

    def startwert_uebernehmen(self, eintraege: Iterable[str | PruefZiel]) -> list[Pruefung]:
        """Übernimmt ``ERREICHBARKEIT_DOMAINS`` genau einmal in die (leere) Tabelle."""
        ziele = ziele_lesen(list(eintraege))
        if stand_lesen(self.datei, MERKER_STARTWERT) is not None:
            self._startwert_hinweis(ziele)
            return []
        if not ziele and not self.pruefungen():
            # Nichts übernommen: Kommt später etwas in die env, gilt es dann als Startwert.
            return []
        neu = []
        if not self.pruefungen():
            jetzt = self._zeitstempel()
            with verbinden(self.datei) as db:
                for ziel in ziele[:MAX_PRUEFUNGEN]:
                    cursor = db.execute(
                        "INSERT INTO erreichbarkeit_pruefungen (domain, port, pfad, "
                        "intervall_minuten, stufe, pausiert, notiz, erstellt, geaendert) "
                        "VALUES (?, ?, ?, ?, ?, 0, '', ?, ?)",
                        (
                            ziel.domain,
                            ziel.port,
                            ziel.pfad,
                            self.intervall_minuten,
                            str(self.stufe),
                            jetzt,
                            jetzt,
                        ),
                    )
                    neu.append(int(cursor.lastrowid or 0))
            if neu:
                log.info(
                    "Erreichbarkeit: %d Prüfung(en) aus ERREICHBARKEIT_DOMAINS übernommen (%s). "
                    "Ab jetzt gilt die Tabelle erreichbarkeit_pruefungen – ändern im Chat.",
                    len(neu),
                    ", ".join(z.schluessel for z in ziele[:MAX_PRUEFUNGEN]),
                )
        stand_setzen(self.datei, MERKER_STARTWERT, self._zeitstempel())
        return [self.pruefung(n) for n in neu]

    def _startwert_hinweis(self, ziele: tuple[PruefZiel, ...]) -> None:
        """Steht in der env etwas, das nicht (mehr) in der Tabelle ist: einmal im Log."""
        vorhanden = {p.ziel for p in self.pruefungen()}
        fehlend = sorted(z.schluessel for z in ziele if z not in vorhanden)
        text = ",".join(fehlend)
        if not fehlend or stand_lesen(self.datei, MERKER_HINWEIS) == text:
            return
        log.info(
            "Erreichbarkeit: ERREICHBARKEIT_DOMAINS nennt %s, das steht aber nicht in den "
            "Prüfungen. Die Variable ist nur der Startwert beim ersten Start – Prüfungen "
            "legt Missa im Chat an.",
            ", ".join(fehlend),
        )
        stand_setzen(self.datei, MERKER_HINWEIS, text)

    # ------------------------------------------------------------------ Prüfungen

    @staticmethod
    def _pruefung(zeile: Any) -> Pruefung:
        return Pruefung(
            id=zeile["id"],
            ziel=PruefZiel(zeile["domain"], zeile["port"], zeile["pfad"]),
            intervall_minuten=zeile["intervall_minuten"],
            stufe=AlarmStufe(zeile["stufe"]),
            pausiert=bool(zeile["pausiert"]),
            notiz=zeile["notiz"],
        )

    def pruefungen(self, *, nur_aktive: bool = False) -> list[Pruefung]:
        with verbinden(self.datei) as db:
            zeilen = db.execute("SELECT * FROM erreichbarkeit_pruefungen ORDER BY id").fetchall()
        alle = [self._pruefung(z) for z in zeilen]
        return [p for p in alle if not p.pausiert] if nur_aktive else alle

    def pruefung(self, nummer: int) -> Pruefung:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM erreichbarkeit_pruefungen WHERE id = ?", (nummer,)
            ).fetchone()
        if zeile is None:
            raise WerkzeugFehler(
                f"Eine Prüfung #{nummer} gibt es nicht. Zeig sie mit "
                "erreichbarkeit_pruefungen_anzeigen an."
            )
        return self._pruefung(zeile)

    def finden(self, angabe: Any) -> Pruefung:
        """Prüfung per Nummer (``3``, ``#3``) oder Name (``git``, ``cloud.example.ch:8443``)."""
        text = str(angabe if angabe is not None else "").strip().casefold()
        if not text:
            raise WerkzeugFehler("Welche Prüfung? Nenne die Nummer oder die Domain (z. B. git).")
        if text.lstrip("#").isdigit():
            return self.pruefung(int(text.lstrip("#")))
        alle = self.pruefungen()
        for vergleich in (
            lambda p: p.schluessel == text,
            lambda p: p.ziel.domain == text,
            lambda p: text in p.schluessel,
        ):
            treffer = [p for p in alle if vergleich(p)]
            if len(treffer) == 1:
                return treffer[0]
            if len(treffer) > 1:
                raise WerkzeugFehler(
                    f"„{angabe}“ passt auf mehrere Prüfungen: "
                    + "; ".join(p.zeile() for p in treffer)
                    + ". Nenne die Nummer."
                )
        raise WerkzeugFehler(
            f"Eine Prüfung „{angabe}“ gibt es nicht. Vorhanden: "
            + (", ".join(f"#{p.id} {p.schluessel}" for p in alle) or "keine")
            + "."
        )

    def _mit_ziel(self, ziel: PruefZiel) -> Pruefung | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM erreichbarkeit_pruefungen "
                "WHERE domain = ? AND port = ? AND pfad = ?",
                (ziel.domain, ziel.port, ziel.pfad),
            ).fetchone()
        return self._pruefung(zeile) if zeile else None

    def anlegen(
        self,
        ziel: PruefZiel,
        *,
        intervall_minuten: int | None = None,
        stufe: AlarmStufe | None = None,
        notiz: str | None = None,
    ) -> tuple[Pruefung, bool]:
        """Legt eine Prüfung an und gibt (Prüfung, neu?) zurück – nie doppelt."""
        vorhanden = self._mit_ziel(ziel)
        if vorhanden is not None:
            felder: dict[str, Any] = {
                "intervall_minuten": intervall_minuten,
                "stufe": str(stufe) if stufe else None,
                "notiz": notiz,
            }
            felder = {k: v for k, v in felder.items() if v is not None}
            if felder:
                self._speichern(vorhanden.id, felder)
            return self.pruefung(vorhanden.id), False
        if len(self.pruefungen()) >= MAX_PRUEFUNGEN:
            raise WerkzeugFehler(
                f"Es gibt schon {MAX_PRUEFUNGEN} Prüfungen – mehr gehen nicht. Lösch oder ändere "
                "erst eine bestehende."
            )
        jetzt = self._zeitstempel()
        with verbinden(self.datei) as db:
            cursor = db.execute(
                "INSERT INTO erreichbarkeit_pruefungen (domain, port, pfad, intervall_minuten, "
                "stufe, pausiert, notiz, erstellt, geaendert) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    ziel.domain,
                    ziel.port,
                    ziel.pfad,
                    intervall_minuten or self.intervall_minuten,
                    str(stufe or self.stufe),
                    notiz or "",
                    jetzt,
                    jetzt,
                ),
            )
            nummer = int(cursor.lastrowid or 0)
        # Ab jetzt verwaltet Louis die Prüfungen selbst – kein Startwert mehr aus der env.
        if stand_lesen(self.datei, MERKER_STARTWERT) is None:
            stand_setzen(self.datei, MERKER_STARTWERT, jetzt)
        log.info("Erreichbarkeit: Prüfung #%s %s angelegt", nummer, ziel.schluessel)
        return self.pruefung(nummer), True

    def _speichern(self, nummer: int, felder: dict[str, Any]) -> None:
        felder = {**felder, "geaendert": self._zeitstempel()}
        with verbinden(self.datei) as db:
            zeile_aendern(
                db,
                "erreichbarkeit_pruefungen",
                felder,
                wo="id",
                wert=nummer,
                erlaubt=PRUEFUNG_SPALTEN,
            )

    def aendern(
        self,
        pruefung: Pruefung,
        *,
        port: int | None = None,
        pfad: str | None = None,
        intervall_minuten: int | None = None,
        stufe: AlarmStufe | None = None,
        notiz: str | None = None,
    ) -> Pruefung:
        neues_ziel = PruefZiel(
            pruefung.ziel.domain,
            pruefung.ziel.port if port is None else port,
            pruefung.ziel.pfad if pfad is None else pfad,
        )
        felder: dict[str, Any] = {
            "intervall_minuten": intervall_minuten,
            "stufe": str(stufe) if stufe else None,
            "notiz": notiz,
        }
        felder = {k: v for k, v in felder.items() if v is not None}
        if neues_ziel != pruefung.ziel:
            andere = self._mit_ziel(neues_ziel)
            if andere is not None:
                raise WerkzeugFehler(
                    f"{neues_ziel.schluessel} wird schon geprüft ({andere.zeile()}). Ändere "
                    "oder lösch lieber diese Prüfung."
                )
            felder.update(port=neues_ziel.port, pfad=neues_ziel.pfad)
        if not felder:
            raise WerkzeugFehler("Was soll ich an der Prüfung ändern? Es fehlen neue Angaben.")
        self._speichern(pruefung.id, felder)
        if neues_ziel != pruefung.ziel:
            # Der alte Zustand gehört zum alten Ziel – ein gemeldeter Ausfall dort ist erledigt.
            self._zustand_vergessen(pruefung.ziel)
            log.info(
                "Erreichbarkeit: Prüfung #%s jetzt %s statt %s",
                pruefung.id,
                neues_ziel.schluessel,
                pruefung.schluessel,
            )
        return self.pruefung(pruefung.id)

    def pausieren(self, pruefung: Pruefung, pausiert: bool = True) -> Pruefung:
        if pruefung.pausiert != pausiert:
            self._speichern(pruefung.id, {"pausiert": int(pausiert)})
        return self.pruefung(pruefung.id)

    def loeschen(self, pruefung: Pruefung) -> Pruefung:
        with verbinden(self.datei) as db:
            db.execute("DELETE FROM erreichbarkeit_pruefungen WHERE id = ?", (pruefung.id,))
        self._zustand_vergessen(pruefung.ziel)
        log.info("Erreichbarkeit: Prüfung #%s %s gelöscht", pruefung.id, pruefung.schluessel)
        return pruefung

    def _zustand_vergessen(self, ziel: PruefZiel) -> None:
        if self._mit_ziel(ziel) is not None:
            return  # eine andere Prüfung hat dasselbe Ziel
        with verbinden(self.datei) as db:
            db.execute("DELETE FROM erreichbarkeit WHERE domain = ?", (ziel.schluessel,))

    def zertifikat_ziele(self) -> list[PruefZiel]:
        """Domain und Port der aktiven Prüfungen – Standard für die Zertifikatsprüfung."""
        return list(
            dict.fromkeys(
                PruefZiel(p.ziel.domain, p.ziel.port) for p in self.pruefungen(nur_aktive=True)
            )
        )

    # ------------------------------------------------------------------ Zustand

    def _alle_zustaende(self) -> dict[str, dict[str, Any]]:
        with verbinden(self.datei) as db:
            zeilen = db.execute("SELECT * FROM erreichbarkeit ORDER BY domain").fetchall()
        return {z["domain"]: dict(z) for z in zeilen}

    def zustand(self) -> dict[str, dict[str, Any]]:
        """Gespeicherter Zustand je Ziel der aktiven Prüfungen (Schlüssel = Name des Ziels)."""
        aktive = {p.schluessel for p in self.pruefungen(nur_aktive=True)}
        return {k: v for k, v in self._alle_zustaende().items() if k in aktive}

    def faellig(self, jetzt: datetime | None = None) -> list[Pruefung]:
        """Aktive Prüfungen, deren Intervall seit der letzten Prüfung abgelaufen ist."""
        jetzt = jetzt or self.jetzt()
        zustaende = self._alle_zustaende()
        faellig = []
        for pruefung in self.pruefungen(nur_aktive=True):
            alt = zustaende.get(pruefung.schluessel)
            if alt is not None:
                try:
                    zuletzt = datetime.fromisoformat(alt["letzte_pruefung"])
                except ValueError:
                    zuletzt = None
                intervall = timedelta(minutes=pruefung.intervall_minuten)
                # Liegt die letzte Prüfung „in der Zukunft“ (Uhr verstellt), gilt sie als fällig.
                if zuletzt is not None and timedelta(0) <= jetzt - zuletzt < intervall - SPIELRAUM:
                    continue
            faellig.append(pruefung)
        return faellig

    async def pruefen(self) -> list[Alarm]:
        """Regelmäßiger Auftrag (jede Minute): fällige Prüfungen ausführen, Änderungen melden."""
        jetzt = self.jetzt()
        faellig = self.faellig(jetzt)
        ergebnisse = await asyncio.gather(*(self.pruefer.pruefen(p.ziel) for p in faellig))
        gemeldet = []
        for pruefung, ergebnis in zip(faellig, ergebnisse, strict=True):
            alarm = self.verarbeiten(ergebnis, jetzt, pruefung.stufe)
            if alarm is not None:
                gemeldet.append(alarm)
        return gemeldet

    def verarbeiten(
        self, ergebnis: DomainErgebnis, jetzt: datetime, stufe: AlarmStufe | None = None
    ) -> Alarm | None:
        name = ergebnis.schluessel
        alt = self._alle_zustaende().get(name)
        seit = datetime.fromisoformat(alt["seit"]).astimezone(self.zone) if alt else jetzt
        gemeldet = bool(alt and alt["gemeldet"])
        alarm = None
        if ergebnis.ok:
            if alt and alt["status"] != "ok":
                if gemeldet:
                    alarm = self.alarme.melden(
                        AlarmStufe.INFO,
                        f"{name} ist wieder erreichbar",
                        f"Ausfall von {seit:%H:%M} bis {jetzt:%H:%M} Uhr "
                        f"({_dauer_text(seit, jetzt)}).",
                        schluessel=f"erreichbarkeit:{name}:zurueck",
                        quelle="erreichbarkeit",
                    )
                    log.info("%s ist wieder erreichbar", name)
                seit = jetzt
            status, fehler, gemeldet = "ok", 0, False
        else:
            if not alt or alt["status"] == "ok":
                seit = jetzt
            status, fehler = "gestoert", (alt["fehler_in_folge"] if alt else 0) + 1
            if fehler >= AUSFALL_NACH and not gemeldet:
                alarm = self.alarme.melden(
                    stufe or self.stufe,
                    f"{name} ist nicht erreichbar",
                    f"Seit {seit:%H:%M} Uhr: {ergebnis.problem}. Geprüft von ai01 über die "
                    "öffentliche Domain. Ist das ein Fehlalarm (z. B. anderer Port oder Pfad), "
                    "sag es mir – dann passe ich die Prüfung an.",
                    schluessel=f"erreichbarkeit:{name}:ausfall",
                    quelle="erreichbarkeit",
                )
                gemeldet = alarm is not None
        bis = ergebnis.zertifikat_bis
        if bis is None and alt and alt["zertifikat_bis"]:
            bis = datetime.fromisoformat(alt["zertifikat_bis"])
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO erreichbarkeit (domain, status, seit, fehler_in_folge, gemeldet, "
                "letzte_pruefung, problem, adressen, http_status, zertifikat_bis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (domain) DO UPDATE SET "
                "status = excluded.status, seit = excluded.seit, "
                "fehler_in_folge = excluded.fehler_in_folge, gemeldet = excluded.gemeldet, "
                "letzte_pruefung = excluded.letzte_pruefung, problem = excluded.problem, "
                "adressen = excluded.adressen, http_status = excluded.http_status, "
                "zertifikat_bis = excluded.zertifikat_bis",
                (
                    name,
                    status,
                    seit.astimezone(UTC).isoformat(timespec="seconds"),
                    fehler,
                    int(gemeldet),
                    jetzt.astimezone(UTC).isoformat(timespec="seconds"),
                    ergebnis.problem,
                    ",".join(ergebnis.adressen),
                    ergebnis.http_status,
                    bis.astimezone(UTC).isoformat(timespec="seconds") if bis else None,
                ),
            )
        return alarm

    # ------------------------------------------------------------------ Anzeigen

    def zeile(self, ergebnis: DomainErgebnis) -> str:
        if ergebnis.ok:
            text = f"- {ergebnis.schluessel}: erreichbar (HTTP {ergebnis.http_status}"
            if ergebnis.zertifikat_bis:
                tage = (ergebnis.zertifikat_bis - datetime.now(UTC)).days
                text += f", Zertifikat gültig bis {ergebnis.zertifikat_bis:%d.%m.%Y}, {tage} Tage"
            return text + ")"
        return f"- {ergebnis.schluessel}: NICHT erreichbar – {ergebnis.problem}"

    def letztes_ergebnis(self, pruefung: Pruefung, zustand: dict[str, Any] | None) -> str:
        if zustand is None:
            return "noch nicht geprüft"
        try:
            zeit = datetime.fromisoformat(zustand["letzte_pruefung"]).astimezone(self.zone)
            wann = f"{zeit:%d.%m. %H:%M}"
        except ValueError:
            wann = "?"
        if zustand["status"] == "ok":
            return f"zuletzt {wann} erreichbar (HTTP {zustand['http_status']})"
        text = f"zuletzt {wann} NICHT erreichbar – {zustand['problem']}"
        return text + (" (als Ausfall gemeldet)" if zustand["gemeldet"] else "")

    def anzeigen(self) -> str:
        """Werkzeug: alle Prüfungen mit Ziel, Intervall, Stufe, Pause und letztem Ergebnis."""
        pruefungen = self.pruefungen()
        if not pruefungen:
            return (
                "Ich habe keine Erreichbarkeits-Prüfungen. Leg eine an mit "
                "erreichbarkeit_pruefung_anlegen (z. B. cloud.example.ch:8443)."
            )
        zustaende = self._alle_zustaende()
        zeilen = [f"Erreichbarkeits-Prüfungen ({len(pruefungen)} von höchstens {MAX_PRUEFUNGEN}):"]
        for p in pruefungen:
            zeilen.append(f"- {p.zeile()}; {self.letztes_ergebnis(p, zustaende.get(p.schluessel))}")
        zeilen.append(
            "Ein Ausfall gilt nach zwei Fehlschlägen hintereinander; die Rückkehr melde ich "
            "als info."
        )
        return "\n".join(zeilen)

    async def jetzt_pruefen(self, domain: str | None = None) -> str:
        """Werkzeug: prüft sofort (alle aktiven oder eine bestimmte Prüfung)."""
        if (domain or "").strip():
            try:
                pruefungen = [self.finden(domain)]
            except WerkzeugFehler:
                vorhanden = ", ".join(p.schluessel for p in self.pruefungen()) or "keine"
                return (
                    f"„{domain}“ prüfe ich nicht. Meine Prüfungen: {vorhanden}. Soll ich eine "
                    "anlegen (erreichbarkeit_pruefung_anlegen)?"
                )
        else:
            pruefungen = self.pruefungen(nur_aktive=True)
            if not pruefungen:
                return (
                    "Ich habe keine aktiven Erreichbarkeits-Prüfungen. Leg eine an mit "
                    "erreichbarkeit_pruefung_anlegen."
                )
        zeilen = ["Erreichbarkeit von außen (geprüft von ai01 über die öffentliche Domain):"]
        ergebnisse = await asyncio.gather(*(self.pruefer.pruefen(p.ziel) for p in pruefungen))
        for pruefung, ergebnis in zip(pruefungen, ergebnisse, strict=True):
            zeile = self.zeile(ergebnis)
            zeilen.append(zeile + (" [Prüfung pausiert]" if pruefung.pausiert else ""))
        gestoert = [d for d, z in self.zustand().items() if z["status"] != "ok" and z["gemeldet"]]
        if gestoert:
            zeilen.append("Als Ausfall gemeldet: " + ", ".join(gestoert))
        if any(not e.ok for e in ergebnisse):
            zeilen.append(
                "Wenn das ein Fehlalarm ist (z. B. läuft der Dienst auf einem anderen Port), "
                "biete Louis an, die Prüfung mit erreichbarkeit_pruefung_aendern anzupassen."
            )
        return "\n".join(zeilen)

    def kontext(self, _frage: str) -> str | None:
        """Kontextquelle: gemeldete Ausfälle im Systemprompt."""
        gestoert = [
            f"- {d}: {z['problem']}"
            for d, z in self.zustand().items()
            if z["status"] != "ok" and z["gemeldet"]
        ]
        if not gestoert:
            return None
        return (
            "## Nicht erreichbare Domains\n"
            + "\n".join(gestoert)
            + "\nIst das ein Fehlalarm (z. B. Dienst auf anderem Port oder Pfad), biete an, die "
            "Prüfung anzupassen („Soll ich das künftig auf 8443 prüfen?“) – "
            "erreichbarkeit_pruefung_aendern."
        )


def erreichbarkeit_werkzeuge(waechter: ErreichbarkeitsWaechter) -> list[Werkzeug]:
    def anzeigen() -> str:
        return waechter.anzeigen()

    def anlegen(
        ziel: str,
        port: int | str | None = None,
        pfad: str | None = None,
        intervall_minuten: int | str | None = None,
        stufe: str | None = None,
        notiz: str | None = None,
    ) -> str:
        pruefung, neu = waechter.anlegen(
            ziel_verstehen(ziel, port, pfad),
            intervall_minuten=intervall_verstehen(intervall_minuten)
            if intervall_minuten not in (None, "")
            else None,
            stufe=stufe_verstehen(stufe) if stufe else None,
            notiz=notiz_verstehen(notiz) if notiz is not None else None,
        )
        if neu:
            text = f"Prüfung angelegt: {pruefung.zeile()}. Die erste Prüfung läuft gleich."
        else:
            text = f"Diese Prüfung gab es schon – aktualisiert: {pruefung.zeile()}."
            if pruefung.pausiert:
                text += " Sie ist pausiert (fortsetzen mit erreichbarkeit_pruefung_pausieren)."
        return text

    def aendern(
        pruefung: int | str,
        port: int | str | None = None,
        pfad: str | None = None,
        intervall_minuten: int | str | None = None,
        stufe: str | None = None,
        notiz: str | None = None,
    ) -> str:
        alt = waechter.finden(pruefung)
        try:
            neuer_port = port_pruefen(port) if port not in (None, "") else None
            neuer_pfad = pfad_pruefen(pfad) if pfad not in (None, "") else None
        except ZielFehler as fehler:
            raise WerkzeugFehler(str(fehler)) from fehler
        neu = waechter.aendern(
            alt,
            port=neuer_port,
            pfad=neuer_pfad,
            intervall_minuten=intervall_verstehen(intervall_minuten)
            if intervall_minuten not in (None, "")
            else None,
            stufe=stufe_verstehen(stufe) if stufe else None,
            notiz=notiz_verstehen(notiz) if notiz is not None else None,
        )
        text = f"Prüfung geändert: {neu.zeile()}."
        if neu.ziel != alt.ziel:
            text += f" (vorher {alt.schluessel}; gilt sofort, ohne Neustart)"
        return text

    def pausieren(pruefung: int | str, pausiert: bool | str = True) -> str:
        if isinstance(pausiert, str):
            pausiert = pausiert.strip().casefold() not in ("false", "nein", "0", "fortsetzen")
        neu = waechter.pausieren(waechter.finden(pruefung), bool(pausiert))
        if neu.pausiert:
            return f"Prüfung pausiert: {neu.zeile()}. Ich melde dafür nichts, bis du sie fortsetzt."
        return f"Prüfung läuft wieder: {neu.zeile()}."

    def vorschau_loeschen(pruefung: int | str) -> str:
        return f"Die Erreichbarkeits-Prüfung {waechter.finden(pruefung).zeile()} wird gelöscht."

    def loeschen(pruefung: int | str) -> str:
        weg = waechter.loeschen(waechter.finden(pruefung))
        return f"Prüfung gelöscht: {weg.zeile()}."

    pruefung_parameter = {
        "type": "string",
        "description": "Nummer der Prüfung (z. B. 2) oder Domain (z. B. git oder "
        "cloud.example.ch:8443)",
    }
    return [
        Werkzeug(
            "erreichbarkeit_pruefen",
            "Prüft sofort, ob die eigenen Domains (z. B. cloud., git., ki.) von außen erreichbar "
            "sind: DNS, HTTPS-Antwort und gültiges Zertifikat, je Prüfung auf ihrem Port und "
            "Pfad. Ohne Angabe alle aktiven Prüfungen, sonst nur eine (Nummer oder Domain).",
            {
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "z. B. git"}},
            },
            waechter.jetzt_pruefen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "erreichbarkeit_pruefungen_anzeigen",
            "Zeigt alle Erreichbarkeits-Prüfungen („Welche Prüfungen hast du?“, „Was prüfst du "
            "von außen?“): Nummer, Ziel mit Port und Pfad, Intervall, Stufe, ob pausiert und das "
            "letzte Ergebnis.",
            {"type": "object", "properties": {}},
            anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "erreichbarkeit_pruefung_anlegen",
            "Legt eine neue Erreichbarkeits-Prüfung an („Prüf cloud.example.ch auf Port 8443“, "
            "„Überwach auch ki.example.ch/health“). Gibt es das Ziel schon, wird es aktualisiert "
            "statt doppelt angelegt. Nur öffentliche Domains, keine IP-Adressen.",
            {
                "type": "object",
                "properties": {
                    "ziel": {
                        "type": "string",
                        "description": "Domain, optional mit :port und /pfad, z. B. "
                        "cloud.example.ch:8443 oder ki.example.ch/health",
                    },
                    "port": {"type": "integer", "description": "Port (Standard 443)"},
                    "pfad": {"type": "string", "description": "Pfad (Standard /), z. B. /health"},
                    "intervall_minuten": {
                        "type": "integer",
                        "description": "So oft prüfen, 1 bis 1440 Minuten (Standard aus der "
                        "Konfiguration, meist 5)",
                    },
                    "stufe": {
                        "type": "string",
                        "description": "Stufe für einen Ausfall: info, wichtig oder notfall",
                    },
                    "notiz": {"type": "string", "description": "Kurze Notiz, optional"},
                },
                "required": ["ziel"],
            },
            anlegen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "erreichbarkeit_pruefung_aendern",
            "Ändert eine bestehende Erreichbarkeits-Prüfung dauerhaft und ohne Neustart: Port, "
            "Pfad, Intervall, Stufe oder Notiz. Für „prüf das lieber auf 8443“, „prüf git nur "
            "noch jede Stunde“, „nimm /health statt /“, „melde das nur als info“ – auch wenn "
            "Louis einen Fehlalarm meldet.",
            {
                "type": "object",
                "properties": {
                    "pruefung": pruefung_parameter,
                    "port": {"type": "integer", "description": "neuer Port, z. B. 8443"},
                    "pfad": {"type": "string", "description": "neuer Pfad, z. B. /health"},
                    "intervall_minuten": {
                        "type": "integer",
                        "description": "neues Intervall, 1 bis 1440 Minuten (60 = jede Stunde)",
                    },
                    "stufe": {"type": "string", "description": "info, wichtig oder notfall"},
                    "notiz": {"type": "string"},
                },
                "required": ["pruefung"],
            },
            aendern,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "erreichbarkeit_pruefung_pausieren",
            "Pausiert eine Erreichbarkeits-Prüfung (z. B. während einer Wartung) oder setzt sie "
            "fort (pausiert=false). Eine pausierte Prüfung läuft nicht und meldet nichts.",
            {
                "type": "object",
                "properties": {
                    "pruefung": pruefung_parameter,
                    "pausiert": {
                        "type": "boolean",
                        "description": "true = pausieren (Standard), false = fortsetzen",
                    },
                },
                "required": ["pruefung"],
            },
            pausieren,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "erreichbarkeit_pruefung_loeschen",
            "Löscht eine Erreichbarkeits-Prüfung ganz (nur nach Louis' ausdrücklichem Ja). Zum "
            "vorübergehenden Abschalten lieber pausieren.",
            {
                "type": "object",
                "properties": {"pruefung": pruefung_parameter},
                "required": ["pruefung"],
            },
            loeschen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=vorschau_loeschen,
        ),
    ]


__all__ = [
    "AUSFALL_NACH",
    "MAX_PRUEFUNGEN",
    "TAKT",
    "ErreichbarkeitsWaechter",
    "Pruefung",
    "erreichbarkeit_werkzeuge",
    "intervall_verstehen",
]
