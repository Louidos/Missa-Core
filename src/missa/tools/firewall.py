"""Firewall-Logs zusammenfassen – nur lesend, ohne jeden Zugang zur Firewall.

Die ZyWALL schickt ihre Logs per Syslog an ai01, rsyslog schreibt sie in
``FIREWALL_LOG_DATEI``. Missa liest die Datei jede Minute weiter (auch über eine
Log-Rotation hinweg), speichert die Verbindungen (Tabelle ``firewall_ereignisse``,
30 Tage) und fasst jeden Morgen den Vortag zusammen:

- **gehäufte Blocks** – Quellen mit vielen geblockten Verbindungen,
- **Port-Scans** – eine Quelle, die in kurzer Zeit viele verschiedene Ports anfragt,
- **ungewöhnliche Ziele** – Verbindungen aus dem eigenen Netz zu Zielen und Ports, die es
  in den 7 Tagen davor nie gab.

Es gibt **keine** Zugangsdaten zur Firewall und kein Werkzeug, das Regeln ändert.
"""

from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.core.zeitangaben import wandzeit
from missa.integrationen.anmeldungen import ist_privat
from missa.integrationen.zywall import FirewallEreignis, zeile_lesen

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS firewall_ereignisse (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeit TEXT NOT NULL,
    src_ip TEXT NOT NULL,
    src_port INTEGER,
    dst_ip TEXT NOT NULL,
    dst_port INTEGER,
    aktion TEXT NOT NULL,
    kategorie TEXT NOT NULL DEFAULT '',
    protokoll TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS firewall_zeit ON firewall_ereignisse (zeit);
CREATE TABLE IF NOT EXISTS firewall_stand (
    datei TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    position INTEGER NOT NULL
);
"""

AUFBEWAHREN_TAGE = 30
#: Höchstens so viel wird pro Durchgang gelesen (der Rest beim nächsten Mal).
MAX_LESEN = 20 * 1024 * 1024
#: Port-Scan: so viele verschiedene Ziel-Ports von einer Quelle …
SCAN_PORTS = 15
#: … innerhalb dieser Zeit.
SCAN_FENSTER = timedelta(minutes=10)
#: Ab so vielen Blocks an einem Tag gilt eine Quelle als „gehäuft“.
BLOCK_GRENZE = 100


def _utc(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class FirewallLog:
    """Liest die Firewall-Logdatei weiter und fasst Tage zusammen."""

    def __init__(
        self,
        datei: Path,
        db_datei: Path,
        zone: ZoneInfo,
        alarme: Alarme | None = None,
        *,
        scan_ports: int = SCAN_PORTS,
        block_grenze: int = BLOCK_GRENZE,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.db_datei = db_datei
        self.zone = zone
        self.alarme = alarme
        self.scan_ports = max(3, scan_ports)
        self.block_grenze = max(1, block_grenze)
        self._uhr = uhr or (alarme.jetzt if alarme is not None else lambda: datetime.now(zone))
        self.fehler = ""
        with verbinden(db_datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Einlesen

    def _stand(self) -> tuple[int, int] | None:
        with verbinden(self.db_datei) as db:
            zeile = db.execute(
                "SELECT inode, position FROM firewall_stand WHERE datei = ?", (str(self.datei),)
            ).fetchone()
        return (zeile["inode"], zeile["position"]) if zeile else None

    def einlesen(self) -> int:
        """Liest neue Zeilen seit dem letzten Mal. Gibt die Zahl gespeicherter Ereignisse zurück."""
        try:
            info = os.stat(self.datei)
        except OSError as fehler:
            self.fehler = f"{self.datei} nicht lesbar: {fehler.strerror or fehler}"
            return 0
        stand = self._stand()
        position = 0
        if stand is not None and stand[0] == info.st_ino and stand[1] <= info.st_size:
            position = stand[1]  # sonst: neue Datei nach Rotation – von vorn
        jetzt = self.jetzt()
        ereignisse: list[FirewallEreignis] = []
        try:
            with open(self.datei, "rb") as datei:
                datei.seek(position)
                daten = datei.read(MAX_LESEN)
        except OSError as fehler:
            self.fehler = f"{self.datei} nicht lesbar: {fehler.strerror or fehler}"
            return 0
        # nur vollständige Zeilen verarbeiten
        ende = daten.rfind(b"\n") + 1
        for zeile in daten[:ende].decode("utf-8", "replace").splitlines():
            if ereignis := zeile_lesen(zeile, self.zone, jetzt):
                ereignisse.append(ereignis)
        self.fehler = ""
        with verbinden(self.db_datei) as db:
            db.executemany(
                "INSERT INTO firewall_ereignisse (zeit, src_ip, src_port, dst_ip, dst_port, "
                "aktion, kategorie, protokoll, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        _utc(e.zeit),
                        e.src_ip,
                        e.src_port,
                        e.dst_ip,
                        e.dst_port,
                        e.aktion,
                        e.kategorie,
                        e.protokoll,
                        e.text,
                    )
                    for e in ereignisse
                ],
            )
            db.execute(
                "INSERT INTO firewall_stand (datei, inode, position) VALUES (?, ?, ?) "
                "ON CONFLICT (datei) DO UPDATE SET inode = excluded.inode, "
                "position = excluded.position",
                (str(self.datei), info.st_ino, position + ende),
            )
            grenze = _utc(jetzt - timedelta(days=AUFBEWAHREN_TAGE))
            db.execute("DELETE FROM firewall_ereignisse WHERE zeit < ?", (grenze,))
        return len(ereignisse)

    # -------------------------------------------------------------- Auswerten

    def _ereignisse(self, von: datetime, bis: datetime) -> list[dict]:
        with verbinden(self.db_datei) as db:
            return [
                dict(z)
                for z in db.execute(
                    "SELECT * FROM firewall_ereignisse WHERE zeit >= ? AND zeit < ? ORDER BY zeit",
                    (_utc(von), _utc(bis)),
                )
            ]

    def _tag_grenzen(self, tag: date) -> tuple[datetime, datetime]:
        return (
            wandzeit(tag, time(0), self.zone),
            wandzeit(tag + timedelta(days=1), time(0), self.zone),
        )

    def port_scans(self, ereignisse: list[dict]) -> list[tuple[str, int, datetime]]:
        """(Quelle, Zahl verschiedener Ports, Beginn) – viele Ports in kurzer Zeit."""
        nach_quelle: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
        for e in ereignisse:
            if e["dst_port"] is not None:
                nach_quelle[e["src_ip"]].append((datetime.fromisoformat(e["zeit"]), e["dst_port"]))
        scans = []
        for quelle, liste in nach_quelle.items():
            liste.sort()
            anfang = 0
            bestes = (0, liste[0][0]) if liste else (0, None)
            for ende in range(len(liste)):
                while liste[ende][0] - liste[anfang][0] > SCAN_FENSTER:
                    anfang += 1
                ports = len({p for _, p in liste[anfang : ende + 1]})
                if ports > bestes[0]:
                    bestes = (ports, liste[anfang][0])
            if bestes[0] >= self.scan_ports and bestes[1] is not None:
                scans.append((quelle, bestes[0], bestes[1].astimezone(self.zone)))
        return sorted(scans, key=lambda s: -s[1])

    def ungewoehnliche_ziele(self, ereignisse: list[dict], vorher: list[dict]) -> list[tuple]:
        """Verbindungen aus dem eigenen Netz zu (Ziel, Port), die es vorher nie gab."""
        bekannt = {
            (e["dst_ip"], e["dst_port"])
            for e in vorher
            if ist_privat(e["src_ip"]) and not ist_privat(e["dst_ip"])
        }
        neu: Counter[tuple[str, str, int | None]] = Counter()
        for e in ereignisse:
            if (
                ist_privat(e["src_ip"])
                and not ist_privat(e["dst_ip"])
                and (e["dst_ip"], e["dst_port"]) not in bekannt
            ):
                neu[(e["src_ip"], e["dst_ip"], e["dst_port"])] += 1
        return [(*k, n) for k, n in neu.most_common(10)]

    def zusammenfassung(self, tag: date) -> str:
        von, bis = self._tag_grenzen(tag)
        ereignisse = self._ereignisse(von, bis)
        blocks = [e for e in ereignisse if e["aktion"] == "blockiert"]
        titel = f"Firewall am {tag:%d.%m.%Y}"
        if not ereignisse:
            hinweis = f" ({self.fehler})" if self.fehler else ""
            return f"{titel}: keine Log-Einträge.{hinweis}"
        zeilen = [f"{titel}: {len(ereignisse)} Einträge, davon {len(blocks)} geblockt."]
        quellen = Counter(e["src_ip"] for e in blocks)
        haeufig = [(ip, n) for ip, n in quellen.most_common(5) if n >= self.block_grenze]
        if haeufig:
            zeilen.append("Gehäufte Blocks:")
            for ip, n in haeufig:
                ports = Counter(e["dst_port"] for e in blocks if e["src_ip"] == ip).most_common(3)
                zeilen.append(
                    f"- {ip}: {n}× geblockt (vor allem Port "
                    + ", ".join(str(p) for p, _ in ports if p is not None)
                    + ")"
                )
        elif quellen:
            zeilen.append(
                "Meiste Blocks: " + ", ".join(f"{ip} ({n}×)" for ip, n in quellen.most_common(3))
            )
        scans = self.port_scans(ereignisse)
        if scans:
            zeilen.append("Port-Scans:")
            zeilen += [
                f"- {ip}: {ports} verschiedene Ports ab {start:%H:%M} Uhr"
                for ip, ports, start in scans[:5]
            ]
        vorher = self._ereignisse(von - timedelta(days=7), von)
        ziele = self.ungewoehnliche_ziele(ereignisse, vorher)
        if ziele and vorher:
            zeilen.append("Ungewöhnliche Ziele (aus dem eigenen Netz, in den 7 Tagen davor nie):")
            zeilen += [f"- {src} → {dst}:{port} ({n}×)" for src, dst, port, n in ziele[:5]]
        if not (haeufig or scans or (ziele and vorher)):
            zeilen.append("Nichts Auffälliges.")
        return "\n".join(zeilen)

    async def taeglich_berichten(self) -> Alarm | None:
        """Täglicher Auftrag: den Vortag zusammenfassen und als info schicken."""
        self.einlesen()
        if self.alarme is None:
            return None
        gestern = self.jetzt().date() - timedelta(days=1)
        text = self.zusammenfassung(gestern)
        titel, _, rest = text.partition("\n")
        return self.alarme.melden(
            AlarmStufe.INFO,
            titel,
            rest,
            schluessel=f"firewall:bericht:{gestern.isoformat()}",
            quelle="firewall",
        )

    def tag_verstehen(self, text: str | None) -> date:
        heute = self.jetzt().date()
        wort = (text or "gestern").strip().casefold()
        if wort == "heute":
            return heute
        if wort == "gestern":
            return heute - timedelta(days=1)
        if wort == "vorgestern":
            return heute - timedelta(days=2)
        try:
            return date.fromisoformat(wort)
        except ValueError as fehler:
            raise WerkzeugFehler(
                "tag: heute, gestern, vorgestern oder ein Datum wie 2026-11-11."
            ) from fehler

    def anzeigen(self, tag: str | None = None) -> str:
        self.einlesen()
        return self.zusammenfassung(self.tag_verstehen(tag))


def firewall_werkzeuge(firewall: FirewallLog) -> list[Werkzeug]:
    """Nur lesend: Es gibt kein Werkzeug, das an der Firewall etwas ändert."""
    return [
        Werkzeug(
            "firewall_zusammenfassung",
            "Fasst die Firewall-Logs (ZyWALL) eines Tages zusammen: geblockte Verbindungen, "
            "Port-Scans, ungewöhnliche Ziele. Für „Was war gestern an der Firewall los?“. Nur "
            "lesen – an der Firewall selbst kannst du nichts ändern.",
            {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "heute, gestern (Standard) oder Datum"}
                },
            },
            firewall.anzeigen,
            stufe=Stufe.LESEN,
        )
    ]
