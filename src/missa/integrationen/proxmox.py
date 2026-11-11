"""Proxmox-API, nur lesend: CPU, RAM und Disk pro VM und Host.

Der Zugang ist ein API-Token mit der Rolle ``PVEAuditor`` (nur lesen, siehe
``scripts/einrichten/proxmox-lesetoken.sh``). Diese Klasse kennt nur GET-Anfragen –
schreiben, starten, stoppen oder löschen kann sie nicht.
"""

from __future__ import annotations

import ssl
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
