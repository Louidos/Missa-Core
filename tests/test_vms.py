"""Eigene VMs in Proxmox: Plan, Ja, Grenzen, Klonen, Playbooks und nur im Pool."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.rechte import Aktionsprotokoll, Rechte
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.proxmox_vms import ProxmoxVerwalter, bereich_lesen
from missa.tools.vms import Grenzen, VmVerwaltung, netze_lesen, vm_werkzeuge
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

PLAYBOOKS = Path(__file__).resolve().parent.parent / "playbooks"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "einrichten" / "proxmox-missa.sh"
GB = 1024**3


class FakeProxmox:
    """Proxmox-API mit Pool „missa“; merkt sich alle ändernden Anfragen."""

    def __init__(self) -> None:
        self.vms = {
            101: {"vmid": 101, "name": "wb01", "type": "qemu", "status": "running",
                  "maxcpu": 2, "maxmem": 4 * GB, "maxdisk": 32 * GB, "pool": ""},
            9000: {"vmid": 9000, "name": "debian-tpl", "type": "qemu", "status": "stopped",
                   "maxcpu": 1, "maxmem": GB, "maxdisk": 4 * GB, "pool": "", "template": 1},
        }  # fmt: skip
        self.aenderungen: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        pfad = request.url.path.removeprefix("/api2/json/")
        if request.method == "GET":
            if pfad == "cluster/resources":
                return httpx.Response(200, json={"data": list(self.vms.values())})
            if pfad == "pools/missa":
                im_pool = [v for v in self.vms.values() if v["pool"] == "missa"]
                return httpx.Response(200, json={"data": {"members": im_pool}})
            if "/tasks/" in pfad:
                return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
            return httpx.Response(404)
        daten = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        daten.update({k: v for k, v in request.url.params.items()})
        self.aenderungen.append((request.method, pfad, daten))
        if pfad.endswith("/clone"):
            vmid = int(daten["newid"])
            self.vms[vmid] = {"vmid": vmid, "name": daten["name"], "type": "qemu",
                              "status": "stopped", "maxcpu": 1, "maxmem": GB,
                              "maxdisk": 4 * GB, "pool": daten["pool"]}  # fmt: skip
        if pfad.endswith("/config"):
            vmid = int(pfad.split("/")[3])
            self.vms[vmid].update(maxcpu=int(daten["cores"]), maxmem=int(daten["memory"]) * 1024**2)
        if pfad.endswith("/resize"):
            self.vms[int(pfad.split("/")[3])]["maxdisk"] = int(daten["size"].rstrip("G")) * GB
        if pfad.endswith("/status/start"):
            self.vms[int(pfad.split("/")[3])]["status"] = "running"
        if request.method == "DELETE":
            self.vms.pop(int(pfad.split("/")[3]), None)
        return httpx.Response(200, json={"data": "UPID:pve-id01:0001:task:"})

    def wege(self) -> list[str]:
        return [f"{m} {p}" for m, p, _ in self.aenderungen]


class FakeAnsible:
    def __init__(self) -> None:
        self.laeufe: list[tuple[str, str, str, dict]] = []
        self.fehler_bei = ""

    async def __call__(self, playbook: Path, ip: str, benutzer: str, variablen: dict) -> str:
        self.laeufe.append((playbook.name, ip, benutzer, variablen))
        if playbook.name == self.fehler_bei:
            from missa.core.werkzeuge import WerkzeugFehler

            raise WerkzeugFehler(f"{playbook.name} ist fehlgeschlagen: Java nicht gefunden")
        return f"PLAY RECAP {ip} : ok=12 changed=8 failed=0"


async def ssh_da(_ip: str, _maximal: float) -> bool:
    return True


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 12, 15, 0, tzinfo=ZONE))


@pytest.fixture
def alarme(tmp_path: Path, uhr) -> Alarme:
    planer = Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)
    return Alarme(planer.datei, ZONE, planer, ruhe_von=None, ruhe_bis=None)


@pytest.fixture
def proxmox() -> FakeProxmox:
    return FakeProxmox()


@pytest.fixture
def ansible() -> FakeAnsible:
    return FakeAnsible()


@pytest.fixture
def protokoll(tmp_path) -> Aktionsprotokoll:
    return Aktionsprotokoll(tmp_path / "missa.db", ZONE)


@pytest.fixture
def verwaltung(proxmox, ansible, alarme, protokoll, tmp_path, uhr) -> VmVerwaltung:
    verwalter = ProxmoxVerwalter(
        "https://pve.test:8006",
        "missa@pve!vms",
        "geheim",
        node="pve-id01",
        id_bereich=range(200, 250),
        transport=httpx.MockTransport(proxmox),
        warte_schritt=0,
    )
    return VmVerwaltung(
        verwalter,
        alarme,
        tmp_path / "missa.db",
        ZONE,
        playbook_ordner=PLAYBOOKS,
        template=9000,
        storage="local-lvm",
        netze=netze_lesen("70:10.0.70.100-10.0.70.149/24:10.0.70.1"),
        grenzen=Grenzen(4, 8, 64, 8, 16, 200),
        ssh_schluessel_pub="ssh-ed25519 AAAAtest missa@ai01",
        verwaltung_ip="10.0.50.10",
        rcon_passwort=lambda: "rcon-geheim",
        ansible=ansible,
        ssh_warten=ssh_da,
        rechte=Rechte(protokoll),
        uhr=uhr,
    )


@pytest.fixture
def kasten(verwaltung, protokoll) -> Werkzeugkasten:
    return Werkzeugkasten(vm_werkzeuge(verwaltung), Rechte(protokoll))


def rufen(kasten, name, argumente, anfrage="", ja=False) -> str:
    return asyncio.run(kasten.ausfuehren(name, argumente, anfrage, nachricht="Ja" if ja else None))


def erstellen_und_warten(kasten, verwaltung, plan_id: int) -> str:
    """Bestätigtes vm_erstellen – und im selben Event-Loop warten, bis der Auftrag fertig ist."""

    async def ablauf() -> str:
        antwort = await kasten.ausfuehren(
            "vm_erstellen", {"plan_id": plan_id}, "a2", nachricht="Ja"
        )
        await verwaltung.warten()
        return antwort

    return asyncio.run(ablauf())


# ----------------------------------------------------------------------- Konfiguration


def test_netze_und_bereich():
    netze = netze_lesen("70:10.0.70.100-10.0.70.149/24:10.0.70.1; 80:10.0.80.50/24:10.0.80.1")
    assert sorted(netze) == [70, 80] and str(netze[80].bis) == "10.0.80.50"
    assert bereich_lesen("200-249") == range(200, 250)
    for falsch in ("70:x/24:1.2.3.4", "0:10.0.0.1/24:10.0.0.1", "70:10.0.0.9-10.0.0.1/24:10.0.0.1"):
        with pytest.raises(ValueError):
            netze_lesen(falsch)
    with pytest.raises(ValueError):
        bereich_lesen("50-60")


# ----------------------------------------------------------------------------- Ablauf


def test_richte_einen_minecraft_server_ein(kasten, verwaltung, proxmox, ansible, protokoll, alarme):
    plan = rufen(kasten, "vm_planen", {"zweck": "Minecraft-Server", "version": "1.21.4"})
    assert "Plan #1 – Minecraft-Server" in plan
    assert "Name: mc01" in plan and "2 CPU, 4 GB RAM, 20 GB Disk" in plan
    assert "VLAN 70, IP 10.0.70.100/24, Gateway 10.0.70.1" in plan
    assert "basis.yml → minecraft.yml (Version 1.21.4)" in plan and "EULA" in plan
    assert "NOCH NICHTS ANGELEGT" in plan and proxmox.aenderungen == []

    rueckfrage = rufen(kasten, "vm_erstellen", {"plan_id": 1}, "a1")
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and proxmox.aenderungen == []

    antwort = erstellen_und_warten(kasten, verwaltung, 1)
    assert "Ich lege mc01 jetzt an" in antwort

    wege = proxmox.wege()
    assert wege == [
        "POST nodes/pve-id01/qemu/9000/clone",
        "POST nodes/pve-id01/qemu/200/config",
        "PUT nodes/pve-id01/qemu/200/resize",
        "POST nodes/pve-id01/qemu/200/status/start",
    ]
    klon, config = proxmox.aenderungen[0][2], proxmox.aenderungen[1][2]
    assert klon == {
        "newid": "200",
        "name": "mc01",
        "pool": "missa",
        "full": "1",
        "storage": "local-lvm",
    }
    assert config["net0"] == "virtio,bridge=vmbr0,tag=70"
    assert config["ipconfig0"] == "ip=10.0.70.100/24,gw=10.0.70.1"
    assert config["sshkeys"] == "ssh-ed25519%20AAAAtest%20missa%40ai01"  # URL-kodiert
    assert config["cores"] == "2" and config["memory"] == "4096"
    assert [lauf[0] for lauf in ansible.laeufe] == ["basis.yml", "minecraft.yml"]
    variablen = ansible.laeufe[1][3]
    assert (
        variablen["rcon_passwort"] == "rcon-geheim" and variablen["minecraft_version"] == "1.21.4"
    )
    assert variablen["rcon_erlaubt_von"] == "10.0.50.10" and variablen["minecraft_ram_mb"] == 3328

    # jeder Schritt steht im Aktionsprotokoll
    schritte = [
        a.werkzeug for a in reversed(protokoll.liste()) if a.werkzeug.startswith("vm_erstellen/")
    ]
    assert schritte == [
        "vm_erstellen/vm-id",
        "vm_erstellen/klonen",
        "vm_erstellen/konfigurieren",
        "vm_erstellen/disk",
        "vm_erstellen/starten",
        "vm_erstellen/ssh",
        "vm_erstellen/playbook basis.yml",
        "vm_erstellen/playbook minecraft.yml",
        "vm_erstellen/fertig",
    ]
    fertig = alarme.liste()[0]
    assert fertig.titel == "VM mc01 ist fertig" and "10.0.70.100:25565" in fertig.text
    assert "mc01" in rufen(kasten, "missa_vms_anzeigen", {})


def test_ueber_den_grenzen_abgelehnt_mit_begruendung(kasten, proxmox):
    antwort = rufen(kasten, "vm_planen", {"zweck": "minecraft", "cpu": 16, "ram_gb": 32})
    assert "ABGELEHNT" in antwort
    assert "16 CPU sind mehr als die erlaubten 4 pro VM" in antwort
    assert "32 GB RAM sind mehr als die erlaubten 8 GB pro VM" in antwort
    vlan = rufen(kasten, "vm_planen", {"zweck": "basis", "vlan": 20})
    assert "VLAN 20 ist nicht erlaubt (erlaubt: 70)" in vlan
    # Pool-Grenze: es läuft schon viel im Pool
    proxmox.vms[201] = {
        "vmid": 201, "name": "gross", "type": "qemu", "status": "running",
        "maxcpu": 4, "maxmem": 14 * GB, "maxdisk": 50 * GB, "pool": "missa",
    }  # fmt: skip
    pool = rufen(kasten, "vm_planen", {"zweck": "minecraft"})
    assert "der Pool hätte dann 18 GB RAM – erlaubt sind 16 GB" in pool
    assert proxmox.aenderungen == []


def test_loeschen_ausserhalb_des_pools_unmoeglich(kasten, verwaltung, proxmox):
    for name in ("wb01", "101", "debian-tpl"):
        antwort = rufen(kasten, "vm_loeschen", {"name": name}, "a1")
        assert "keine VM im Pool „missa“" in antwort
        antwort = rufen(kasten, "vm_loeschen", {"name": name}, "a2", ja=True)
        assert "keine VM im Pool „missa“" in antwort
    # auch direkt über die Proxmox-Schicht: ohne Pool-Mitgliedschaft passiert nichts
    with pytest.raises(Exception, match="nicht zum Pool"):
        asyncio.run(verwaltung.proxmox.loeschen(205))
    with pytest.raises(Exception, match="außerhalb des erlaubten ID-Bereichs"):
        asyncio.run(verwaltung.proxmox.loeschen(101))
    assert proxmox.aenderungen == []


def test_pool_vm_stoppen_und_loeschen_nach_ja(kasten, proxmox):
    proxmox.vms[210] = {
        "vmid": 210, "name": "mc02", "type": "qemu", "status": "running",
        "maxcpu": 2, "maxmem": 4 * GB, "maxdisk": 20 * GB, "pool": "missa",
    }  # fmt: skip
    rueckfrage = rufen(kasten, "vm_loeschen", {"name": "mc02"}, "a1")
    assert "ENDGÜLTIG gelöscht" in rueckfrage and proxmox.aenderungen == []
    rufen(kasten, "vm_loeschen", {"name": "mc02"}, "a2", ja=True)
    assert proxmox.wege() == [
        "POST nodes/pve-id01/qemu/210/status/shutdown",
        "DELETE nodes/pve-id01/qemu/210",
    ]
    assert 210 not in proxmox.vms


def test_fehler_im_playbook_wird_gemeldet(kasten, verwaltung, ansible, alarme):
    ansible.fehler_bei = "minecraft.yml"
    rufen(kasten, "vm_planen", {"zweck": "minecraft"})
    rufen(kasten, "vm_erstellen", {"plan_id": 1}, "a1")
    erstellen_und_warten(kasten, verwaltung, 1)
    fehler = alarme.liste()[0]
    assert fehler.stufe is AlarmStufe.WICHTIG
    assert (
        "Schritt „playbook minecraft.yml“" in fehler.text and "Java nicht gefunden" in fehler.text
    )
    assert "fehler" in rufen(kasten, "missa_vms_anzeigen", {})


def test_unbekannter_zweck_und_katalog(kasten):
    assert "kein Playbook" in rufen(kasten, "vm_planen", {"zweck": "Kaffeemaschine"})
    katalog = rufen(kasten, "playbooks_anzeigen", {})
    assert "minecraft" in katalog and "höchstens 4 CPU" in katalog


# ----------------------------------------------------------------------------- Playbooks


def test_playbooks_sind_gueltig_und_im_katalog():
    katalog = yaml.safe_load((PLAYBOOKS / "katalog.yml").read_text(encoding="utf-8"))
    assert {"basis", "minecraft"} <= set(katalog)
    for eintrag in katalog.values():
        for playbook in eintrag["playbooks"]:
            inhalt = yaml.safe_load((PLAYBOOKS / playbook).read_text(encoding="utf-8"))
            assert isinstance(inhalt, list) and inhalt[0]["hosts"] == "all"
            for aufgabe in inhalt[0]["tasks"]:
                module = [k for k in aufgabe if "." in k]
                assert all(m.startswith("ansible.builtin.") for m in module), aufgabe["name"]
    minecraft = (PLAYBOOKS / "templates" / "server.properties.j2").read_text(encoding="utf-8")
    assert "enable-rcon=true" in minecraft and "white-list=true" in minecraft


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible nicht installiert")
def test_playbooks_syntax():
    for playbook in ("basis.yml", "minecraft.yml"):
        ergebnis = subprocess.run(
            ["ansible-playbook", "--syntax-check", "-i", "localhost,", playbook],
            cwd=PLAYBOOKS,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr


# ----------------------------------------------------------------------------- Einrichtungs-Script


def test_proxmox_missa_vergibt_nur_rechte_im_pool(tmp_path):
    protokoll = tmp_path / "pveum.log"
    pveum = tmp_path / "pveum"
    pveum.write_text(f'#!/bin/bash\necho "$*" >> "{protokoll}"\necho "[]"\n', encoding="utf-8")
    pveum.chmod(0o755)
    ergebnis = subprocess.run(
        ["bash", str(SCRIPT), "--template", "9000", "--storage", "local-lvm", "--vlans", "70,80",
         "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
    )  # fmt: skip
    assert ergebnis.returncode == 0, ergebnis.stderr
    acls = [z for z in ergebnis.stdout.splitlines() if "acl modify" in z]
    pfade = [z.split("acl modify ")[1].split()[0] for z in acls]
    assert pfade == [
        "/pool/missa",
        "/vms/9000",
        "/storage/local-lvm",
        "/sdn/zones/localnetwork/vmbr0/70",
        "/sdn/zones/localnetwork/vmbr0/80",
    ]
    assert "/ " not in " ".join(pfade) + " " and "Administrator" not in ergebnis.stdout
    assert "--roles MissaVMs" in acls[0] and "--roles PVETemplateUser" in acls[1]
    assert "user token add missa@pve vms --privsep 1" in ergebnis.stdout
    # im Dry-Run wurde nur gelesen
    aufrufe = protokoll.read_text(encoding="utf-8").splitlines()
    assert aufrufe and all(" list" in z for z in aufrufe)
    falsch = subprocess.run(
        ["bash", str(SCRIPT), "--template", "x"], capture_output=True, text=True, check=False
    )
    assert falsch.returncode == 1
