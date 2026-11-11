"""Login-Überwachung: SSH- und Nextcloud-Anmeldungen auswerten und Ungewöhnliches melden.

Der :class:`LoginWaechter` liest regelmäßig (Standard: alle 5 Minuten) die neuen
Anmeldungen jedes Hosts (journal von sshd) und – wenn eingerichtet – aus dem
Nextcloud-Log. Er meldet:

- **viele Fehlversuche**: mindestens ``LOGIN_FEHLVERSUCHE_GRENZE`` (10) in
  ``LOGIN_FEHLVERSUCHE_FENSTER_MINUTEN`` (15) auf einem Host → ``wichtig``,
- **neue IP**: erfolgreiche Anmeldung von einer IP, die es noch nie gab → ``wichtig``
  (aus dem eigenen Netz: ``info``),
- **ungewohnte Zeit**: erfolgreiche Anmeldung zu einer Stunde, in der es sonst (fast)
  nie Anmeldungen gibt → ``info`` (mit wenig Verlauf: zwischen 0 und 5 Uhr).

IPs, die Louis als „bekannt“ markiert, lösen keine Hinweise aus. Beim allerersten Lauf
lernt der Wächter still die letzten 7 Tage, damit nicht alles als „neu“ gemeldet wird.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.hintergrund import im_fenster
from missa.core.vorgaben import Vorgaben
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.anmeldungen import (
    LoginEreignis,
    NextcloudLog,
    ip_pruefen,
    ist_privat,
    nextcloud_lesen,
    ssh_lesen,
)
from missa.integrationen.ssh import SshFehler, SshVerbindung

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS anmeldungen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quelle TEXT NOT NULL,
    host TEXT NOT NULL,
    zeit TEXT NOT NULL,
    benutzer TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    erfolg INTEGER NOT NULL,
    art TEXT NOT NULL DEFAULT '',
    kennung TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS anmeldungen_zeit ON anmeldungen (zeit);
CREATE INDEX IF NOT EXISTS anmeldungen_ip ON anmeldungen (ip, erfolg);
CREATE TABLE IF NOT EXISTS login_ips (
    ip TEXT PRIMARY KEY,
    bekannt INTEGER NOT NULL DEFAULT 0,
    notiz TEXT NOT NULL DEFAULT '',
    erstmals TEXT NOT NULL,
    zuletzt TEXT NOT NULL,
    erfolge INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_stand (
    quelle TEXT PRIMARY KEY,
    bis TEXT NOT NULL
);
"""

QUELLEN_NAMEN = {"ssh": "SSH", "nextcloud": "Nextcloud"}
#: Beim ersten Lauf werden so viele Tage still gelernt.
LERN_TAGE = 7
#: Anmeldungen werden so lange aufbewahrt.
AUFBEWAHREN_TAGE = 90
#: Ab so vielen erfolgreichen Anmeldungen (30 Tage) gilt der gelernte Tagesverlauf.
MIN_VERLAUF = 20
#: Stunden mit weniger als diesem Anteil aller Anmeldungen gelten als ungewohnt.
SELTEN = 0.03
GRENZE = "login.fehlversuche_grenze"
FENSTER = "login.fenster_minuten"


def _utc(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class LoginWaechter:
    """Liest Anmeldungen, speichert sie und meldet Fehlversuche, neue IPs und ungewohnte Zeiten."""

    def __init__(
        self,
        ssh: SshVerbindung,
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        *,
        hosts: Iterable[str] = (),
        nextcloud_log: NextcloudLog | None = None,
        fehlversuche_grenze: int = 10,
        fenster_minuten: int = 15,
        automatisch: Iterable[str] = ("missa",),
        bekannte_ips: Iterable[str] = (),
        nacht_von: time = time(0, 0),
        nacht_bis: time = time(5, 0),
        vorgaben: Vorgaben | None = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.ssh = ssh
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self.hosts = [h.casefold() for h in hosts] or ssh.namen
        self.nextcloud_log = nextcloud_log
        self.standard_grenze = max(1, fehlversuche_grenze)
        self.standard_fenster = max(1, fenster_minuten)
        self.automatisch = {b.strip().casefold() for b in automatisch if b.strip()}
        self.nacht = (nacht_von, nacht_bis)
        self.vorgaben = vorgaben or Vorgaben(datei)
        self._uhr = uhr or alarme.jetzt
        self.zuletzt_geprueft: datetime | None = None
        self.fehler: dict[str, str] = {}
        with verbinden(datei) as db:
            db.executescript(SCHEMA)
        for ip in bekannte_ips:
            try:
                self.bekannt_markieren(ip, "aus der Konfiguration (LOGIN_BEKANNTE_IPS)")
            except WerkzeugFehler:
                log.warning("LOGIN_BEKANNTE_IPS: „%s“ ist keine IP-Adresse", ip)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Einstellungen

    def grenze(self) -> int:
        try:
            return max(1, int(self.vorgaben.lesen(GRENZE, str(self.standard_grenze))))
        except ValueError:
            return self.standard_grenze

    def fenster(self) -> timedelta:
        try:
            minuten = int(self.vorgaben.lesen(FENSTER, str(self.standard_fenster)))
        except ValueError:
            minuten = self.standard_fenster
        return timedelta(minutes=max(1, minuten))

    # -------------------------------------------------------------- IPs

    def bekannt_markieren(self, ip: Any, notiz: str = "") -> str:
        try:
            adresse = ip_pruefen(ip)
        except ValueError as fehler:
            raise WerkzeugFehler(f"„{ip}“ ist keine gültige IP-Adresse.") from fehler
        jetzt = _utc(self.jetzt())
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO login_ips (ip, bekannt, notiz, erstmals, zuletzt) "
                "VALUES (?, 1, ?, ?, ?) ON CONFLICT (ip) DO UPDATE SET bekannt = 1, "
                "notiz = CASE WHEN excluded.notiz != '' THEN excluded.notiz ELSE notiz END",
                (adresse, " ".join(str(notiz or "").split())[:200], jetzt, jetzt),
            )
        return adresse

    def bekannt_entfernen(self, ip: Any) -> str:
        try:
            adresse = ip_pruefen(ip)
        except ValueError as fehler:
            raise WerkzeugFehler(f"„{ip}“ ist keine gültige IP-Adresse.") from fehler
        with verbinden(self.datei) as db:
            geaendert = db.execute(
                "UPDATE login_ips SET bekannt = 0 WHERE ip = ? AND bekannt = 1", (adresse,)
            ).rowcount
        if not geaendert:
            raise WerkzeugFehler(f"{adresse} war nicht als bekannt markiert.")
        return adresse

    def bekannte(self) -> list[dict[str, Any]]:
        with verbinden(self.datei) as db:
            return [
                dict(z) for z in db.execute("SELECT * FROM login_ips WHERE bekannt = 1 ORDER BY ip")
            ]

    def _ip(self, ip: str) -> dict[str, Any] | None:
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT * FROM login_ips WHERE ip = ?", (ip,)).fetchone()
        return dict(zeile) if zeile else None

    # -------------------------------------------------------------- Lesen und Speichern

    def _stand(self, quelle: str) -> datetime | None:
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT bis FROM login_stand WHERE quelle = ?", (quelle,)).fetchone()
        return datetime.fromisoformat(zeile["bis"]) if zeile else None

    def _stand_setzen(self, quelle: str, bis: datetime) -> None:
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO login_stand (quelle, bis) VALUES (?, ?) ON CONFLICT (quelle) "
                "DO UPDATE SET bis = excluded.bis",
                (quelle, _utc(bis)),
            )

    def speichern(self, ereignisse: list[LoginEreignis]) -> list[LoginEreignis]:
        """Speichert Ereignisse; gibt die zurück, die neu waren."""
        neu = []
        with verbinden(self.datei) as db:
            for e in ereignisse:
                kennung = hashlib.sha256(e.kennung.encode()).hexdigest()
                cursor = db.execute(
                    "INSERT OR IGNORE INTO anmeldungen (quelle, host, zeit, benutzer, ip, erfolg, "
                    "art, kennung) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        e.quelle,
                        e.host,
                        _utc(e.zeit),
                        e.benutzer,
                        e.ip,
                        int(e.erfolg),
                        e.art,
                        kennung,
                    ),
                )
                if cursor.rowcount:
                    neu.append(e)
            grenze = _utc(self.jetzt() - timedelta(days=AUFBEWAHREN_TAGE))
            db.execute("DELETE FROM anmeldungen WHERE zeit < ?", (grenze,))
        return neu

    async def _lesen(self, quelle: str, host: str, seit: datetime) -> list[LoginEreignis]:
        if quelle == "ssh":
            text = await self.ssh.befehl(
                host, "ssh-log", f"seit=@{int(seit.timestamp())}", "zeilen=5000", timeout=60
            )
            return ssh_lesen(host, text)
        assert self.nextcloud_log is not None
        text = await self.nextcloud_log.lesen(self.ssh, 2000, seit)
        return [e for e in nextcloud_lesen(host, text) if e.zeit >= seit]

    async def pruefen(self) -> list[Alarm]:
        """Regelmäßiger Auftrag: neue Anmeldungen lesen, speichern und auswerten."""
        jetzt = self.jetzt()
        quellen = [("ssh", h) for h in self.hosts if h in self.ssh.hosts]
        if self.nextcloud_log is not None:
            quellen.append(("nextcloud", self.nextcloud_log.host))
        gemeldet: list[Alarm] = []
        for quelle, host in quellen:
            kennung = f"{quelle}:{host}"
            stand = self._stand(kennung)
            lernen = stand is None
            seit = (stand or jetzt - timedelta(days=LERN_TAGE)) - timedelta(minutes=2)
            try:
                ereignisse = await self._lesen(quelle, host, seit)
            except SshFehler as fehler:
                self.fehler[kennung] = str(fehler)
                log.info("Anmeldungen %s: %s", kennung, fehler)
                continue
            self.fehler.pop(kennung, None)
            neu = self.speichern(ereignisse)
            self._stand_setzen(kennung, jetzt)
            gemeldet += self.auswerten(neu, quelle, host, lernen)
        self.zuletzt_geprueft = jetzt
        return gemeldet

    # -------------------------------------------------------------- Auswerten

    def _ungewohnt(self, ereignis: LoginEreignis) -> bool:
        if ereignis.benutzer.casefold() in self.automatisch:
            return False
        zeit = ereignis.zeit.astimezone(self.zone)
        seit = _utc(self.jetzt() - timedelta(days=30))
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT zeit, benutzer FROM anmeldungen WHERE quelle = ? AND erfolg = 1 "
                "AND zeit >= ?",
                (ereignis.quelle, seit),
            ).fetchall()
        stunden = Counter(
            datetime.fromisoformat(z["zeit"]).astimezone(self.zone).hour
            for z in zeilen
            if z["benutzer"].casefold() not in self.automatisch
        )
        gesamt = sum(stunden.values()) - 1  # ohne diese Anmeldung
        if gesamt >= MIN_VERLAUF:
            return (stunden[zeit.hour] - 1) / gesamt < SELTEN
        return im_fenster(zeit, *self.nacht)

    def auswerten(
        self, neu: list[LoginEreignis], quelle: str, host: str, lernen: bool = False
    ) -> list[Alarm]:
        gemeldet: list[Alarm] = []
        name = QUELLEN_NAMEN.get(quelle, quelle)
        for e in [e for e in neu if e.erfolg]:
            vorher = self._ip(e.ip) if e.ip else None
            with verbinden(self.datei) as db:
                db.execute(
                    "INSERT INTO login_ips (ip, erstmals, zuletzt, erfolge) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT (ip) DO UPDATE SET zuletzt = excluded.zuletzt, "
                    "erfolge = erfolge + 1",
                    (e.ip, _utc(e.zeit), _utc(e.zeit)),
                )
            if lernen or not e.ip:
                continue
            bekannt = bool(vorher and vorher["bekannt"])
            neue_ip = vorher is None or (not vorher["bekannt"] and not vorher["erfolge"])
            ungewohnt = not bekannt and self._ungewohnt(e)
            uhrzeit = e.zeit.astimezone(self.zone)
            if neue_ip and not bekannt:
                stufe = AlarmStufe.WICHTIG if not ist_privat(e.ip) or ungewohnt else AlarmStufe.INFO
                netz = "aus dem eigenen Netz" if ist_privat(e.ip) else "von außen"
                alarm = self.alarme.melden(
                    stufe,
                    f"Neue IP bei {name} auf {host}: {e.benutzer} von {e.ip}",
                    f"Anmeldung am {uhrzeit:%d.%m. um %H:%M} Uhr ({e.art}), IP {netz}"
                    + (", zu einer ungewohnten Zeit" if ungewohnt else "")
                    + f". Warst du das? Dann sag „{e.ip} ist bekannt“.",
                    schluessel=f"login:neue_ip:{e.ip}",
                    quelle=quelle,
                )
            elif ungewohnt:
                alarm = self.alarme.melden(
                    AlarmStufe.INFO,
                    f"{name}-Anmeldung zu ungewohnter Zeit: {e.benutzer} auf {host}",
                    f"Am {uhrzeit:%d.%m. um %H:%M} Uhr von {e.ip} ({e.art}). Um diese Zeit meldet "
                    "sich sonst (fast) nie jemand an.",
                    schluessel=f"login:zeit:{quelle}:{host}:{e.benutzer}:{uhrzeit:%Y-%m-%d}",
                    quelle=quelle,
                )
            else:
                continue
            if alarm is not None:
                gemeldet.append(alarm)
        if (
            not lernen
            and any(not e.erfolg for e in neu)
            and (alarm := self._fehlversuche(quelle, host))
        ):
            gemeldet.append(alarm)
        return gemeldet

    def _fehlversuche(self, quelle: str, host: str) -> Alarm | None:
        fenster = self.fenster()
        seit = _utc(self.jetzt() - fenster)
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                "SELECT ip, benutzer FROM anmeldungen WHERE quelle = ? AND host = ? AND erfolg = 0 "
                "AND zeit >= ?",
                (quelle, host, seit),
            ).fetchall()
        if len(zeilen) < self.grenze():
            return None
        ips = Counter(z["ip"] for z in zeilen)
        benutzer = Counter(z["benutzer"] for z in zeilen if z["benutzer"])
        minuten = int(fenster.total_seconds() // 60)
        bekannt = {b["ip"] for b in self.bekannte()}
        haeufig = ", ".join(
            f"{ip} ({n}×{', bekannt' if ip in bekannt else ''})" for ip, n in ips.most_common(3)
        )
        return self.alarme.melden(
            AlarmStufe.WICHTIG,
            f"{len(zeilen)} fehlgeschlagene {QUELLEN_NAMEN.get(quelle, quelle)}-Anmeldungen "
            f"auf {host}",
            f"In den letzten {minuten} Minuten, vor allem von {haeufig}. Benutzer: "
            + (", ".join(b for b, _ in benutzer.most_common(5)) or "?")
            + ". Das sieht nach einem Rate-Versuch aus.",
            schluessel=f"login:fehlversuche:{quelle}:{host}",
            quelle=quelle,
        )

    # -------------------------------------------------------------- Anzeigen

    def zusammenfassung(self, host: str | None = None, tage: int = 1) -> str:
        seit = self.jetzt() - timedelta(days=max(1, min(int(tage), 90)))
        bedingung, werte = "zeit >= ?", [_utc(seit)]
        if host:
            bedingung += " AND host = ?"
            werte.append(host.casefold())
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                f"SELECT * FROM anmeldungen WHERE {bedingung} ORDER BY zeit", werte
            ).fetchall()
        bekannt = {b["ip"]: b["notiz"] for b in self.bekannte()}
        erfolge = [z for z in zeilen if z["erfolg"]]
        fehler = [z for z in zeilen if not z["erfolg"]]
        titel = f"Anmeldungen der letzten {tage * 24} Stunden" + (f" auf {host}" if host else "")
        text = [f"{titel}: {len(erfolge)} erfolgreich, {len(fehler)} fehlgeschlagen."]
        for z in erfolge[-30:]:
            zeit = datetime.fromisoformat(z["zeit"]).astimezone(self.zone)
            markierung = ""
            if z["ip"] in bekannt:
                markierung = f" (bekannt{': ' + bekannt[z['ip']] if bekannt[z['ip']] else ''})"
            text.append(
                f"- {zeit:%d.%m. %H:%M} {QUELLEN_NAMEN.get(z['quelle'], z['quelle'])} "
                f"{z['host']}: {z['benutzer']} von {z['ip']}{markierung}"
            )
        if fehler:
            ips = Counter(z["ip"] for z in fehler).most_common(5)
            text.append("Fehlversuche vor allem von: " + ", ".join(f"{ip} ({n}×)" for ip, n in ips))
        if self.fehler:
            text.append("Nicht lesbar: " + "; ".join(f"{k}: {v}" for k, v in self.fehler.items()))
        if self.zuletzt_geprueft:
            text.append(f"(Zuletzt geprüft: {self.zuletzt_geprueft:%H:%M} Uhr.)")
        return "\n".join(text)


def login_werkzeuge(waechter: LoginWaechter) -> list[Werkzeug]:
    def anzeigen(host: str | None = None, tage: int = 1) -> str:
        return waechter.zusammenfassung(host, tage)

    def bekannt(ip: str, notiz: str = "") -> str:
        adresse = waechter.bekannt_markieren(ip, notiz)
        return f"{adresse} ist jetzt als bekannt markiert – dafür gibt es keine Hinweise mehr."

    def nicht_bekannt(ip: str) -> str:
        return f"{waechter.bekannt_entfernen(ip)} ist nicht mehr als bekannt markiert."

    def liste() -> str:
        eintraege = waechter.bekannte()
        if not eintraege:
            return "Es sind keine IPs als bekannt markiert."
        return "Bekannte IPs:\n" + "\n".join(
            f"- {e['ip']}" + (f": {e['notiz']}" if e["notiz"] else "") for e in eintraege
        )

    def einstellen(fehlversuche_grenze: int | None = None, fenster_minuten: int | None = None):
        if fehlversuche_grenze is None and fenster_minuten is None:
            raise WerkzeugFehler("Was soll ich ändern? fehlversuche_grenze oder fenster_minuten.")
        if fehlversuche_grenze is not None:
            waechter.vorgaben.setzen(GRENZE, str(max(1, int(fehlversuche_grenze))))
        if fenster_minuten is not None:
            waechter.vorgaben.setzen(FENSTER, str(max(1, int(fenster_minuten))))
        minuten = int(waechter.fenster().total_seconds() // 60)
        return (
            f"Gespeichert: Ich melde mich ab {waechter.grenze()} fehlgeschlagenen Anmeldungen "
            f"in {minuten} Minuten auf einem Host."
        )

    return [
        Werkzeug(
            "anmeldungen_anzeigen",
            "Zeigt die SSH- und Nextcloud-Anmeldungen (erfolgreich und fehlgeschlagen) der "
            "letzten Tage, optional für einen Host.",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "z. B. wb01"},
                    "tage": {"type": "integer", "description": "Standard 1"},
                },
            },
            anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "ip_als_bekannt_markieren",
            "Markiert eine IP als bekannt (z. B. „10.0.30.5 ist mein Laptop“) – Anmeldungen von "
            "dort lösen keine Hinweise mehr aus.",
            {
                "type": "object",
                "properties": {
                    "ip": {"type": "string"},
                    "notiz": {"type": "string", "description": "z. B. Laptop"},
                },
                "required": ["ip"],
            },
            bekannt,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "ip_nicht_mehr_bekannt",
            "Nimmt die Markierung „bekannt“ von einer IP wieder weg.",
            {
                "type": "object",
                "properties": {"ip": {"type": "string"}},
                "required": ["ip"],
            },
            nicht_bekannt,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "bekannte_ips_anzeigen",
            "Listet alle als bekannt markierten IPs.",
            {"type": "object", "properties": {}},
            liste,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "login_einstellungen",
            "Ändert, ab wie vielen fehlgeschlagenen Anmeldungen in wie vielen Minuten du dich "
            "meldest.",
            {
                "type": "object",
                "properties": {
                    "fehlversuche_grenze": {"type": "integer"},
                    "fenster_minuten": {"type": "integer"},
                },
            },
            einstellen,
            stufe=Stufe.SCHREIBEN,
        ),
    ]
