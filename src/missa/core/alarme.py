"""Alarme mit Stufen (``info``, ``wichtig``, ``notfall``), Ruhezeiten und Ruhefenster.

Alles, was Missa von sich aus meldet, weil etwas auffällt (Server-Alarme, Ausfälle,
später Logins, Updates, Zertifikate), läuft über :meth:`Alarme.melden`:

- **Stufen** – ``info`` (gut zu wissen), ``wichtig`` (bald anschauen) und ``notfall``
  (sofort, „ruf mich an“). Solange es kein Telefon gibt, kommt auch ein Notfall über
  die vorhandenen Kanäle (Nextcloud Talk) – deutlich markiert.
- **Ruhezeiten** – in der Ruhezeit (Standard 22:00–07:00, per Gespräch änderbar)
  warten ``info`` und ``wichtig`` bis zum Ende der Ruhezeit. Ein ``notfall`` kommt
  immer sofort.
- **Ruhefenster** – derselbe Alarm (gleicher Schlüssel) kommt nicht im Minutentakt:
  Innerhalb des Ruhefensters (Standard 60 Minuten) wird er nicht wiederholt, außer er
  ist dringender geworden (z. B. ``info`` → ``wichtig``).

Zugestellt wird über den :class:`~missa.core.zeitplaner.Zeitplaner` (mit dessen
Wiederholversuchen); jeder gemeldete Alarm steht in der Tabelle ``alarme``. Alarme
laufen auch während eines Notfall-Stopps weiter – sie sind Nachrichten an Louis,
keine Aktionen.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.hintergrund import im_fenster
from missa.core.vorgaben import Vorgaben, uhrzeit_lesen
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.core.zeitangaben import wandzeit
from missa.core.zeitplaner import Zeitplaner

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarme (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeit TEXT NOT NULL,
    stufe TEXT NOT NULL CHECK (stufe IN ('info', 'wichtig', 'notfall')),
    schluessel TEXT NOT NULL,
    titel TEXT NOT NULL,
    text TEXT NOT NULL,
    quelle TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('gesendet', 'aufgeschoben')),
    zustellen_ab TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alarme_schluessel ON alarme (schluessel, zeit);
CREATE INDEX IF NOT EXISTS alarme_zeit ON alarme (zeit);
"""

RUHE_VON = "alarme.ruhezeit_von"
RUHE_BIS = "alarme.ruhezeit_bis"
RUHEFENSTER = "alarme.ruhefenster_minuten"
AUS = "aus"


class AlarmStufe(StrEnum):
    """Wie dringend ein Alarm ist."""

    INFO = "info"
    WICHTIG = "wichtig"
    NOTFALL = "notfall"

    @property
    def rang(self) -> int:
        return {"info": 0, "wichtig": 1, "notfall": 2}[self.value]

    @property
    def symbol(self) -> str:
        return {"info": "ℹ️", "wichtig": "⚠️", "notfall": "🚨"}[self.value]

    @property
    def titel(self) -> str:
        return {"info": "Hinweis", "wichtig": "Wichtig", "notfall": "NOTFALL"}[self.value]


#: Wörter, mit denen Louis eine Stufe meint („ruf mich an“ = Notfall).
STUFEN_WOERTER = {
    AlarmStufe.NOTFALL: (
        "notfall",
        "anrufen",
        "ruf",
        "anruf",
        "telefon",
        "weck",
        "wecken",
        "sofort",
        "dringend",
        "kritisch",
    ),
    AlarmStufe.WICHTIG: ("wichtig", "warnung", "warn", "warnen", "hoch", "bald"),
    AlarmStufe.INFO: ("info", "hinweis", "information", "leise", "nebenbei", "niedrig", "kurz"),
}


def stufe_verstehen(text: Any) -> AlarmStufe:
    """``"notfall"``, ``"ruf mich an"``, ``"wichtig"``, ``"Hinweis"`` … → :class:`AlarmStufe`."""
    if isinstance(text, AlarmStufe):
        return text
    woerter = re.findall(r"[a-zäöüß]+", str(text or "").casefold())
    for stufe in (AlarmStufe.NOTFALL, AlarmStufe.WICHTIG, AlarmStufe.INFO):
        if any(w.startswith(STUFEN_WOERTER[stufe]) for w in woerter):
            return stufe
    raise WerkzeugFehler(
        f"„{text}“ verstehe ich nicht als Alarm-Stufe. Möglich: info (Hinweis), wichtig, "
        "notfall („ruf mich an“)."
    )


def _utc_text(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@dataclass(frozen=True)
class Alarm:
    """Ein gemeldeter Alarm."""

    id: int
    zeit: datetime
    stufe: AlarmStufe
    schluessel: str
    titel: str
    text: str
    quelle: str
    status: str
    zustellen_ab: datetime

    def zeile(self) -> str:
        zeile = f"- {self.zeit:%d.%m. %H:%M} [{self.stufe}] {self.titel}"
        if self.status == "aufgeschoben":
            zeile += f" (Ruhezeit – zugestellt ab {self.zustellen_ab:%H:%M} Uhr)"
        return zeile

    def als_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "zeit": self.zeit.isoformat(timespec="seconds"),
            "stufe": str(self.stufe),
            "schluessel": self.schluessel,
            "titel": self.titel,
            "text": self.text,
            "quelle": self.quelle,
            "status": self.status,
            "zustellen_ab": self.zustellen_ab.isoformat(timespec="seconds"),
        }


class Alarme:
    """Meldet Alarme nach Stufe – mit Ruhezeit, Ruhefenster und dauerhaftem Verlauf."""

    def __init__(
        self,
        datei: Path,
        zone: ZoneInfo,
        zeitplaner: Zeitplaner,
        vorgaben: Vorgaben | None = None,
        *,
        ruhe_von: time | None = time(22, 0),
        ruhe_bis: time | None = time(7, 0),
        ruhefenster_minuten: int = 60,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.zone = zone
        self.zeitplaner = zeitplaner
        self.vorgaben = vorgaben or Vorgaben(datei)
        self.standard_ruhe = (
            (ruhe_von, ruhe_bis) if ruhe_von and ruhe_bis and ruhe_von != ruhe_bis else None
        )
        self.standard_ruhefenster = max(1, ruhefenster_minuten)
        self._uhr = uhr or zeitplaner.jetzt
        #: So viele Wiederholungen wurden seit dem Start im Ruhefenster zurückgehalten.
        self.unterdrueckt = 0
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Einstellungen

    def ruhezeit(self) -> tuple[time, time] | None:
        """Die Ruhezeit (von, bis) oder ``None``, wenn es keine gibt."""
        von = self.vorgaben.lesen(RUHE_VON, "")
        bis = self.vorgaben.lesen(RUHE_BIS, "")
        if not von and not bis:
            return self.standard_ruhe
        if AUS in (von, bis):
            return None
        try:
            zeiten = uhrzeit_lesen(von), uhrzeit_lesen(bis)
        except WerkzeugFehler:
            log.warning("Gespeicherte Ruhezeit %r–%r ist ungültig, nutze Standard", von, bis)
            return self.standard_ruhe
        return zeiten if zeiten[0] != zeiten[1] else None

    def ruhezeit_einstellen(self, von: time | None, bis: time | None) -> None:
        """Setzt die Ruhezeit; ``None`` (oder von = bis) schaltet sie aus."""
        if von is None or bis is None or von == bis:
            self.vorgaben.setzen(RUHE_VON, AUS)
            self.vorgaben.setzen(RUHE_BIS, AUS)
            return
        self.vorgaben.setzen(RUHE_VON, f"{von:%H:%M}")
        self.vorgaben.setzen(RUHE_BIS, f"{bis:%H:%M}")

    def ruhezeit_text(self) -> str:
        ruhe = self.ruhezeit()
        if ruhe is None:
            return "keine Ruhezeit (alles kommt sofort)"
        return f"{ruhe[0]:%H:%M}–{ruhe[1]:%H:%M} Uhr (Notfälle kommen trotzdem sofort)"

    def ruhefenster(self) -> timedelta:
        """So lange wird derselbe Alarm nicht wiederholt."""
        text = self.vorgaben.lesen(RUHEFENSTER, str(self.standard_ruhefenster))
        try:
            return timedelta(minutes=max(1, int(text)))
        except ValueError:
            return timedelta(minutes=self.standard_ruhefenster)

    def ruhefenster_einstellen(self, minuten: int) -> None:
        if minuten < 1:
            raise WerkzeugFehler("Das Ruhefenster muss mindestens 1 Minute lang sein.")
        self.vorgaben.setzen(RUHEFENSTER, str(int(minuten)))

    def in_ruhezeit(self, jetzt: datetime | None = None) -> bool:
        ruhe = self.ruhezeit()
        return ruhe is not None and im_fenster(jetzt or self.jetzt(), *ruhe)

    def ruhezeit_ende(self, jetzt: datetime) -> datetime:
        """Wann die laufende Ruhezeit endet (Wanduhrzeit, Zeitumstellung beachtet)."""
        ruhe = self.ruhezeit()
        if ruhe is None:
            return jetzt
        ende = wandzeit(jetzt.date(), ruhe[1], self.zone)
        if ende <= jetzt:
            ende = wandzeit(jetzt.date() + timedelta(days=1), ruhe[1], self.zone)
        return ende

    # -------------------------------------------------------------- Melden

    def _alarm(self, zeile: Any) -> Alarm:
        return Alarm(
            id=zeile["id"],
            zeit=datetime.fromisoformat(zeile["zeit"]).astimezone(self.zone),
            stufe=AlarmStufe(zeile["stufe"]),
            schluessel=zeile["schluessel"],
            titel=zeile["titel"],
            text=zeile["text"],
            quelle=zeile["quelle"],
            status=zeile["status"],
            zustellen_ab=datetime.fromisoformat(zeile["zustellen_ab"]).astimezone(self.zone),
        )

    def zuletzt(self, schluessel: str) -> Alarm | None:
        """Der letzte gemeldete Alarm mit diesem Schlüssel."""
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM alarme WHERE schluessel = ? ORDER BY zeit DESC, id DESC LIMIT 1",
                (schluessel,),
            ).fetchone()
        return self._alarm(zeile) if zeile else None

    def melden(
        self,
        stufe: AlarmStufe | str,
        titel: str,
        text: str = "",
        *,
        schluessel: str,
        quelle: str = "",
        ruhefenster: timedelta | None = None,
    ) -> Alarm | None:
        """Meldet einen Alarm. ``None``, wenn er im Ruhefenster schon gemeldet wurde.

        ``schluessel`` macht einen Alarm wiedererkennbar (z. B. ``regel:3:ai01``).
        """
        stufe = stufe_verstehen(stufe)
        titel = " ".join(str(titel).split())
        jetzt = self.jetzt()
        fenster = ruhefenster if ruhefenster is not None else self.ruhefenster()
        letzter = self.zuletzt(schluessel)
        if (
            letzter is not None
            and jetzt - letzter.zeit < fenster
            and stufe.rang <= letzter.stufe.rang
        ):
            self.unterdrueckt += 1
            log.debug("Alarm %s im Ruhefenster – nicht wiederholt", schluessel)
            return None

        zustellen_ab, status = jetzt, "gesendet"
        nachricht = f"{stufe.symbol} {stufe.titel}: {titel}"
        if text.strip():
            nachricht += f"\n{text.strip()}"
        if stufe is not AlarmStufe.NOTFALL and self.in_ruhezeit(jetzt):
            zustellen_ab, status = self.ruhezeit_ende(jetzt), "aufgeschoben"
            nachricht += f"\n_(Während der Ruhezeit um {jetzt:%H:%M} Uhr bemerkt.)_"

        with verbinden(self.datei) as db:
            cursor = db.execute(
                "INSERT INTO alarme (zeit, stufe, schluessel, titel, text, quelle, status, "
                "zustellen_ab) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_text(jetzt),
                    str(stufe),
                    schluessel,
                    titel,
                    text.strip(),
                    quelle,
                    status,
                    _utc_text(zustellen_ab),
                ),
            )
            zeile = db.execute("SELECT * FROM alarme WHERE id = ?", (cursor.lastrowid,)).fetchone()
        alarm = self._alarm(zeile)
        self.zeitplaner.anlegen(
            f"{stufe.titel}: {titel}",
            zustellen_ab,
            nachricht=nachricht,
            schluessel=f"alarm:{alarm.id}",
            herkunft="alarm",
        )
        log.log(
            logging.WARNING if stufe is not AlarmStufe.INFO else logging.INFO,
            "Alarm [%s] %s (%s)",
            stufe,
            titel,
            status,
        )
        return alarm

    def liste(self, seit: datetime | None = None, anzahl: int = 50) -> list[Alarm]:
        """Gemeldete Alarme (neueste zuerst), optional erst ab ``seit``."""
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT * FROM alarme WHERE zeit >= ? ORDER BY zeit DESC, id DESC LIMIT ?",
                (_utc_text(seit) if seit else "", max(1, anzahl)),
            ).fetchall()
        return [self._alarm(z) for z in zeilen]


# ----------------------------------------------------------------------------- Werkzeuge


def alarm_werkzeuge(alarme: Alarme) -> list[Werkzeug]:
    """Werkzeuge für Alarm-Verlauf, Ruhezeit und Ruhefenster."""

    def anzeigen(tage: int = 1) -> str:
        try:
            tage = max(1, min(int(tage), 30))
        except (TypeError, ValueError) as fehler:
            raise WerkzeugFehler("tage muss eine Zahl zwischen 1 und 30 sein.") from fehler
        seit = alarme.jetzt() - timedelta(days=tage)
        liste = alarme.liste(seit, 100)
        zeilen = [
            f"Alarme der letzten {tage * 24} Stunden ({len(liste)}):",
            *([a.zeile() for a in reversed(liste)] or ["- (keine)"]),
            f"Ruhezeit: {alarme.ruhezeit_text()}.",
            f"Gleiche Alarme wiederhole ich frühestens nach "
            f"{int(alarme.ruhefenster().total_seconds() // 60)} Minuten.",
        ]
        return "\n".join(zeilen)

    def einstellen(
        ruhezeit_von: str | None = None,
        ruhezeit_bis: str | None = None,
        ruhezeit_aus: bool = False,
        wiederholen_nach_minuten: int | None = None,
    ) -> str:
        geaendert = []
        if ruhezeit_aus:
            alarme.ruhezeit_einstellen(None, None)
            geaendert.append("Ruhezeit aus")
        elif ruhezeit_von or ruhezeit_bis:
            aktuell = alarme.ruhezeit() or alarme.standard_ruhe or (time(22, 0), time(7, 0))
            von = uhrzeit_lesen(ruhezeit_von) if ruhezeit_von else aktuell[0]
            bis = uhrzeit_lesen(ruhezeit_bis) if ruhezeit_bis else aktuell[1]
            alarme.ruhezeit_einstellen(von, bis)
            geaendert.append("Ruhezeit")
        if wiederholen_nach_minuten is not None:
            try:
                minuten = int(wiederholen_nach_minuten)
            except (TypeError, ValueError) as fehler:
                raise WerkzeugFehler("wiederholen_nach_minuten muss eine Zahl sein.") from fehler
            alarme.ruhefenster_einstellen(minuten)
            geaendert.append("Ruhefenster")
        if not geaendert:
            raise WerkzeugFehler(
                "Was soll ich ändern? Gib ruhezeit_von/ruhezeit_bis, ruhezeit_aus oder "
                "wiederholen_nach_minuten an."
            )
        return (
            f"Gespeichert. Ruhezeit: {alarme.ruhezeit_text()}. Gleiche Alarme wiederhole ich "
            f"frühestens nach {int(alarme.ruhefenster().total_seconds() // 60)} Minuten."
        )

    return [
        Werkzeug(
            "alarme_anzeigen",
            "Zeigt die Alarme (Server, Erreichbarkeit …), die du zuletzt gemeldet hast, dazu "
            "Ruhezeit und Ruhefenster.",
            {
                "type": "object",
                "properties": {
                    "tage": {"type": "integer", "description": "Zeitraum in Tagen (Standard 1)"}
                },
            },
            anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "alarm_einstellungen",
            "Ändert die Ruhezeit für Alarme (info und wichtig warten bis zum Ende, Notfälle "
            "kommen immer) oder wie lange derselbe Alarm nicht wiederholt wird.",
            {
                "type": "object",
                "properties": {
                    "ruhezeit_von": {"type": "string", "description": "Beginn, z. B. 22:00"},
                    "ruhezeit_bis": {"type": "string", "description": "Ende, z. B. 07:00"},
                    "ruhezeit_aus": {"type": "boolean", "description": "Keine Ruhezeit mehr"},
                    "wiederholen_nach_minuten": {
                        "type": "integer",
                        "description": "Ruhefenster: so lange denselben Alarm nicht wiederholen",
                    },
                },
            },
            einstellen,
            stufe=Stufe.SCHREIBEN,
        ),
    ]
