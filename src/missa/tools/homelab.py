"""Homelab-Wissen: Missa kennt den aktuellen Stand aller VMs aus Proxmox von selbst.

Stündlich (``HOMELAB_INTERVALL_MINUTEN``) liest Missa über die Proxmox-API (nur lesend)
alle VMs und Container mit Name, Status, Ressourcen, VLAN-Tag und IP (über den
Guest-Agent, wo vorhanden, sonst aus der Konfiguration). Der Stand steht in der Tabelle
``inventar`` und kommt in Missas Wissen: Fragt Louis nach einer VM („Welche IP hat rp01?“),
steht die aktuelle Antwort im Systemprompt; dazu gibt es das Werkzeug
``homelab_anzeigen``. Neue VMs, verschwundene VMs und geänderte IPs meldet Missa als
``info`` – beim allerersten Scan lernt sie still.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug
from missa.integrationen.monitoring import QuellenFehler
from missa.integrationen.proxmox import ProxmoxLeser, VmEintrag, inventar_lesen

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventar (
    vmid INTEGER PRIMARY KEY,
    daten TEXT NOT NULL,
    erstmals TEXT NOT NULL,
    zuletzt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventar_stand (
    name TEXT PRIMARY KEY,
    wert TEXT NOT NULL
);
"""

#: Bei diesen Wörtern kommt das ganze Inventar in den Kontext.
STICHWOERTER = re.compile(
    r"\b(ip|ips|vlan|vms?|homelab|proxmox|server|container|lxc|netz|subnetz)\b", re.IGNORECASE
)


def _utc(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class HomelabInventar:
    """Hält das Wissen über VMs und IPs aktuell und meldet Änderungen."""

    def __init__(
        self,
        leser: ProxmoxLeser,
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.leser = leser
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self._uhr = uhr or alarme.jetzt
        self.fehler = ""
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def eintraege(self) -> list[VmEintrag]:
        with verbinden(self.datei) as db:
            zeilen = db.execute("SELECT daten FROM inventar ORDER BY vmid").fetchall()
        return [self._eintrag(json.loads(z["daten"])) for z in zeilen]

    @staticmethod
    def _eintrag(daten: dict[str, Any]) -> VmEintrag:
        return VmEintrag(
            **{
                **daten,
                "vlans": tuple(daten.get("vlans", ())),
                "ips": tuple(daten.get("ips", ())),
                "tags": tuple(daten.get("tags", ())),
            }
        )

    def stand(self) -> datetime | None:
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT wert FROM inventar_stand WHERE name = 'scan'").fetchone()
        return datetime.fromisoformat(zeile["wert"]).astimezone(self.zone) if zeile else None

    async def scannen(self) -> list[Alarm]:
        """Regelmäßiger Auftrag: Proxmox lesen, Wissen aktualisieren, Änderungen melden."""
        try:
            neu = await inventar_lesen(self.leser)
        except QuellenFehler as fehler:
            self.fehler = str(fehler)
            log.warning("Homelab-Inventar: %s", fehler)
            return []
        self.fehler = ""
        jetzt = self.jetzt()
        erster_scan = self.stand() is None
        alt = {e.vmid: e for e in self.eintraege()}
        gemeldet: list[Alarm] = []
        with verbinden(self.datei) as db:
            for eintrag in neu:
                db.execute(
                    "INSERT INTO inventar (vmid, daten, erstmals, zuletzt) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (vmid) DO UPDATE SET daten = excluded.daten, "
                    "zuletzt = excluded.zuletzt",
                    (eintrag.vmid, json.dumps(eintrag.als_dict()), _utc(jetzt), _utc(jetzt)),
                )
            weg = [vmid for vmid in alt if vmid not in {e.vmid for e in neu}]
            for vmid in weg:
                db.execute("DELETE FROM inventar WHERE vmid = ?", (vmid,))
            db.execute(
                "INSERT INTO inventar_stand (name, wert) VALUES ('scan', ?) "
                "ON CONFLICT (name) DO UPDATE SET wert = excluded.wert",
                (_utc(jetzt),),
            )
        if erster_scan:
            log.info("Homelab-Inventar gelernt: %d VMs", len(neu))
            return []
        for eintrag in neu:
            vorher = alt.get(eintrag.vmid)
            if vorher is None:
                alarm = self.alarme.melden(
                    AlarmStufe.INFO,
                    f"Neue VM in Proxmox: {eintrag.name}",
                    eintrag.zeile()[2:],
                    schluessel=f"homelab:neu:{eintrag.vmid}:{eintrag.name}",
                    quelle="homelab",
                )
            elif vorher.ips and eintrag.ips and set(vorher.ips) != set(eintrag.ips):
                alarm = self.alarme.melden(
                    AlarmStufe.INFO,
                    f"IP von {eintrag.name} hat sich geändert",
                    f"Vorher {', '.join(vorher.ips)}, jetzt {', '.join(eintrag.ips)}.",
                    schluessel=f"homelab:ip:{eintrag.vmid}:{','.join(eintrag.ips)}",
                    quelle="homelab",
                )
            else:
                continue
            if alarm is not None:
                gemeldet.append(alarm)
        for vmid in weg:
            alarm = self.alarme.melden(
                AlarmStufe.INFO,
                f"VM verschwunden: {alt[vmid].name}",
                f"{alt[vmid].zeile()[2:]} – sie steht nicht mehr in Proxmox.",
                schluessel=f"homelab:weg:{vmid}:{alt[vmid].name}",
                quelle="homelab",
            )
            if alarm is not None:
                gemeldet.append(alarm)
        return gemeldet

    def finden(self, name: str) -> list[VmEintrag]:
        gesucht = str(name or "").strip().casefold()
        return [
            e
            for e in self.eintraege()
            if gesucht and (gesucht == e.name or gesucht == str(e.vmid) or gesucht in e.ips)
        ] or [e for e in self.eintraege() if gesucht and gesucht in e.name]

    async def anzeigen(self, name: str | None = None, neu_scannen: bool = False) -> str:
        if neu_scannen or self.stand() is None:
            await self.scannen()
        eintraege = self.finden(name) if name else self.eintraege()
        stand = self.stand()
        zeilen = []
        if name and not eintraege:
            bekannt = ", ".join(e.name for e in self.eintraege()) or "keine"
            zeilen.append(f"„{name}“ kenne ich nicht. Bekannte VMs: {bekannt}.")
        else:
            zeilen.append(f"Homelab ({len(eintraege)} VMs/Container):")
            zeilen += [e.zeile() for e in eintraege]
        if stand:
            zeilen.append(f"(Stand aus Proxmox: {stand:%d.%m. %H:%M} Uhr.)")
        if self.fehler:
            zeilen.append(f"Letzter Scan fehlgeschlagen: {self.fehler}")
        return "\n".join(zeilen)

    def kontext(self, frage: str) -> str | None:
        """Kontextquelle: aktuelles Wissen über die VMs, die in der Frage vorkommen."""
        eintraege = self.eintraege()
        if not eintraege:
            return None
        text = (frage or "").casefold()
        genannt = [e for e in eintraege if re.search(rf"\b{re.escape(e.name)}\b", text)]
        if not genannt and STICHWOERTER.search(text):
            genannt = eintraege
        if not genannt:
            return None
        stand = self.stand()
        kopf = "## Homelab (aktueller Stand aus Proxmox"
        kopf += f", {stand:%d.%m. %H:%M} Uhr)" if stand else ")"
        return kopf + "\n" + "\n".join(e.zeile() for e in genannt[:30])


def homelab_werkzeuge(inventar: HomelabInventar) -> list[Werkzeug]:
    return [
        Werkzeug(
            "homelab_anzeigen",
            "Zeigt die VMs und Container aus Proxmox mit IP, VLAN, Status und Ressourcen – für "
            "Fragen wie „Welche IP hat rp01?“. Optional nur eine VM (Name, VMID oder IP).",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "z. B. rp01"},
                    "neu_scannen": {"type": "boolean", "description": "jetzt neu einlesen"},
                },
            },
            inventar.anzeigen,
            stufe=Stufe.LESEN,
        )
    ]
