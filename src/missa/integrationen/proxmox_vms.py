"""Proxmox mit Schreibrechten – aber nur im Pool ``missa`` und im erlaubten ID-Bereich.

Das Token ``missa@pve!vms`` (``scripts/einrichten/proxmox-missa.sh``) hat Rechte nur auf den
Pool, das erlaubte Storage, die erlaubten VLANs und das Cloud-Init-Template (klonen).
Zusätzlich prüft der Code vor **jeder** ändernden Anfrage, dass die VM im Pool liegt und
ihre ID im erlaubten Bereich ist – eine VM außerhalb des Pools kann Missa weder starten,
stoppen noch löschen.
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from missa.core.werkzeuge import WerkzeugFehler

TIMEOUT = 30.0


class ProxmoxFehler(WerkzeugFehler):
    """Proxmox hat eine Anfrage abgelehnt oder ist nicht erreichbar (Text geht ans Modell)."""


def bereich_lesen(text: str) -> range:
    """``200-249`` → range(200, 250)."""
    von, _, bis = str(text).partition("-")
    try:
        anfang, ende = int(von), int(bis or von)
    except ValueError as fehler:
        raise ValueError(f"Ungültiger VM-ID-Bereich: {text} (z. B. 200-249)") from fehler
    if not 100 <= anfang <= ende:
        raise ValueError(f"Ungültiger VM-ID-Bereich: {text} (ab 100, von ≤ bis)")
    return range(anfang, ende + 1)


class ProxmoxVerwalter:
    """Legt VMs im Pool an, konfiguriert, startet, stoppt und löscht sie – nur dort."""

    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        *,
        node: str,
        pool: str = "missa",
        id_bereich: range = range(200, 250),
        tls_pruefen: bool = True,
        ca_datei: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
        warte_schritt: float = 2.0,
    ) -> None:
        self.url = url.rstrip("/").removesuffix("/api2/json")
        self.token_id = token_id
        self.node = node
        self.pool = pool
        self.id_bereich = id_bereich
        self._kopf = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self.transport = transport
        self.timeout = timeout
        self.warte_schritt = warte_schritt
        self.verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(ca_datei)) if ca_datei else bool(tls_pruefen)
        )

    async def anfrage(self, methode: str, pfad: str, daten: dict[str, Any] | None = None) -> Any:
        adresse = f"{self.url}/api2/json/{pfad.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout, verify=self.verify, trust_env=False
            ) as client:
                if methode == "GET":
                    antwort = await client.get(adresse, params=daten, headers=self._kopf)
                elif methode == "DELETE":
                    antwort = await client.delete(adresse, params=daten, headers=self._kopf)
                else:
                    antwort = await client.request(
                        methode, adresse, data=daten or {}, headers=self._kopf
                    )
        except httpx.HTTPError as fehler:
            raise ProxmoxFehler(
                f"Proxmox nicht erreichbar: {fehler or type(fehler).__name__}"
            ) from fehler
        if antwort.status_code in (401, 403):
            raise ProxmoxFehler(
                f"Proxmox verweigert {methode} {pfad} (HTTP {antwort.status_code}) – das Token "
                f"{self.token_id} hat dafür keine Rechte."
            )
        if antwort.status_code >= 400:
            try:
                grund = antwort.json().get("message") or antwort.json().get("errors")
            except ValueError:
                grund = antwort.text[:200]
            raise ProxmoxFehler(
                f"Proxmox: {methode} {pfad} fehlgeschlagen ({antwort.status_code}): {grund}"
            )
        try:
            return antwort.json().get("data")
        except (ValueError, AttributeError) as fehler:
            raise ProxmoxFehler(f"Proxmox liefert kein gültiges JSON für {pfad}") from fehler

    # -------------------------------------------------------------- Lesen

    async def ressourcen(self) -> list[dict[str, Any]]:
        daten = await self.anfrage("GET", "cluster/resources", {"type": "vm"})
        return [r for r in daten or [] if isinstance(r, dict)]

    async def pool_vms(self) -> list[dict[str, Any]]:
        """VMs im Pool (laut Proxmox) – nur diese darf Missa anfassen."""
        daten = await self.anfrage("GET", f"pools/{self.pool}") or {}
        return [m for m in daten.get("members", []) if m.get("type") in ("qemu", "lxc")]

    async def freie_vmid(self) -> int:
        belegt = {int(r.get("vmid", 0)) for r in await self.ressourcen()}
        for vmid in self.id_bereich:
            if vmid not in belegt:
                return vmid
        raise ProxmoxFehler(
            f"Im erlaubten ID-Bereich {self.id_bereich.start}–{self.id_bereich.stop - 1} ist "
            "keine VM-ID mehr frei."
        )

    async def im_pool(self, vmid: int) -> dict[str, Any]:
        """Die VM aus dem Pool – sonst Fehler (dann wird nichts geändert)."""
        if vmid not in self.id_bereich:
            raise ProxmoxFehler(
                f"VM {vmid} liegt außerhalb des erlaubten ID-Bereichs "
                f"{self.id_bereich.start}–{self.id_bereich.stop - 1} – die fasse ich nicht an."
            )
        for vm in await self.pool_vms():
            if int(vm.get("vmid", 0)) == vmid:
                return vm
        raise ProxmoxFehler(
            f"VM {vmid} gehört nicht zum Pool „{self.pool}“ – die fasse ich nicht an."
        )

    async def warten(self, upid: str, maximal: float = 900) -> None:
        """Wartet, bis eine Proxmox-Aufgabe fertig ist, und prüft ihr Ergebnis."""
        ende = asyncio.get_running_loop().time() + maximal
        pfad = f"nodes/{self.node}/tasks/{quote(upid, safe='')}/status"
        while True:
            status = await self.anfrage("GET", pfad) or {}
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise ProxmoxFehler(
                        f"Proxmox-Aufgabe fehlgeschlagen: {status.get('exitstatus')}"
                    )
                return
            if asyncio.get_running_loop().time() > ende:
                raise ProxmoxFehler("Proxmox-Aufgabe dauert zu lange – abgebrochen.")
            await asyncio.sleep(self.warte_schritt)

    # -------------------------------------------------------------- Schreiben (nur im Pool)

    async def klonen(self, template: int, vmid: int, name: str, storage: str) -> None:
        if vmid not in self.id_bereich:
            raise ProxmoxFehler(f"VM-ID {vmid} liegt außerhalb des erlaubten Bereichs.")
        upid = await self.anfrage(
            "POST",
            f"nodes/{self.node}/qemu/{template}/clone",
            {"newid": vmid, "name": name, "pool": self.pool, "full": 1, "storage": storage},
        )
        await self.warten(str(upid))

    async def konfigurieren(self, vmid: int, werte: dict[str, Any]) -> None:
        await self.im_pool(vmid)
        daten = dict(werte)
        if "sshkeys" in daten:
            # Proxmox erwartet die Schlüssel URL-kodiert (zusätzlich zur Formular-Kodierung).
            daten["sshkeys"] = quote(str(daten["sshkeys"]), safe="")
        await self.anfrage("POST", f"nodes/{self.node}/qemu/{vmid}/config", daten)

    async def disk_vergroessern(self, vmid: int, disk: str, groesse_gb: int) -> None:
        await self.im_pool(vmid)
        await self.anfrage(
            "PUT", f"nodes/{self.node}/qemu/{vmid}/resize", {"disk": disk, "size": f"{groesse_gb}G"}
        )

    async def starten(self, vmid: int) -> None:
        await self.im_pool(vmid)
        await self.warten(
            str(await self.anfrage("POST", f"nodes/{self.node}/qemu/{vmid}/status/start"))
        )

    async def stoppen(self, vmid: int) -> None:
        await self.im_pool(vmid)
        upid = await self.anfrage(
            "POST",
            f"nodes/{self.node}/qemu/{vmid}/status/shutdown",
            {"forceStop": 1, "timeout": 120},
        )
        await self.warten(str(upid))

    async def loeschen(self, vmid: int) -> None:
        vm = await self.im_pool(vmid)
        if vm.get("status") == "running":
            await self.stoppen(vmid)
        upid = await self.anfrage(
            "DELETE",
            f"nodes/{self.node}/qemu/{vmid}",
            {"purge": 1, "destroy-unreferenced-disks": 1},
        )
        await self.warten(str(upid))
