"""Server-Alarme: Regeln per Gespräch, Hinweise auf Auffälliges und aktuelle Messwerte.

Der :class:`ServerWaechter` holt regelmäßig (Standard: jede Minute) die Messwerte aller
Quellen (eigenes Monitoring-Tool, Homepage-Dashboard, Proxmox – alles nur lesend) und

1. prüft die **Regeln**, die Louis im Gespräch festlegt („Bei Auslastung über 90 %
   länger als 5 Minuten: Notfall“). Gespeichert werden Ziel, Messwert, Richtung,
   Grenze, Dauer und Alarm-Stufe (Tabelle ``alarm_regeln``). Ist der Wert wieder
   normal, kommt ein kurzer Hinweis.
2. merkt sich den **üblichen Verlauf** (Mittel je Stunde und Tag, Tabelle
   ``messwert_stunden``) und meldet einen ungewöhnlichen Anstieg schon *unter* jeder
   Grenze: deutlich über dem 7-Tage-Mittel derselben Tageszeit → ``info``, sehr
   deutlich → ``wichtig``.
3. meldet, wenn eine Quelle länger nicht antwortet.

Gemeldet wird über :class:`~missa.core.alarme.Alarme` – mit Ruhezeit und Ruhefenster,
damit derselbe Alarm nicht im Minutentakt kommt.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe, stufe_verstehen
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.monitoring import (
    METRIK_EINHEITEN,
    Messwert,
    Quelle,
    QuellenFehler,
    metrik_titel,
    metrik_verstehen,
    wert_text,
    zahl_lesen,
    ziel_normal,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_regeln (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ziel TEXT NOT NULL,
    metrik TEXT NOT NULL,
    richtung TEXT NOT NULL CHECK (richtung IN ('ueber', 'unter')),
    grenze REAL NOT NULL,
    dauer_minuten REAL NOT NULL,
    stufe TEXT NOT NULL CHECK (stufe IN ('info', 'wichtig', 'notfall')),
    ruhefenster_minuten INTEGER,
    erstellt TEXT NOT NULL,
    geaendert TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messwert_stunden (
    ziel TEXT NOT NULL,
    metrik TEXT NOT NULL,
    tag TEXT NOT NULL,
    stunde INTEGER NOT NULL,
    anzahl INTEGER NOT NULL,
    summe REAL NOT NULL,
    PRIMARY KEY (ziel, metrik, tag, stunde)
);
CREATE INDEX IF NOT EXISTS messwert_stunden_tag ON messwert_stunden (tag);
"""

ALLE = "*"
ALLE_WOERTER = frozenset({"*", "alle", "jede", "jeder", "jedes", "alles", "überall", "ueberall"})
UEBER_WOERTER = ("über", "ueber", ">", "mehr", "höher", "hoeher", "oberhalb", "above", "größer")
UNTER_WOERTER = ("unter", "<", "weniger", "tiefer", "niedriger", "unterhalb", "below", "kleiner")

#: Über so viele Tage wird der übliche Verlauf gemittelt (ohne heute).
VERLAUF_TAGE = 7
#: Stundenmittel werden so lange aufbewahrt.
AUFBEWAHREN_TAGE = 30
#: Für den aktuellen Wert einer Auffälligkeit zählt das Mittel dieser Zeit …
AKTUELL_FENSTER = timedelta(minutes=5)
#: … aus mindestens so vielen Messungen (kurze Spitzen lösen nichts aus).
MIN_PROBEN = 3
#: Dieselbe Auffälligkeit kommt frühestens nach dieser Zeit wieder (außer sie wird dringender).
AUFFAELLIG_RUHEFENSTER = timedelta(hours=6)
#: Antwortet eine Quelle so lange nicht, gibt es einen Hinweis …
QUELLE_STILL = timedelta(minutes=15)
#: … höchstens einmal in dieser Zeit.
QUELLE_RUHEFENSTER = timedelta(hours=12)
#: Werte, die älter sind, holt server_werte_anzeigen neu.
WERTE_VERALTET = timedelta(minutes=3)


def richtung_verstehen(text: Any) -> str:
    roh = str(text or "über").casefold().strip()
    if any(roh.startswith(w) or w in roh.split() for w in UNTER_WOERTER):
        return "unter"
    if any(roh.startswith(w) or w in roh.split() for w in UEBER_WOERTER):
        return "ueber"
    raise WerkzeugFehler(f"„{text}“ verstehe ich nicht als Richtung – „über“ oder „unter“?")


def grenze_verstehen(wert: Any) -> float:
    zahl = zahl_lesen(wert)
    if zahl is None:
        raise WerkzeugFehler(f"„{wert}“ ist keine Grenze, die ich verstehe (z. B. 90 für 90 %).")
    return zahl


def dauer_verstehen(wert: Any, standard: float) -> float:
    if wert in (None, ""):
        return standard
    zahl = zahl_lesen(wert)
    if zahl is None or zahl < 0 or zahl > 24 * 60:
        raise WerkzeugFehler("dauer_minuten muss eine Zahl zwischen 0 und 1440 sein.")
    return zahl


def ziel_verstehen(text: Any) -> str:
    ziel = ziel_normal(text)
    if not ziel:
        raise WerkzeugFehler("Für welchen Server gilt die Regel? Das Ziel fehlt (z. B. ai01).")
    return ALLE if ziel in ALLE_WOERTER else ziel


def _minuten_text(minuten: float) -> str:
    if minuten <= 0:
        return "sofort"
    if minuten == 1:
        return "länger als 1 Minute"
    return f"länger als {minuten:g} Minuten"


@dataclass(frozen=True)
class Regel:
    """Eine Alarm-Regel: Ziel, Messwert, Richtung, Grenze, Dauer und Stufe."""

    id: int
    ziel: str
    metrik: str
    richtung: str
    grenze: float
    dauer_minuten: float
    stufe: AlarmStufe
    ruhefenster_minuten: int | None = None

    @property
    def einheit(self) -> str:
        return METRIK_EINHEITEN.get(self.metrik, "")

    def passt(self, wert: Messwert) -> bool:
        return wert.metrik == self.metrik and self.ziel in (ALLE, wert.ziel)

    def verletzt(self, zahl: float) -> bool:
        return zahl > self.grenze if self.richtung == "ueber" else zahl < self.grenze

    def bedingung(self, ziel: str | None = None) -> str:
        wer = ziel or ("alle Server" if self.ziel == ALLE else self.ziel)
        if self.metrik in ("laeuft", "erreichbar") and self.richtung == "unter":
            return f"{wer} {'läuft nicht' if self.metrik == 'laeuft' else 'nicht erreichbar'}"
        grenze = f"{self.grenze:g}" + (f" {self.einheit}" if self.einheit else "")
        richtung = "über" if self.richtung == "ueber" else "unter"
        return f"{wer}: {metrik_titel(self.metrik)} {richtung} {grenze}"

    def zeile(self) -> str:
        zeile = f"#{self.id}: {self.bedingung()} {_minuten_text(self.dauer_minuten)} → {self.stufe}"
        if self.ruhefenster_minuten:
            zeile += f" (wiederholen frühestens nach {self.ruhefenster_minuten} Minuten)"
        return zeile

    def als_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ziel": self.ziel,
            "messwert": self.metrik,
            "richtung": self.richtung,
            "grenze": self.grenze,
            "einheit": self.einheit,
            "dauer_minuten": self.dauer_minuten,
            "stufe": str(self.stufe),
            "ruhefenster_minuten": self.ruhefenster_minuten,
            "text": self.zeile(),
        }


class ServerWaechter:
    """Misst regelmäßig, prüft Regeln und meldet Auffälliges."""

    def __init__(
        self,
        quellen: Iterable[Quelle],
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        *,
        dauer_minuten: float = 5,
        auffaellig_aktiv: bool = True,
        auffaellig_min_abstand: float = 15.0,
        min_tage: int = 3,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        #: Liefern zwei Quellen denselben Wert, gilt die erste in dieser Liste.
        self.quellen = list(quellen)
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self.standard_dauer = max(0.0, float(dauer_minuten))
        self.auffaellig_aktiv = auffaellig_aktiv
        self.min_abstand = max(1.0, float(auffaellig_min_abstand))
        self.min_tage = max(1, int(min_tage))
        self._uhr = uhr or alarme.jetzt
        self.aktuell: dict[tuple[str, str], Messwert] = {}
        self.zuletzt_gemessen: datetime | None = None
        self.quellen_fehler: dict[str, str] = {}
        self._verlauf: dict[tuple[str, str], deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=60)
        )
        self._verletzt_seit: dict[tuple[int, str], datetime] = {}
        self._ausgeloest: dict[tuple[int, str], datetime] = {}
        self._fehler_seit: dict[str, datetime] = {}
        self._fehler_gemeldet: set[str] = set()
        self._aufgeraeumt: date | None = None
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Regeln speichern

    @staticmethod
    def _regel(zeile: Any) -> Regel:
        return Regel(
            id=zeile["id"],
            ziel=zeile["ziel"],
            metrik=zeile["metrik"],
            richtung=zeile["richtung"],
            grenze=zeile["grenze"],
            dauer_minuten=zeile["dauer_minuten"],
            stufe=AlarmStufe(zeile["stufe"]),
            ruhefenster_minuten=zeile["ruhefenster_minuten"],
        )

    def regeln(self) -> list[Regel]:
        with verbinden(self.datei) as db:
            zeilen = db.execute("SELECT * FROM alarm_regeln ORDER BY id").fetchall()
        return [self._regel(z) for z in zeilen]

    def regel(self, regel_id: Any) -> Regel:
        try:
            nummer = int(str(regel_id).strip().lstrip("#"))
        except ValueError as fehler:
            raise WerkzeugFehler(f"regel_id „{regel_id}“ ist keine Zahl.") from fehler
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT * FROM alarm_regeln WHERE id = ?", (nummer,)).fetchone()
        if zeile is None:
            raise WerkzeugFehler(
                f"Eine Alarm-Regel #{nummer} gibt es nicht. Zeig sie mit alarmregeln_anzeigen an."
            )
        return self._regel(zeile)

    def regel_anlegen(
        self,
        ziel: str,
        metrik: str,
        richtung: str,
        grenze: float,
        dauer_minuten: float,
        stufe: AlarmStufe,
        ruhefenster_minuten: int | None = None,
    ) -> tuple[Regel, bool]:
        """Speichert eine Regel und gibt (Regel, neu?) zurück – nie doppelt."""
        jetzt = self.jetzt().isoformat(timespec="seconds")
        with verbinden(self.datei) as db:
            vorhanden = db.execute(
                "SELECT * FROM alarm_regeln WHERE ziel = ? AND metrik = ? AND richtung = ? "
                "AND grenze = ?",
                (ziel, metrik, richtung, grenze),
            ).fetchone()
            if vorhanden is not None:
                db.execute(
                    "UPDATE alarm_regeln SET dauer_minuten = ?, stufe = ?, "
                    "ruhefenster_minuten = COALESCE(?, ruhefenster_minuten), geaendert = ? "
                    "WHERE id = ?",
                    (dauer_minuten, str(stufe), ruhefenster_minuten, jetzt, vorhanden["id"]),
                )
                nummer, neu = vorhanden["id"], False
            else:
                cursor = db.execute(
                    "INSERT INTO alarm_regeln (ziel, metrik, richtung, grenze, dauer_minuten, "
                    "stufe, ruhefenster_minuten, erstellt, geaendert) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ziel,
                        metrik,
                        richtung,
                        grenze,
                        dauer_minuten,
                        str(stufe),
                        ruhefenster_minuten,
                        jetzt,
                        jetzt,
                    ),
                )
                nummer, neu = int(cursor.lastrowid or 0), True
        self._zustand_vergessen(nummer)
        log.info("Alarm-Regel #%s %s", nummer, "angelegt" if neu else "aktualisiert")
        return self.regel(nummer), neu

    def regel_aendern(self, regel_id: Any, **felder: Any) -> Regel:
        alt = self.regel(regel_id)
        felder = {k: v for k, v in felder.items() if v is not None}
        if not felder:
            raise WerkzeugFehler("Was soll ich an der Regel ändern? Es fehlen neue Angaben.")
        if "stufe" in felder:
            felder["stufe"] = str(felder["stufe"])
        felder["geaendert"] = self.jetzt().isoformat(timespec="seconds")
        zuweisung = ", ".join(f"{name} = ?" for name in felder)
        with verbinden(self.datei) as db:
            db.execute(
                f"UPDATE alarm_regeln SET {zuweisung} WHERE id = ?", (*felder.values(), alt.id)
            )
        self._zustand_vergessen(alt.id)
        return self.regel(alt.id)

    def regel_loeschen(self, regel_id: Any) -> Regel:
        alt = self.regel(regel_id)
        with verbinden(self.datei) as db:
            db.execute("DELETE FROM alarm_regeln WHERE id = ?", (alt.id,))
        self._zustand_vergessen(alt.id)
        log.info("Alarm-Regel #%s gelöscht", alt.id)
        return alt

    def _zustand_vergessen(self, regel_id: int) -> None:
        for zustand in (self._verletzt_seit, self._ausgeloest):
            for schluessel in [s for s in zustand if s[0] == regel_id]:
                del zustand[schluessel]

    # -------------------------------------------------------------- Messen

    async def _eine_quelle(self, quelle: Quelle) -> tuple[str, list[Messwert], str | None]:
        try:
            return quelle.name, await quelle.messwerte(), None
        except QuellenFehler as fehler:
            return quelle.name, [], str(fehler)
        except Exception as fehler:
            log.exception("Monitoring-Quelle %s: unerwarteter Fehler", quelle.name)
            return quelle.name, [], f"unerwarteter Fehler: {fehler or type(fehler).__name__}"

    async def messen(self) -> list[Messwert]:
        """Holt alle Quellen gleichzeitig; je Ziel und Messwert zählt die erste Quelle."""
        ergebnisse = await asyncio.gather(*(self._eine_quelle(q) for q in self.quellen))
        jetzt = self.jetzt()
        werte: dict[tuple[str, str], Messwert] = {}
        self.quellen_fehler = {}
        for name, liste, fehler in ergebnisse:
            if fehler is not None:
                log.warning("Monitoring-Quelle %s: %s", name, fehler)
                self.quellen_fehler[name] = fehler
            for wert in liste:
                werte.setdefault(wert.schluessel, wert)
        for wert in werte.values():
            self.aktuell[wert.schluessel] = wert
            self._verlauf[wert.schluessel].append((jetzt, wert.wert))
        self.zuletzt_gemessen = jetzt
        return list(werte.values())

    async def pruefen(self) -> list[Alarm]:
        """Regelmäßiger Auftrag: messen, Verlauf speichern, Regeln und Auffälliges prüfen."""
        werte = await self.messen()
        jetzt = self.jetzt()
        self._speichern(werte, jetzt)
        return [
            *self.quellen_pruefen(jetzt),
            *self.regeln_pruefen(werte, jetzt),
            *self.auffaelligkeiten_pruefen(werte, jetzt),
        ]

    # -------------------------------------------------------------- Regeln prüfen

    def regeln_pruefen(self, werte: list[Messwert], jetzt: datetime) -> list[Alarm]:
        gemeldet: list[Alarm] = []
        regeln = self.regeln()
        ids = {r.id for r in regeln}
        for zustand in (self._verletzt_seit, self._ausgeloest):
            for schluessel in [s for s in zustand if s[0] not in ids]:
                del zustand[schluessel]
        for regel in regeln:
            for wert in werte:
                if not regel.passt(wert):
                    continue
                schluessel = (regel.id, wert.ziel)
                ruhefenster = (
                    timedelta(minutes=regel.ruhefenster_minuten)
                    if regel.ruhefenster_minuten
                    else None
                )
                if regel.verletzt(wert.wert):
                    seit = self._verletzt_seit.setdefault(schluessel, jetzt)
                    if jetzt - seit < timedelta(minutes=regel.dauer_minuten):
                        continue
                    self._ausgeloest.setdefault(schluessel, seit)
                    alarm = self.alarme.melden(
                        regel.stufe,
                        regel.bedingung(wert.ziel),
                        f"Seit {seit:%H:%M} Uhr, aktuell {wert_text(wert.wert, wert.einheit)} "
                        f"(Regel #{regel.id}, Quelle {wert.quelle or '?'}).",
                        schluessel=f"regel:{regel.id}:{wert.ziel}",
                        quelle=wert.quelle,
                        ruhefenster=ruhefenster,
                    )
                else:
                    self._verletzt_seit.pop(schluessel, None)
                    seit = self._ausgeloest.pop(schluessel, None)
                    if seit is None:
                        continue
                    alarm = self.alarme.melden(
                        AlarmStufe.INFO,
                        f"{regel.bedingung(wert.ziel)} – wieder normal",
                        f"Aktuell {wert_text(wert.wert, wert.einheit)}; der Alarm lief von "
                        f"{seit:%H:%M} bis {jetzt:%H:%M} Uhr (Regel #{regel.id}).",
                        schluessel=f"regel:{regel.id}:{wert.ziel}:normal",
                        quelle=wert.quelle,
                        ruhefenster=ruhefenster,
                    )
                if alarm is not None:
                    gemeldet.append(alarm)
        return gemeldet

    # -------------------------------------------------------------- Auffälligkeiten

    def _speichern(self, werte: list[Messwert], jetzt: datetime) -> None:
        """Stundenmittel je Tag für den üblichen Verlauf (nur Prozentwerte)."""
        tag, stunde = jetzt.date().isoformat(), jetzt.hour
        zeilen = [(w.ziel, w.metrik, tag, stunde, w.wert) for w in werte if w.einheit == "%"]
        with verbinden(self.datei) as db:
            db.executemany(
                "INSERT INTO messwert_stunden (ziel, metrik, tag, stunde, anzahl, summe) "
                "VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT (ziel, metrik, tag, stunde) DO UPDATE "
                "SET anzahl = anzahl + 1, summe = summe + excluded.summe",
                zeilen,
            )
            if self._aufgeraeumt != jetzt.date():
                grenze = (jetzt.date() - timedelta(days=AUFBEWAHREN_TAGE)).isoformat()
                db.execute("DELETE FROM messwert_stunden WHERE tag < ?", (grenze,))
                self._aufgeraeumt = jetzt.date()

    def grundlinie(
        self, ziel: str, metrik: str, jetzt: datetime
    ) -> tuple[float, float, int] | None:
        """Üblicher Wert zu dieser Stunde: (Mittel, Streuung, Tage) der letzten 7 Tage."""
        heute = jetzt.date()
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT summe / anzahl AS mittel FROM messwert_stunden WHERE ziel = ? "
                "AND metrik = ? AND stunde = ? AND tag >= ? AND tag < ? AND anzahl > 0",
                (
                    ziel,
                    metrik,
                    jetzt.hour,
                    (heute - timedelta(days=VERLAUF_TAGE)).isoformat(),
                    heute.isoformat(),
                ),
            ).fetchall()
        mittel = [z["mittel"] for z in zeilen]
        if len(mittel) < self.min_tage:
            return None
        return statistics.fmean(mittel), statistics.pstdev(mittel), len(mittel)

    def auffaelligkeiten_pruefen(self, werte: list[Messwert], jetzt: datetime) -> list[Alarm]:
        if not self.auffaellig_aktiv:
            return []
        regeln = {r.id: r for r in self.regeln()}
        laufend = {(z, regeln[rid].metrik) for rid, z in self._ausgeloest if rid in regeln}
        gemeldet: list[Alarm] = []
        for wert in werte:
            if wert.einheit != "%" or (wert.ziel, wert.metrik) in laufend:
                continue
            verlauf = self._verlauf[wert.schluessel]
            juengste = [w for z, w in verlauf if jetzt - z <= AKTUELL_FENSTER]
            if len(juengste) < MIN_PROBEN:
                continue
            grund = self.grundlinie(wert.ziel, wert.metrik, jetzt)
            if grund is None:
                continue
            mittel, streuung, tage = grund
            aktuell = statistics.fmean(juengste)
            abstand = aktuell - mittel
            if abstand >= max(2 * self.min_abstand, 5 * streuung):
                stufe = AlarmStufe.WICHTIG
            elif abstand >= max(self.min_abstand, 3 * streuung):
                stufe = AlarmStufe.INFO
            else:
                continue
            alarm = self.alarme.melden(
                stufe,
                f"{wert.ziel}: {metrik_titel(wert.metrik)} ungewöhnlich hoch",
                f"Gerade {aktuell:.0f} %, üblich um diese Zeit sind etwa {mittel:.0f} % "
                f"(Mittel der letzten {tage} Tage). Noch keine Alarm-Grenze erreicht – "
                "nur damit du es weißt.",
                schluessel=f"auffaellig:{wert.ziel}:{wert.metrik}",
                quelle=wert.quelle,
                ruhefenster=max(self.alarme.ruhefenster(), AUFFAELLIG_RUHEFENSTER),
            )
            if alarm is not None:
                gemeldet.append(alarm)
        return gemeldet

    # -------------------------------------------------------------- Quellen

    def quellen_pruefen(self, jetzt: datetime) -> list[Alarm]:
        gemeldet: list[Alarm] = []
        for name, fehler in self.quellen_fehler.items():
            seit = self._fehler_seit.setdefault(name, jetzt)
            if jetzt - seit < QUELLE_STILL:
                continue
            alarm = self.alarme.melden(
                AlarmStufe.INFO,
                f"Monitoring-Quelle {name} antwortet nicht",
                f"Seit {seit:%H:%M} Uhr: {fehler}. Solange fehlen mir diese Werte.",
                schluessel=f"quelle:{name}",
                quelle=name,
                ruhefenster=QUELLE_RUHEFENSTER,
            )
            if alarm is not None:
                self._fehler_gemeldet.add(name)
                gemeldet.append(alarm)
        for name in [n for n in self._fehler_seit if n not in self.quellen_fehler]:
            del self._fehler_seit[name]
            if name in self._fehler_gemeldet:
                self._fehler_gemeldet.discard(name)
                alarm = self.alarme.melden(
                    AlarmStufe.INFO,
                    f"Monitoring-Quelle {name} antwortet wieder",
                    schluessel=f"quelle:{name}:wieder",
                    quelle=name,
                )
                if alarm is not None:
                    gemeldet.append(alarm)
        return gemeldet

    # -------------------------------------------------------------- Anzeigen

    def werte_text(self, ziel: str | None = None) -> str:
        gesucht = ziel_normal(ziel) if ziel else ""
        nach_ziel: dict[str, list[Messwert]] = defaultdict(list)
        for wert in self.aktuell.values():
            if not gesucht or wert.ziel == gesucht:
                nach_ziel[wert.ziel].append(wert)
        zeilen = []
        if self.zuletzt_gemessen is not None:
            zeilen.append(f"Messwerte (Stand {self.zuletzt_gemessen:%H:%M:%S} Uhr):")
        for name in sorted(nach_ziel):
            liste = sorted(nach_ziel[name], key=lambda w: w.metrik)
            quellen = ", ".join(sorted({w.quelle for w in liste if w.quelle}))
            zeilen.append(f"- {name}: " + ", ".join(w.text() for w in liste) + f" ({quellen})")
        if not nach_ziel:
            bekannt = ", ".join(sorted({w.ziel for w in self.aktuell.values()})) or "keine"
            zeilen.append(
                f"Für „{ziel}“ habe ich keine Werte. Bekannte Ziele: {bekannt}."
                if gesucht
                else "Ich habe noch keine Messwerte."
            )
        for name, fehler in self.quellen_fehler.items():
            zeilen.append(f"- Quelle {name} antwortet nicht: {fehler}")
        aktiv = self.aktive_alarme()
        if aktiv:
            zeilen.append("Aktive Alarme: " + "; ".join(aktiv))
        return "\n".join(zeilen)

    def aktive_alarme(self) -> list[str]:
        regeln = {r.id: r for r in self.regeln()}
        return [
            f"{regeln[rid].bedingung(ziel)} seit {seit:%H:%M} Uhr ({regeln[rid].stufe})"
            for (rid, ziel), seit in sorted(self._ausgeloest.items(), key=lambda e: e[1])
            if rid in regeln
        ]

    def kontext(self, _frage: str) -> str | None:
        """Kontextquelle: aktive Server-Alarme im Systemprompt."""
        aktiv = self.aktive_alarme()
        if not aktiv:
            return None
        return "## Aktive Server-Alarme\n" + "\n".join(f"- {a}" for a in aktiv)

    def bekannte_ziele(self) -> list[str]:
        return sorted({w.ziel for w in self.aktuell.values()})


# ----------------------------------------------------------------------------- Werkzeuge


def server_werkzeuge(waechter: ServerWaechter) -> list[Werkzeug]:
    alarme = waechter.alarme

    async def werte_anzeigen(ziel: str | None = None) -> str:
        if (
            waechter.zuletzt_gemessen is None
            or waechter.jetzt() - waechter.zuletzt_gemessen > WERTE_VERALTET
        ):
            await waechter.messen()
        return waechter.werte_text(ziel)

    def anlegen(
        ziel: str,
        grenze: float | str,
        messwert: str = "cpu",
        richtung: str = "über",
        dauer_minuten: float | str | None = None,
        stufe: str = "wichtig",
    ) -> str:
        regel, neu = waechter.regel_anlegen(
            ziel_verstehen(ziel),
            metrik_verstehen(messwert),
            richtung_verstehen(richtung),
            grenze_verstehen(grenze),
            dauer_verstehen(dauer_minuten, waechter.standard_dauer),
            stufe_verstehen(stufe),
        )
        zeilen = [f"Alarm-Regel {'gespeichert' if neu else 'aktualisiert'}: {regel.zeile()}"]
        passend = [w for w in waechter.aktuell.values() if regel.passt(w)]
        if passend:
            zeilen.append("Aktuell: " + ", ".join(f"{w.ziel} {w.text()}" for w in passend))
        elif waechter.aktuell:
            zeilen.append(
                f"ACHTUNG: Für „{regel.ziel}“ / {metrik_titel(regel.metrik)} habe ich gerade "
                f"keine Werte. Bekannte Ziele: {', '.join(waechter.bekannte_ziele())}. "
                "Frag Louis, ob der Name stimmt."
            )
        if regel.stufe is AlarmStufe.NOTFALL:
            zeilen.append(
                "Notfälle melde ich sofort, auch in der Ruhezeit. Ein Telefon-Anruf ist noch "
                "nicht eingerichtet – bis dahin kommt der Notfall deutlich markiert per Talk. "
                "Sag Louis das ehrlich."
            )
        if not alarme.zeitplaner.zustellung.verfuegbar:
            zeilen.append(
                "ACHTUNG: Gerade gibt es keinen Kanal, über den ich Alarme schicken kann "
                "(NC_TALK_NOTIFY_CONVERSATION fehlt) – sag Louis das."
            )
        return "\n".join(zeilen)

    def anzeigen() -> str:
        regeln = waechter.regeln()
        zeilen = ["Alarm-Regeln:", *([f"- {r.zeile()}" for r in regeln] or ["- (keine)"])]
        aktiv = waechter.aktive_alarme()
        if aktiv:
            zeilen.append("Gerade aktiv: " + "; ".join(aktiv))
        zeilen.append(
            "Zusätzlich melde ich ungewöhnliche Anstiege unter den Grenzen (info/wichtig)."
            if waechter.auffaellig_aktiv
            else "Hinweise auf ungewöhnliche Anstiege sind ausgeschaltet."
        )
        zeilen.append(f"Ruhezeit: {alarme.ruhezeit_text()}.")
        return "\n".join(zeilen)

    def aendern(
        regel_id: int | str,
        grenze: float | str | None = None,
        richtung: str | None = None,
        dauer_minuten: float | str | None = None,
        stufe: str | None = None,
        ziel: str | None = None,
        messwert: str | None = None,
        wiederholen_nach_minuten: int | None = None,
    ) -> str:
        regel = waechter.regel_aendern(
            regel_id,
            grenze=grenze_verstehen(grenze) if grenze not in (None, "") else None,
            richtung=richtung_verstehen(richtung) if richtung else None,
            dauer_minuten=dauer_verstehen(dauer_minuten, 0)
            if dauer_minuten not in (None, "")
            else None,
            stufe=stufe_verstehen(stufe) if stufe else None,
            ziel=ziel_verstehen(ziel) if ziel else None,
            metrik=metrik_verstehen(messwert) if messwert else None,
            ruhefenster_minuten=max(1, int(wiederholen_nach_minuten))
            if wiederholen_nach_minuten
            else None,
        )
        return f"Alarm-Regel geändert: {regel.zeile()}"

    def vorschau_loeschen(regel_id: int | str) -> str:
        return f"Die Alarm-Regel {waechter.regel(regel_id).zeile()} wird gelöscht."

    def loeschen(regel_id: int | str) -> str:
        regel = waechter.regel_loeschen(regel_id)
        return f"Alarm-Regel gelöscht: {regel.zeile()}"

    return [
        Werkzeug(
            "server_werte_anzeigen",
            "Zeigt die aktuellen Messwerte der Server (CPU, RAM, Disk, läuft …) aus Monitoring, "
            "Homepage und Proxmox, dazu aktive Alarme. Optional nur für ein Ziel wie ai01.",
            {
                "type": "object",
                "properties": {"ziel": {"type": "string", "description": "z. B. ai01"}},
            },
            werte_anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "alarmregel_anlegen",
            "Speichert eine Alarm-Regel: Wenn ein Messwert eines Servers länger als dauer_minuten "
            "über (oder unter) der Grenze liegt, meldest du dich mit der Stufe. „Ruf mich an“, "
            "„weck mich“, „sofort“ = notfall; „warn mich“ = wichtig; „sag kurz Bescheid“ = info.",
            {
                "type": "object",
                "properties": {
                    "ziel": {"type": "string", "description": "Server, z. B. ai01, oder alle"},
                    "grenze": {"type": "number", "description": "z. B. 90 für 90 %"},
                    "messwert": {
                        "type": "string",
                        "description": "cpu (Auslastung, Standard), ram, disk, laeuft, "
                        "erreichbar oder ein anderer Name",
                    },
                    "richtung": {"type": "string", "description": "über (Standard) oder unter"},
                    "dauer_minuten": {
                        "type": "number",
                        "description": "So lange muss es anhalten (Standard 5)",
                    },
                    "stufe": {"type": "string", "description": "info, wichtig oder notfall"},
                },
                "required": ["ziel", "grenze"],
            },
            anlegen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "alarmregeln_anzeigen",
            "Listet alle Alarm-Regeln für die Server und welche gerade ausgelöst sind.",
            {"type": "object", "properties": {}},
            anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "alarmregel_aendern",
            "Ändert eine Alarm-Regel (Grenze, Richtung, Dauer, Stufe, Ziel, Messwert oder wie "
            "lange derselbe Alarm nicht wiederholt wird).",
            {
                "type": "object",
                "properties": {
                    "regel_id": {"type": "integer", "description": "Nummer der Regel"},
                    "grenze": {"type": "number"},
                    "richtung": {"type": "string", "description": "über oder unter"},
                    "dauer_minuten": {"type": "number"},
                    "stufe": {"type": "string", "description": "info, wichtig oder notfall"},
                    "ziel": {"type": "string"},
                    "messwert": {"type": "string"},
                    "wiederholen_nach_minuten": {"type": "integer"},
                },
                "required": ["regel_id"],
            },
            aendern,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "alarmregel_loeschen",
            "Löscht eine Alarm-Regel (nur nach Louis' ausdrücklichem Ja).",
            {
                "type": "object",
                "properties": {"regel_id": {"type": "integer", "description": "Nummer"}},
                "required": ["regel_id"],
            },
            loeschen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=vorschau_loeschen,
        ),
    ]


__all__ = ["Regel", "ServerWaechter", "server_werkzeuge"]
