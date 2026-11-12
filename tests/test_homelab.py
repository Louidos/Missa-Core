"""Homelab-Wissen aus Proxmox: Inventar mit IP und VLAN, Änderungen melden, Wissen im Prompt."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.proxmox import ProxmoxLeser, agent_ips, inventar_lesen, netze_lesen
from missa.tools.homelab import HomelabInventar, homelab_werkzeuge
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

GB = 1024**3


class FakeProxmox:
    """Proxmox-API mit VMs, Konfigurationen und Guest-Agent-Antworten."""

    def __init__(self) -> None:
        self.ressourcen = [
            {"type": "node", "node": "pve-id01", "status": "online"},
            {"type": "qemu", "vmid": 101, "name": "wb01", "node": "pve-id01",
             "status": "running", "maxcpu": 2, "maxmem": 4 * GB, "maxdisk": 32 * GB},
            {"type": "qemu", "vmid": 103, "name": "ai01", "node": "pve-id01",
             "status": "running", "maxcpu": 8, "maxmem": 32 * GB, "maxdisk": 200 * GB},
            {"type": "lxc", "vmid": 110, "name": "rp01", "node": "pve-id01",
             "status": "running", "maxcpu": 1, "maxmem": GB // 2, "maxdisk": 8 * GB},
            {"type": "qemu", "vmid": 9000, "name": "debian-tpl", "node": "pve-id01",
             "status": "stopped", "template": 1},
        ]  # fmt: skip
        self.configs = {
            101: {"net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,tag=20"},
            103: {"net0": "virtio=BC:24:11:00:00:03,bridge=vmbr0,tag=50",
                  "ipconfig0": "ip=10.0.50.10/24,gw=10.0.50.1"},
            110: {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:10,"
                          "ip=10.0.70.10/24,tag=70"},
        }  # fmt: skip
        self.agent = {
            101: {"result": [
                {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                {"name": "eth0", "ip-addresses": [
                    {"ip-address": "10.0.20.10"}, {"ip-address": "fe80::1"}]},
            ]},
        }  # fmt: skip
        self.anfragen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        pfad = request.url.path.removeprefix("/api2/json/")
        if pfad == "cluster/resources":
            return httpx.Response(200, json={"data": self.ressourcen})
        teile = pfad.split("/")
        vmid = int(teile[3])
        if pfad.endswith("/config"):
            return httpx.Response(200, json={"data": self.configs.get(vmid, {})})
        if pfad.endswith("agent/network-get-interfaces"):
            if vmid in self.agent:
                return httpx.Response(200, json={"data": self.agent[vmid]})
            return httpx.Response(500, json={"data": None})  # kein Guest-Agent
        if pfad.endswith("/interfaces"):
            return httpx.Response(200, json={"data": [{"name": "eth0", "inet": "10.0.70.10/24"}]})
        return httpx.Response(404)


@pytest.fixture
def proxmox() -> FakeProxmox:
    return FakeProxmox()


@pytest.fixture
def leser(proxmox) -> ProxmoxLeser:
    return ProxmoxLeser(
        "https://pve.test:8006", "missa@pve!monitor", "x", transport=httpx.MockTransport(proxmox)
    )


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 12, 9, 0, tzinfo=ZONE))


@pytest.fixture
def alarme(tmp_path: Path, uhr) -> Alarme:
    planer = Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)
    return Alarme(planer.datei, ZONE, planer)


@pytest.fixture
def inventar(leser, alarme, tmp_path, uhr) -> HomelabInventar:
    return HomelabInventar(leser, alarme, tmp_path / "missa.db", ZONE, uhr=uhr)


def test_netze_und_agent():
    assert netze_lesen(
        {"net0": "virtio=AA,bridge=vmbr0,tag=70", "ipconfig0": "ip=10.0.70.5/24"}
    ) == (
        (70,),
        ("10.0.70.5",),
    )
    assert netze_lesen({"net0": "name=eth0,ip=dhcp,tag=30"}) == ((30,), ())
    assert agent_ips({"result": [{"name": "eth0", "ip-addresses": [
        {"ip-address": "fe80::5"}, {"ip-address": "2001:db8::5"}, {"ip-address": "10.0.1.5"}]}]}
    ) == ("10.0.1.5", "2001:db8::5")  # fmt: skip


def test_inventar_lesen(leser, proxmox):
    eintraege = {e.name: e for e in asyncio.run(inventar_lesen(leser))}
    assert set(eintraege) == {"wb01", "ai01", "rp01"}  # ohne Template und Host
    assert eintraege["wb01"].ips == ("10.0.20.10",) and eintraege["wb01"].ip_quelle == "agent"
    assert eintraege["wb01"].vlans == (20,)
    assert eintraege["ai01"].ips == ("10.0.50.10",)  # kein Agent → aus Cloud-Init
    assert eintraege["ai01"].ip_quelle == "konfiguration"
    assert eintraege["rp01"].ips == ("10.0.70.10",) and eintraege["rp01"].typ == "lxc"
    assert eintraege["ai01"].ram_mb == 32 * 1024 and eintraege["ai01"].disk_gb == 200
    assert all(r.method == "GET" for r in proxmox.anfragen)


def test_welche_ip_hat_rp01(inventar):
    kasten = Werkzeugkasten(homelab_werkzeuge(inventar))
    antwort = asyncio.run(kasten.ausfuehren("homelab_anzeigen", {"name": "rp01"}))
    assert "rp01 (Container 110 auf pve-id01, running) | IP 10.0.70.10 | VLAN 70" in antwort
    # das Wissen steht auch im Systemprompt, wenn rp01 in der Frage vorkommt
    kontext = inventar.kontext("Welche IP hat rp01?")
    assert kontext.startswith("## Homelab (aktueller Stand aus Proxmox, 12.11. 09:00 Uhr)")
    assert "IP 10.0.70.10" in kontext and "wb01" not in kontext
    assert inventar.kontext("Wie wird das Wetter?") is None
    assert "wb01" in inventar.kontext("Welche VMs gibt es?")


def test_neue_vm_ip_aenderung_und_verschwunden(inventar, proxmox, uhr):
    assert asyncio.run(inventar.scannen()) == []  # erster Scan: still lernen
    proxmox.ressourcen.append(
        {"type": "qemu", "vmid": 120, "name": "mc01", "node": "pve-id01", "status": "running",
         "maxcpu": 2, "maxmem": 4 * GB, "maxdisk": 20 * GB, "pool": "missa"}
    )  # fmt: skip
    proxmox.configs[120] = {
        "net0": "virtio=AA,bridge=vmbr0,tag=70",
        "ipconfig0": "ip=10.0.70.120/24",
    }
    proxmox.agent[101] = {
        "result": [{"name": "eth0", "ip-addresses": [{"ip-address": "10.0.20.11"}]}]
    }
    proxmox.ressourcen = [r for r in proxmox.ressourcen if r.get("vmid") != 103]
    uhr.vor(hours=1)
    alarme = asyncio.run(inventar.scannen())
    assert sorted((a.stufe, a.titel) for a in alarme) == [
        (AlarmStufe.INFO, "IP von wb01 hat sich geändert"),
        (AlarmStufe.INFO, "Neue VM in Proxmox: mc01"),
        (AlarmStufe.INFO, "VM verschwunden: ai01"),
    ]
    # spätestens nach dem nächsten Scan kennt Missa die neue VM
    assert "IP 10.0.70.120" in inventar.kontext("Was ist mit mc01?")
    assert "Vorher 10.0.20.10, jetzt 10.0.20.11." in next(
        a.text for a in alarme if "wb01" in a.titel
    )
    uhr.vor(hours=1)
    assert asyncio.run(inventar.scannen()) == []


def test_proxmox_nicht_erreichbar(alarme, tmp_path, uhr):
    leser = ProxmoxLeser(
        "https://pve.test:8006", "missa@pve!monitor", "x",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )  # fmt: skip
    inventar = HomelabInventar(leser, alarme, tmp_path / "missa.db", ZONE, uhr=uhr)
    assert asyncio.run(inventar.scannen()) == []
    assert "Letzter Scan fehlgeschlagen" in asyncio.run(inventar.anzeigen())
