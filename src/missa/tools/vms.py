"""VMs in Proxmox erstellen und per Playbook einrichten – mit Plan, Rückfrage und harten Grenzen.

Ablauf („Richte einen Minecraft-Server ein“):

1. ``vm_planen`` stellt einen Plan zusammen (Template, Ressourcen, VLAN, IP, Name, Playbooks
   aus ``playbooks/katalog.yml``) und prüft die Grenzen (``VM_MAX_*`` pro VM,
   ``VM_POOL_MAX_*`` für den ganzen Pool, erlaubte VLANs, ID-Bereich). Über den Grenzen
   wird abgelehnt – mit Begründung. Angelegt wird dabei nichts.
2. ``vm_erstellen`` ist ``zerstoererisch``: erst nach Louis' Ja. Dann läuft im Hintergrund:
   VM-ID wählen → aus dem Cloud-Init-Template klonen (in den Pool) → CPU, RAM, VLAN, IP,
   SSH-Schlüssel setzen → Disk vergrößern → starten → auf SSH warten → Ansible-Playbooks
   → fertig melden und ins Homelab-Wissen aufnehmen. Jeder Schritt steht im
   Aktionsprotokoll.
3. Starten, Stoppen und Löschen gehen nur für VMs im Pool ``missa`` (``zerstoererisch``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.proxmox_vms import ProxmoxFehler, ProxmoxVerwalter

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vm_plaene (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    daten TEXT NOT NULL,
    erstellt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vm_auftraege (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    vmid INTEGER,
    ip TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    schritt TEXT NOT NULL DEFAULT '',
    fehler TEXT NOT NULL DEFAULT '',
    erstellt TEXT NOT NULL,
    geaendert TEXT NOT NULL
);
"""

NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
GB = 1024**3


def _utc(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@dataclass(frozen=True)
class Netz:
    """Ein erlaubtes VLAN mit IP-Bereich für Missas VMs."""

    vlan: int
    von: ipaddress.IPv4Address
    bis: ipaddress.IPv4Address
    prefix: int
    gateway: str

    def text(self) -> str:
        return f"VLAN {self.vlan} ({self.von}–{self.bis}/{self.prefix}, Gateway {self.gateway})"


def netze_lesen(text: str) -> dict[int, Netz]:
    """``70:10.0.70.100-10.0.70.149/24:10.0.70.1; 80:…`` → erlaubte Netze."""
    netze: dict[int, Netz] = {}
    for eintrag in (text or "").split(";"):
        if not eintrag.strip():
            continue
        try:
            vlan, bereich, gateway = (t.strip() for t in eintrag.split(":"))
            spanne, _, prefix = bereich.partition("/")
            von, _, bis = spanne.partition("-")
            netz = Netz(
                int(vlan),
                ipaddress.IPv4Address(von.strip()),
                ipaddress.IPv4Address((bis or von).strip()),
                int(prefix or 24),
                str(ipaddress.IPv4Address(gateway)),
            )
        except ValueError as fehler:
            raise ValueError(
                f"Ungültiger Eintrag in VM_NETZE: {eintrag.strip()} "
                "(Format: VLAN:von-bis/Präfix:Gateway)"
            ) from fehler
        if netz.von > netz.bis or not 1 <= netz.vlan <= 4094:
            raise ValueError(f"Ungültiger Eintrag in VM_NETZE: {eintrag.strip()}")
        netze[netz.vlan] = netz
    return netze


@dataclass(frozen=True)
class Grenzen:
    vm_cpu: int = 4
    vm_ram_gb: int = 8
    vm_disk_gb: int = 64
    pool_cpu: int = 8
    pool_ram_gb: int = 16
    pool_disk_gb: int = 200


@dataclass
class VmPlan:
    zweck: str
    name: str
    cpu: int
    ram_gb: int
    disk_gb: int
    vlan: int
    ip: str
    playbooks: list[str]
    beschreibung: str
    variablen: dict[str, Any] = field(default_factory=dict)
    hinweis: str = ""
    id: int = 0


AnsibleAusfuehrer = Callable[[Path, str, str, dict[str, Any]], Awaitable[str]]
SshWarten = Callable[[str, float], Awaitable[bool]]


async def port_offen(ip: str, maximal: float = 300, port: int = 22) -> bool:
    """Wartet, bis ``ip:port`` Verbindungen annimmt (neue VM bootet noch)."""
    ende = asyncio.get_running_loop().time() + maximal
    while asyncio.get_running_loop().time() < ende:
        try:
            _l, schreiber = await asyncio.wait_for(asyncio.open_connection(ip, port), 5)
        except (OSError, TimeoutError):
            await asyncio.sleep(5)
            continue
        schreiber.close()
        with contextlib.suppress(OSError):
            await schreiber.wait_closed()
        return True
    return False


class AnsibleLaeufer:
    """Startet ``ansible-playbook`` gegen genau eine neue VM (ohne Shell)."""

    def __init__(self, befehl: str, schluessel: Path, timeout: float = 2700) -> None:
        self.befehl = befehl
        self.schluessel = schluessel
        self.timeout = timeout

    async def __call__(
        self, playbook: Path, ip: str, benutzer: str, variablen: dict[str, Any]
    ) -> str:
        umgebung = {
            **os.environ,
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_RETRY_FILES_ENABLED": "0",
            # Die VM ist gerade frisch aus dem eigenen Template entstanden – ihren Host-Schlüssel
            # kann es noch nirgends geben.
            "ANSIBLE_HOST_KEY_CHECKING": "False",
        }
        try:
            prozess = await asyncio.create_subprocess_exec(
                self.befehl,
                "-i",
                f"{ip},",
                "-u",
                benutzer,
                "--private-key",
                str(self.schluessel),
                "-e",
                json.dumps(variablen),
                str(playbook),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=umgebung,
                cwd=str(playbook.parent),
            )
        except OSError as fehler:
            raise WerkzeugFehler(
                f"ansible-playbook lässt sich nicht starten ({fehler}) – ist ansible-core auf ai01 "
                "installiert (ANSIBLE_PLAYBOOK)?"
            ) from fehler
        try:
            ausgabe, _ = await asyncio.wait_for(prozess.communicate(), self.timeout)
        except TimeoutError as fehler:
            prozess.kill()
            raise WerkzeugFehler(f"{playbook.name} dauert zu lange – abgebrochen.") from fehler
        text = ausgabe.decode("utf-8", "replace")
        if prozess.returncode != 0:
            ende = "\n".join(text.strip().splitlines()[-8:])
            raise WerkzeugFehler(f"{playbook.name} ist fehlgeschlagen:\n{ende}")
        return "\n".join(z for z in text.splitlines() if z.startswith(("PLAY RECAP", ip)))


class VmVerwaltung:
    """Plant, erstellt und verwaltet VMs im Pool ``missa``."""

    def __init__(
        self,
        proxmox: ProxmoxVerwalter,
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        *,
        playbook_ordner: Path,
        template: int,
        storage: str,
        netze: dict[int, Netz],
        grenzen: Grenzen,
        bridge: str = "vmbr0",
        disk: str = "scsi0",
        ci_benutzer: str = "missa",
        ssh_schluessel_pub: str = "",
        nameserver: str = "",
        verwaltung_ip: str = "",
        rcon_passwort: Callable[[], str] | None = None,
        ansible: AnsibleAusfuehrer | None = None,
        ssh_warten: SshWarten = port_offen,
        rechte: Any = None,
        homelab: Any = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.proxmox = proxmox
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self.playbook_ordner = playbook_ordner
        self.template = template
        self.storage = storage
        self.netze = netze
        self.grenzen = grenzen
        self.bridge = bridge
        self.disk = disk
        self.ci_benutzer = ci_benutzer
        self.ssh_schluessel_pub = ssh_schluessel_pub
        self.nameserver = nameserver
        self.verwaltung_ip = verwaltung_ip
        self._rcon_passwort = rcon_passwort or (lambda: "")
        self.ansible = ansible
        self.ssh_warten = ssh_warten
        self.rechte = rechte
        self.homelab = homelab
        self._uhr = uhr or alarme.jetzt
        self._laufend: dict[int, asyncio.Task[None]] = {}
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Katalog

    def katalog(self) -> dict[str, dict[str, Any]]:
        import yaml

        try:
            daten = yaml.safe_load(
                (self.playbook_ordner / "katalog.yml").read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as fehler:
            raise WerkzeugFehler(f"Der Playbook-Katalog ist nicht lesbar: {fehler}") from fehler
        return {str(k).casefold(): v for k, v in (daten or {}).items() if isinstance(v, dict)}

    def _eintrag(self, zweck: str) -> tuple[str, dict[str, Any]]:
        gesucht = " ".join(str(zweck or "").casefold().split())
        katalog = self.katalog()
        if gesucht in katalog:
            return gesucht, katalog[gesucht]
        for name, eintrag in katalog.items():
            woerter = [str(w).casefold() for w in eintrag.get("stichwoerter", [])]
            if any(w in gesucht.split() or w == gesucht for w in woerter) or name in gesucht:
                return name, eintrag
        raise WerkzeugFehler(
            f"Für „{zweck}“ habe ich kein Playbook. Möglich: "
            + ", ".join(f"{n} ({e.get('beschreibung', '')})" for n, e in katalog.items())
            + ". Ein neues Playbook kommt als Datei in playbooks/ und in den Katalog."
        )

    def playbooks_anzeigen(self) -> str:
        zeilen = ["Was ich einrichten kann (playbooks/katalog.yml):"]
        for name, e in self.katalog().items():
            zeilen.append(
                f"- {name}: {e.get('beschreibung', '')} – Standard {e.get('cpu')} CPU, "
                f"{e.get('ram_gb')} GB RAM, {e.get('disk_gb')} GB Disk "
                f"({' → '.join(e.get('playbooks', []))})"
            )
        g = self.grenzen
        zeilen.append(
            f"Grenzen: pro VM höchstens {g.vm_cpu} CPU, {g.vm_ram_gb} GB RAM, {g.vm_disk_gb} GB "
            f"Disk; der ganze Pool höchstens {g.pool_cpu} CPU, {g.pool_ram_gb} GB RAM, "
            f"{g.pool_disk_gb} GB Disk. Erlaubte VLANs: "
            + ", ".join(str(v) for v in sorted(self.netze))
            + "."
        )
        return "\n".join(zeilen)

    # -------------------------------------------------------------- Plan

    def _auftraege(self) -> list[dict[str, Any]]:
        with verbinden(self.datei) as db:
            return [dict(z) for z in db.execute("SELECT * FROM vm_auftraege ORDER BY id DESC")]

    def _plan(self, plan_id: Any) -> VmPlan:
        try:
            nummer = int(str(plan_id).strip().lstrip("#"))
        except ValueError as fehler:
            raise WerkzeugFehler(f"plan_id „{plan_id}“ ist keine Zahl.") from fehler
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT * FROM vm_plaene WHERE id = ?", (nummer,)).fetchone()
        if zeile is None:
            raise WerkzeugFehler(f"Einen Plan #{nummer} gibt es nicht. Erst vm_planen aufrufen.")
        return VmPlan(**{**json.loads(zeile["daten"]), "id": nummer})

    async def _belegt(self) -> tuple[set[str], set[str]]:
        """Namen und IPs, die schon vergeben sind (Proxmox, Homelab-Wissen, eigene Aufträge)."""
        namen = {str(r.get("name", "")).casefold() for r in await self.proxmox.ressourcen()}
        ips: set[str] = set()
        if self.homelab is not None:
            for eintrag in self.homelab.eintraege():
                namen.add(eintrag.name)
                ips.update(eintrag.ips)
        for auftrag in self._auftraege():
            if auftrag["status"] not in ("geloescht", "fehler"):
                namen.add(auftrag["name"])
                if auftrag["ip"]:
                    ips.add(auftrag["ip"])
        return namen, ips

    def _freie_ip(self, netz: Netz, belegt: set[str]) -> str:
        adresse = netz.von
        while adresse <= netz.bis:
            if str(adresse) not in belegt and str(adresse) != netz.gateway:
                return str(adresse)
            adresse += 1
        raise WerkzeugFehler(f"Im {netz.text()} ist keine IP mehr frei.")

    def _zahl(self, wert: Any, standard: int, name: str) -> int:
        if wert in (None, ""):
            return int(standard)
        try:
            return int(float(str(wert).replace(",", ".")))
        except ValueError as fehler:
            raise WerkzeugFehler(f"{name} muss eine Zahl sein.") from fehler

    async def grenzen_pruefen(self, plan: VmPlan, ohne_vmid: int | None = None) -> list[str]:
        """Gründe, warum der Plan nicht geht (leer = in Ordnung)."""
        g = self.grenzen
        gruende = []
        if plan.cpu > g.vm_cpu:
            gruende.append(f"{plan.cpu} CPU sind mehr als die erlaubten {g.vm_cpu} pro VM")
        if plan.ram_gb > g.vm_ram_gb:
            gruende.append(
                f"{plan.ram_gb} GB RAM sind mehr als die erlaubten {g.vm_ram_gb} GB pro VM"
            )
        if plan.disk_gb > g.vm_disk_gb:
            gruende.append(
                f"{plan.disk_gb} GB Disk sind mehr als die erlaubten {g.vm_disk_gb} GB pro VM"
            )
        if min(plan.cpu, plan.ram_gb, plan.disk_gb) < 1:
            gruende.append("CPU, RAM und Disk müssen mindestens 1 sein")
        if plan.vlan not in self.netze:
            gruende.append(
                f"VLAN {plan.vlan} ist nicht erlaubt (erlaubt: "
                + ", ".join(str(v) for v in sorted(self.netze))
                + ")"
            )
        im_pool = [v for v in await self.proxmox.pool_vms() if v.get("vmid") != ohne_vmid]
        cpu = sum(int(v.get("maxcpu") or 0) for v in im_pool) + plan.cpu
        ram = sum(int(v.get("maxmem") or 0) for v in im_pool) / GB + plan.ram_gb
        disk = sum(int(v.get("maxdisk") or 0) for v in im_pool) / GB + plan.disk_gb
        if cpu > g.pool_cpu:
            gruende.append(f"der Pool hätte dann {cpu} CPU – erlaubt sind {g.pool_cpu}")
        if ram > g.pool_ram_gb:
            gruende.append(f"der Pool hätte dann {ram:g} GB RAM – erlaubt sind {g.pool_ram_gb} GB")
        if disk > g.pool_disk_gb:
            gruende.append(
                f"der Pool hätte dann {disk:g} GB Disk – erlaubt sind {g.pool_disk_gb} GB"
            )
        return gruende

    def _plan_text(self, plan: VmPlan) -> str:
        netz = self.netze.get(plan.vlan)
        zeilen = [
            f"Plan #{plan.id} – {plan.beschreibung}:",
            f"- Name: {plan.name}, VM-ID: die nächste freie aus "
            f"{self.proxmox.id_bereich.start}–{self.proxmox.id_bereich.stop - 1}, Pool "
            f"„{self.proxmox.pool}“",
            f"- Template: {self.template} (Cloud-Init), Storage {self.storage}, "
            f"Host {self.proxmox.node}",
            f"- Ressourcen: {plan.cpu} CPU, {plan.ram_gb} GB RAM, {plan.disk_gb} GB Disk",
            f"- Netz: VLAN {plan.vlan}, IP {plan.ip}"
            + (f"/{netz.prefix}, Gateway {netz.gateway}" if netz else ""),
            f"- Einrichtung: {' → '.join(plan.playbooks)}"
            + (
                f" (Version {plan.variablen['minecraft_version']})"
                if "minecraft_version" in plan.variablen
                else ""
            ),
        ]
        if plan.hinweis:
            zeilen.append(f"- Hinweis: {plan.hinweis}")
        return "\n".join(zeilen)

    async def planen(
        self,
        zweck: str,
        name: str | None = None,
        cpu: int | None = None,
        ram_gb: int | None = None,
        disk_gb: int | None = None,
        vlan: int | None = None,
        version: str | None = None,
    ) -> str:
        schluessel, eintrag = self._eintrag(zweck)
        namen, ips = await self._belegt()
        if name:
            name = str(name).strip().casefold()
            if not NAME.match(name):
                raise WerkzeugFehler("Der Name darf nur a–z, 0–9 und - enthalten (z. B. mc01).")
            if name in namen:
                raise WerkzeugFehler(f"Den Namen „{name}“ gibt es schon.")
        else:
            vorlage = str(eintrag.get("namen_vorlage") or schluessel[:3])
            name = next(
                f"{vorlage}{n:02d}" for n in range(1, 100) if f"{vorlage}{n:02d}" not in namen
            )
        vlan_nr = self._zahl(vlan, min(self.netze) if self.netze else 0, "vlan")
        netz = self.netze.get(vlan_nr)
        variablen = dict(eintrag.get("variablen") or {})
        if version:
            if not re.fullmatch(r"latest|[0-9][0-9a-z.\-]{0,20}", str(version).strip()):
                raise WerkzeugFehler(
                    f"„{version}“ ist keine Version, die ich kenne (z. B. 1.21.4)."
                )
            variablen["minecraft_version"] = str(version).strip()
        plan = VmPlan(
            zweck=schluessel,
            name=name,
            cpu=self._zahl(cpu, eintrag.get("cpu", 1), "cpu"),
            ram_gb=self._zahl(ram_gb, eintrag.get("ram_gb", 1), "ram_gb"),
            disk_gb=self._zahl(disk_gb, eintrag.get("disk_gb", 16), "disk_gb"),
            vlan=vlan_nr,
            ip=self._freie_ip(netz, ips) if netz else "",
            playbooks=list(eintrag.get("playbooks", ["basis.yml"])),
            beschreibung=str(eintrag.get("beschreibung", schluessel)),
            variablen=variablen,
            hinweis=" ".join(str(eintrag.get("hinweis", "")).split()),
        )
        gruende = await self.grenzen_pruefen(plan)
        if gruende:
            raise WerkzeugFehler(
                "ABGELEHNT – das geht über die festen Grenzen: " + "; ".join(gruende) + ". "
                "Sag Louis die Gründe; mit weniger Ressourcen oder nach dem Löschen einer "
                "Missa-VM geht es."
            )
        daten = {k: v for k, v in asdict(plan).items() if k != "id"}
        with verbinden(self.datei) as db:
            cursor = db.execute(
                "INSERT INTO vm_plaene (daten, erstellt) VALUES (?, ?)",
                (json.dumps(daten), _utc(self.jetzt())),
            )
            plan.id = int(cursor.lastrowid or 0)
        return (
            self._plan_text(plan)
            + "\nNOCH NICHTS ANGELEGT. Zeig Louis den Plan und frag, ob du ihn so umsetzen sollst. "
            f"Erst nach seinem Ja rufe vm_erstellen mit plan_id={plan.id} auf."
        )

    # -------------------------------------------------------------- Erstellen

    async def erstellen_vorschau(self, plan_id: int | str) -> str:
        plan = self._plan(plan_id)
        gruende = await self.grenzen_pruefen(plan)
        if gruende:
            raise WerkzeugFehler("ABGELEHNT – inzwischen über den Grenzen: " + "; ".join(gruende))
        return (
            self._plan_text(plan)
            + "\nDie VM wird angelegt, gestartet und per Playbook eingerichtet "
            "(dauert einige Minuten)."
        )

    async def erstellen(self, plan_id: int | str) -> str:
        """Legt die VM nach Louis' Ja an – im Hintergrund, jeder Schritt ins Protokoll."""
        plan = self._plan(plan_id)
        gruende = await self.grenzen_pruefen(plan)
        if gruende:
            raise WerkzeugFehler("ABGELEHNT – inzwischen über den Grenzen: " + "; ".join(gruende))
        jetzt = _utc(self.jetzt())
        with verbinden(self.datei) as db:
            cursor = db.execute(
                "INSERT INTO vm_auftraege (plan_id, name, ip, status, schritt, erstellt, "
                "geaendert) "
                "VALUES (?, ?, ?, 'laeuft', 'start', ?, ?)",
                (plan.id, plan.name, plan.ip, jetzt, jetzt),
            )
            auftrag = int(cursor.lastrowid or 0)
        self._laufend[auftrag] = asyncio.create_task(
            self._ablauf(auftrag, plan), name=f"vm-{plan.name}"
        )
        return (
            f"Ich lege {plan.name} jetzt an (Auftrag #{auftrag}): klonen, einrichten, starten, "
            f"Playbooks {' → '.join(plan.playbooks)}. Das dauert einige Minuten – ich melde mich, "
            "wenn die VM fertig ist. Den Stand zeigt missa_vms_anzeigen."
        )

    async def warten(self) -> None:
        """Wartet auf alle laufenden Aufträge (für Tests und beim Herunterfahren)."""
        if self._laufend:
            await asyncio.gather(*self._laufend.values(), return_exceptions=True)

    def _setzen(self, auftrag: int, **felder: Any) -> None:
        felder["geaendert"] = _utc(self.jetzt())
        zuweisung = ", ".join(f"{k} = ?" for k in felder)
        with verbinden(self.datei) as db:
            db.execute(
                f"UPDATE vm_auftraege SET {zuweisung} WHERE id = ?", (*felder.values(), auftrag)
            )

    def _schritt(
        self, auftrag: int, plan: VmPlan, schritt: str, ergebnis: str, status: str = "ausgefuehrt"
    ) -> None:
        self._setzen(auftrag, schritt=schritt)
        if self.rechte is not None:
            self.rechte.protokollieren(
                werkzeug=f"vm_erstellen/{schritt}",
                stufe="zerstoererisch",
                parameter={"auftrag": auftrag, "vm": plan.name, "plan": plan.id},
                bestaetigt=True,
                status=status,
                ergebnis=ergebnis,
                kanal="hintergrund",
                account="missa",
            )

    async def _ablauf(self, auftrag: int, plan: VmPlan) -> None:
        schritt = "start"
        try:
            netz = self.netze[plan.vlan]
            schritt = "vm-id"
            vmid = await self.proxmox.freie_vmid()
            self._setzen(auftrag, vmid=vmid)
            self._schritt(auftrag, plan, schritt, f"VM-ID {vmid}")
            schritt = "klonen"
            await self.proxmox.klonen(self.template, vmid, plan.name, self.storage)
            self._schritt(
                auftrag, plan, schritt, f"aus Template {self.template} in Pool {self.proxmox.pool}"
            )
            schritt = "konfigurieren"
            werte: dict[str, Any] = {
                "cores": plan.cpu,
                "memory": plan.ram_gb * 1024,
                "net0": f"virtio,bridge={self.bridge},tag={plan.vlan}",
                "ipconfig0": f"ip={plan.ip}/{netz.prefix},gw={netz.gateway}",
                "ciuser": self.ci_benutzer,
                "agent": "1",
                "tags": f"missa;{plan.zweck}",
                "description": f"Angelegt von Missa am {self.jetzt():%d.%m.%Y %H:%M} "
                f"(Plan #{plan.id})",
            }
            if self.ssh_schluessel_pub:
                werte["sshkeys"] = self.ssh_schluessel_pub
            if self.nameserver:
                werte["nameserver"] = self.nameserver
            await self.proxmox.konfigurieren(vmid, werte)
            self._schritt(
                auftrag,
                plan,
                schritt,
                f"{plan.cpu} CPU, {plan.ram_gb} GB, VLAN {plan.vlan}, IP {plan.ip}",
            )
            schritt = "disk"
            await self.proxmox.disk_vergroessern(vmid, self.disk, plan.disk_gb)
            self._schritt(auftrag, plan, schritt, f"{self.disk} auf {plan.disk_gb} GB")
            schritt = "starten"
            await self.proxmox.starten(vmid)
            self._schritt(auftrag, plan, schritt, "gestartet")
            schritt = "ssh"
            if not await self.ssh_warten(plan.ip, 300):
                raise WerkzeugFehler(f"{plan.ip} ist nach 5 Minuten per SSH nicht erreichbar.")
            self._schritt(auftrag, plan, schritt, f"{plan.ip}:22 erreichbar")
            if self.ansible is None:
                raise WerkzeugFehler("ansible-playbook ist nicht eingerichtet (ANSIBLE_PLAYBOOK).")
            variablen = self._variablen(plan)
            for playbook in plan.playbooks:
                schritt = f"playbook {playbook}"
                datei = (self.playbook_ordner / playbook).resolve()
                if self.playbook_ordner.resolve() not in datei.parents or not datei.is_file():
                    raise WerkzeugFehler(f"Das Playbook {playbook} gibt es nicht.")
                ergebnis = await self.ansible(datei, plan.ip, self.ci_benutzer, variablen)
                self._schritt(auftrag, plan, schritt, ergebnis or "ok")
            schritt = "fertig"
            self._setzen(auftrag, status="fertig", schritt=schritt)
            self._schritt(auftrag, plan, schritt, f"{plan.name} läuft auf {plan.ip}")
            if self.homelab is not None:
                with contextlib.suppress(Exception):
                    await self.homelab.scannen()
            text = (
                f"{plan.beschreibung} läuft auf {plan.name} "
                f"({plan.ip}, VLAN {plan.vlan}, VM {vmid})."
            )
            if plan.zweck == "minecraft":
                text += (
                    f" Spieler verbinden sich mit {plan.ip}:25565 (für außen braucht es eine "
                    "Portweiterleitung oder Subdomain). Für RCON in /etc/missa/missa.env "
                    f"MINECRAFT_RCON_HOST={plan.ip} eintragen."
                )
            self.alarme.melden(
                AlarmStufe.INFO,
                f"VM {plan.name} ist fertig",
                text,
                schluessel=f"vm:fertig:{auftrag}",
                quelle="vms",
            )
        except Exception as fehler:
            grund = str(fehler) or type(fehler).__name__
            if not isinstance(fehler, WerkzeugFehler):
                log.exception("VM-Erstellung %s: Schritt %s", plan.name, schritt)
            self._setzen(auftrag, status="fehler", fehler=grund[:500])
            self._schritt(auftrag, plan, schritt, grund, status="fehlgeschlagen")
            self.alarme.melden(
                AlarmStufe.WICHTIG,
                f"VM {plan.name} konnte nicht fertig eingerichtet werden",
                f"Schritt „{schritt}“: {grund}\nFalls die VM schon angelegt ist, bleibt sie zum "
                f"Nachsehen stehen; löschen geht mit „Lösch {plan.name}“ (nach deinem Ja).",
                schluessel=f"vm:fehler:{auftrag}",
                quelle="vms",
            )
        finally:
            self._laufend.pop(auftrag, None)

    def _variablen(self, plan: VmPlan) -> dict[str, Any]:
        variablen = dict(plan.variablen)
        erlaubt = self.verwaltung_ip or "any"
        variablen.setdefault("ssh_erlaubt_von", erlaubt)
        if plan.zweck == "minecraft":
            variablen.setdefault("minecraft_ram_mb", max(1024, plan.ram_gb * 1024 - 768))
            variablen.setdefault("rcon_erlaubt_von", erlaubt)
            variablen["rcon_passwort"] = self._rcon_passwort() or secrets.token_urlsafe(18)
        return variablen

    # -------------------------------------------------------------- Pool-VMs

    async def _vm(self, name: Any) -> dict[str, Any]:
        gesucht = str(name or "").strip().casefold()
        for vm in await self.proxmox.pool_vms():
            if gesucht in (str(vm.get("name", "")).casefold(), str(vm.get("vmid"))):
                await self.proxmox.im_pool(int(vm["vmid"]))  # prüft auch den ID-Bereich
                return vm
        raise WerkzeugFehler(
            f"„{name}“ ist keine VM im Pool „{self.proxmox.pool}“ – andere VMs kann ich weder "
            "starten, stoppen noch löschen."
        )

    async def anzeigen(self) -> str:
        vms = await self.proxmox.pool_vms()
        zeilen = [f"VMs im Pool „{self.proxmox.pool}“ ({len(vms)}):"]
        zeilen += [
            f"- {v.get('name')} (VM {v.get('vmid')}, {v.get('status')}, {v.get('maxcpu')} CPU, "
            f"{int(v.get('maxmem') or 0) // GB} GB RAM, {int(v.get('maxdisk') or 0) // GB} GB Disk)"
            for v in vms
        ] or ["- (keine)"]
        auftraege = self._auftraege()[:5]
        if auftraege:
            zeilen.append("Letzte Aufträge:")
            for a in auftraege:
                zeile = (
                    f"- #{a['id']} {a['name']} ({a['ip']}): {a['status']}, Schritt {a['schritt']}"
                )
                if a["fehler"]:
                    zeile += f" – {a['fehler'][:150]}"
                zeilen.append(zeile)
        return "\n".join(zeilen)

    async def starten_vorschau(self, name: str) -> str:
        vm = await self._vm(name)
        return (
            f"Die VM {vm.get('name')} (VM {vm.get('vmid')}, Pool {self.proxmox.pool}) "
            "wird gestartet."
        )

    async def starten(self, name: str) -> str:
        vm = await self._vm(name)
        await self.proxmox.starten(int(vm["vmid"]))
        return f"{vm.get('name')} ist gestartet."

    async def stoppen_vorschau(self, name: str) -> str:
        vm = await self._vm(name)
        return (
            f"Die VM {vm.get('name')} (VM {vm.get('vmid')}) wird heruntergefahren (nach 2 Minuten "
            "hart gestoppt). Dienste darauf sind dann nicht erreichbar."
        )

    async def stoppen(self, name: str) -> str:
        vm = await self._vm(name)
        await self.proxmox.stoppen(int(vm["vmid"]))
        return f"{vm.get('name')} ist gestoppt."

    async def loeschen_vorschau(self, name: str) -> str:
        vm = await self._vm(name)
        return (
            f"Die VM {vm.get('name')} (VM {vm.get('vmid')}, Pool {self.proxmox.pool}) wird "
            "ENDGÜLTIG gelöscht – samt Disk. Das lässt sich nicht rückgängig machen."
        )

    async def loeschen(self, name: str) -> str:
        vm = await self._vm(name)
        await self.proxmox.loeschen(int(vm["vmid"]))
        with verbinden(self.datei) as db:
            db.execute(
                "UPDATE vm_auftraege SET status = 'geloescht' WHERE vmid = ?", (int(vm["vmid"]),)
            )
        if self.homelab is not None:
            with contextlib.suppress(Exception):
                await self.homelab.scannen()
        return f"{vm.get('name')} ist gelöscht."


def vm_werkzeuge(verwaltung: VmVerwaltung) -> list[Werkzeug]:
    name = {"type": "string", "description": "Name der VM im Pool missa, z. B. mc01"}
    return [
        Werkzeug(
            "vm_planen",
            "Plant eine neue VM in Proxmox für einen Zweck (z. B. „Richte einen Minecraft-Server "
            "ein“): Template, Ressourcen, VLAN, IP, Name und Playbooks – mit Prüfung der Grenzen. "
            "Legt noch nichts an.",
            {
                "type": "object",
                "properties": {
                    "zweck": {"type": "string", "description": "z. B. minecraft oder basis"},
                    "name": {"type": "string", "description": "optional, z. B. mc01"},
                    "cpu": {"type": "integer"},
                    "ram_gb": {"type": "integer"},
                    "disk_gb": {"type": "integer"},
                    "vlan": {"type": "integer"},
                    "version": {"type": "string", "description": "z. B. Minecraft-Version 1.21.4"},
                },
                "required": ["zweck"],
            },
            verwaltung.planen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "vm_erstellen",
            "Legt die geplante VM an und richtet sie ein – nur nach Louis' Ja.",
            {
                "type": "object",
                "properties": {"plan_id": {"type": "integer", "description": "aus vm_planen"}},
                "required": ["plan_id"],
            },
            verwaltung.erstellen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.erstellen_vorschau,
        ),
        Werkzeug(
            "missa_vms_anzeigen",
            "Zeigt die VMs im Pool missa und den Stand der letzten Erstellungs-Aufträge.",
            {"type": "object", "properties": {}},
            verwaltung.anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "playbooks_anzeigen",
            "Zeigt, was Missa auf neuen VMs einrichten kann, und die festen Grenzen.",
            {"type": "object", "properties": {}},
            verwaltung.playbooks_anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "vm_starten",
            "Startet eine VM aus dem Pool missa – nur nach Louis' Ja.",
            {"type": "object", "properties": {"name": name}, "required": ["name"]},
            verwaltung.starten,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.starten_vorschau,
        ),
        Werkzeug(
            "vm_stoppen",
            "Fährt eine VM aus dem Pool missa herunter – nur nach Louis' Ja.",
            {"type": "object", "properties": {"name": name}, "required": ["name"]},
            verwaltung.stoppen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.stoppen_vorschau,
        ),
        Werkzeug(
            "vm_loeschen",
            "Löscht eine VM aus dem Pool missa endgültig – nur nach Louis' Ja. Andere VMs sind "
            "unmöglich.",
            {"type": "object", "properties": {"name": name}, "required": ["name"]},
            verwaltung.loeschen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.loeschen_vorschau,
        ),
    ]


__all__ = [
    "AnsibleLaeufer",
    "Grenzen",
    "Netz",
    "ProxmoxFehler",
    "VmVerwaltung",
    "netze_lesen",
    "vm_werkzeuge",
]
