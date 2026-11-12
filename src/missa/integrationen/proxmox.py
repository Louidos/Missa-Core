"""Proxmox-API, nur lesend: CPU, RAM und Disk pro VM und Host.

Der Zugang ist ein API-Token mit der Rolle ``PVEAuditor`` (nur lesen, siehe
``scripts/einrichten/proxmox-lesetoken.sh``). Diese Klasse kennt nur GET-Anfragen –
schreiben, starten, stoppen oder löschen kann sie nicht.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from missa.integrationen.monitoring import Messwert, QuellenFehler, ziel_normal

TIMEOUT = 10.0
#: Diese Arten von Einträgen aus ``/cluster/resources`` sind Rechner.
RECHNER = ("qemu", "lxc", "node")


class ProxmoxLeser:
    """Liest Ressourcen aus der Proxmox-API (``/api2/json``), ausschließlich per GET."""

    name = "proxmox"

    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        *,
        tls_pruefen: bool = True,
        ca_datei: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/").removesuffix("/api2/json")
        self.token_id = token_id
        self._kopf = {
            "Authorization": f"PVEAPIToken={token_id}={token_secret}",
            "Accept": "application/json",
        }
        self.transport = transport
        self.timeout = timeout
        self.verify: ssl.SSLContext | bool
        if ca_datei is not None:
            self.verify = ssl.create_default_context(cafile=str(ca_datei))
        else:
            self.verify = bool(tls_pruefen)

    async def lesen(self, pfad: str, params: dict[str, str] | None = None) -> Any:
        """GET ``/api2/json/<pfad>`` und gibt ``data`` zurück. Es gibt bewusst nur GET."""
        adresse = f"{self.url}/api2/json/{pfad.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout, verify=self.verify, trust_env=False
            ) as client:
                antwort = await client.get(adresse, params=params, headers=self._kopf)
        except httpx.HTTPError as fehler:
            raise QuellenFehler(
                f"Proxmox ({self.url}) nicht erreichbar: {fehler or type(fehler).__name__}"
            ) from fehler
        if antwort.status_code in (401, 403):
            raise QuellenFehler(
                f"Proxmox lehnt das Token {self.token_id} ab (HTTP {antwort.status_code}) – "
                "Token und Rolle PVEAuditor prüfen"
            )
        if antwort.status_code >= 400:
            raise QuellenFehler(f"Proxmox antwortet mit HTTP {antwort.status_code} auf {pfad}")
        try:
            return antwort.json().get("data")
        except (ValueError, AttributeError) as fehler:
            raise QuellenFehler(f"Proxmox liefert kein gültiges JSON für {pfad}") from fehler

    async def ressourcen(self) -> list[dict[str, Any]]:
        """Alle VMs, Container und Hosts aus ``/cluster/resources``."""
        daten = await self.lesen("cluster/resources")
        if not isinstance(daten, list):
            raise QuellenFehler("Proxmox: /cluster/resources liefert keine Liste")
        return [r for r in daten if isinstance(r, dict) and r.get("type") in RECHNER]

    async def messwerte(self) -> list[Messwert]:
        werte: list[Messwert] = []
        for eintrag in await self.ressourcen():
            if eintrag["type"] == "node":
                ziel = ziel_normal(eintrag.get("node"))
            else:
                ziel = ziel_normal(eintrag.get("name")) or f"vm{eintrag.get('vmid', '?')}"
            if not ziel:
                continue
            laeuft = eintrag.get("status") in ("running", "online")
            werte.append(Messwert(ziel, "laeuft", 1.0 if laeuft else 0.0, "", self.name))
            if not laeuft:
                continue
            cpu = eintrag.get("cpu")
            if isinstance(cpu, int | float):
                werte.append(Messwert(ziel, "cpu", round(float(cpu) * 100, 1), "%", self.name))
            for metrik, wert, maximal in (
                ("ram", eintrag.get("mem"), eintrag.get("maxmem")),
                ("disk", eintrag.get("disk"), eintrag.get("maxdisk")),
            ):
                # Bei VMs (qemu) kennt Proxmox die Disk-Belegung meist nicht (disk = 0).
                if (
                    isinstance(wert, int | float)
                    and isinstance(maximal, int | float)
                    and maximal > 0
                    and wert > 0
                ):
                    anteil = round(float(wert) / float(maximal) * 100, 1)
                    werte.append(Messwert(ziel, metrik, anteil, "%", self.name))
        return werte


# ----------------------------------------------------------------------------- Inventar


@dataclass(frozen=True)
class VmEintrag:
    """Eine VM oder ein Container, wie Missa ihn im Homelab kennt."""

    vmid: int
    name: str
    typ: str  # qemu oder lxc
    node: str
    status: str
    cpus: int
    ram_mb: int
    disk_gb: int
    vlans: tuple[int, ...] = ()
    ips: tuple[str, ...] = ()
    pool: str = ""
    tags: tuple[str, ...] = ()
    ip_quelle: str = ""  # agent, cloud-init, lxc oder leer

    def zeile(self) -> str:
        art = "Container" if self.typ == "lxc" else "VM"
        teile = [f"{self.name} ({art} {self.vmid} auf {self.node}, {self.status})"]
        teile.append("IP " + ", ".join(self.ips) if self.ips else "IP unbekannt")
        if self.vlans:
            teile.append("VLAN " + ", ".join(str(v) for v in self.vlans))
        teile.append(
            f"{self.cpus} CPU, {self.ram_mb // 1024 if self.ram_mb >= 1024 else self.ram_mb} "
            f"{'GB' if self.ram_mb >= 1024 else 'MB'} RAM, {self.disk_gb} GB Disk"
        )
        if self.pool:
            teile.append(f"Pool {self.pool}")
        return "- " + " | ".join(teile)

    def als_dict(self) -> dict[str, Any]:
        return {
            "vmid": self.vmid,
            "name": self.name,
            "typ": self.typ,
            "node": self.node,
            "status": self.status,
            "cpus": self.cpus,
            "ram_mb": self.ram_mb,
            "disk_gb": self.disk_gb,
            "vlans": list(self.vlans),
            "ips": list(self.ips),
            "pool": self.pool,
            "tags": list(self.tags),
            "ip_quelle": self.ip_quelle,
        }


def _optionen(text: str) -> dict[str, str]:
    """``virtio=AA:BB,bridge=vmbr0,tag=70`` → ``{"virtio": "AA:BB", …, "tag": "70"}``."""
    werte = {}
    for teil in str(text or "").split(","):
        schluessel, _, wert = teil.partition("=")
        if schluessel.strip():
            werte[schluessel.strip()] = wert.strip()
    return werte


def netze_lesen(config: dict[str, Any]) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """VLAN-Tags und feste IPs (Cloud-Init ``ipconfigN`` bzw. LXC ``ip=``) aus der Konfiguration."""
    vlans: list[int] = []
    ips: list[str] = []
    for schluessel in sorted(config):
        if schluessel.startswith("net") and schluessel[3:].isdigit():
            optionen = _optionen(config[schluessel])
            if optionen.get("tag", "").isdigit():
                vlans.append(int(optionen["tag"]))
            ip = optionen.get("ip", "")
            if ip and ip not in ("dhcp", "manual"):
                ips.append(ip.split("/")[0])
        if schluessel.startswith("ipconfig") and schluessel[8:].isdigit():
            ip = _optionen(config[schluessel]).get("ip", "")
            if ip and ip != "dhcp":
                ips.append(ip.split("/")[0])
    return tuple(dict.fromkeys(vlans)), tuple(dict.fromkeys(ips))


def agent_ips(daten: Any) -> tuple[str, ...]:
    """IPv4/IPv6 aus ``agent/network-get-interfaces`` (ohne Loopback und Link-Local)."""
    ergebnis = daten.get("result", []) if isinstance(daten, dict) else []
    ips = []
    for schnittstelle in ergebnis if isinstance(ergebnis, list) else []:
        if not isinstance(schnittstelle, dict) or schnittstelle.get("name") == "lo":
            continue
        for adresse in schnittstelle.get("ip-addresses") or []:
            ip = str(adresse.get("ip-address", ""))
            if ip and not ip.startswith(("127.", "::1", "fe80", "169.254.")):
                ips.append(ip)
    return tuple(dict.fromkeys(sorted(ips, key=lambda i: (":" in i, i))))


def lxc_ips(daten: Any) -> tuple[str, ...]:
    ips = []
    for schnittstelle in daten if isinstance(daten, list) else []:
        if not isinstance(schnittstelle, dict) or schnittstelle.get("name") == "lo":
            continue
        for feld in ("inet", "inet6"):
            ip = str(schnittstelle.get(feld) or "").split("/")[0]
            if ip and not ip.startswith(("127.", "::1", "fe80")):
                ips.append(ip)
    return tuple(dict.fromkeys(ips))


async def inventar_lesen(leser: ProxmoxLeser) -> list[VmEintrag]:
    """Alle VMs und Container mit Konfiguration, VLAN und IP (Guest-Agent, wo vorhanden)."""
    eintraege = []
    for r in await leser.ressourcen():
        if r.get("type") not in ("qemu", "lxc") or r.get("template"):
            continue
        node, vmid, typ = str(r.get("node", "")), int(r.get("vmid", 0)), str(r["type"])
        try:
            config = await leser.lesen(f"nodes/{node}/{typ}/{vmid}/config") or {}
        except QuellenFehler:
            config = {}
        vlans, feste_ips = netze_lesen(config if isinstance(config, dict) else {})
        ips, quelle = (), ""
        if r.get("status") == "running":
            try:
                if typ == "qemu":
                    ips = agent_ips(
                        await leser.lesen(f"nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
                    )
                    quelle = "agent" if ips else ""
                else:
                    ips = lxc_ips(await leser.lesen(f"nodes/{node}/lxc/{vmid}/interfaces"))
                    quelle = "lxc" if ips else ""
            except QuellenFehler:
                ips = ()  # kein Guest-Agent oder kein Recht dafür
        if not ips and feste_ips:
            ips, quelle = feste_ips, "konfiguration"
        tags = tuple(
            t
            for t in str(r.get("tags") or config.get("tags", "") or "").replace(",", ";").split(";")
            if t
        )
        eintraege.append(
            VmEintrag(
                vmid=vmid,
                name=ziel_normal(r.get("name")) or f"vm{vmid}",
                typ=typ,
                node=node,
                status=str(r.get("status", "")),
                cpus=int(r.get("maxcpu") or 0),
                ram_mb=int((r.get("maxmem") or 0) // (1024 * 1024)),
                disk_gb=int((r.get("maxdisk") or 0) // (1024**3)),
                vlans=vlans,
                ips=ips,
                pool=str(r.get("pool") or ""),
                tags=tags,
                ip_quelle=quelle,
            )
        )
    return sorted(eintraege, key=lambda e: e.vmid)
