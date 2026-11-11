"""Erreichbarkeit über die öffentliche Domain: DNS, HTTPS-Antwort und gültiges Zertifikat.

Geprüft wird von ai01 aus über den normalen Weg ins Internet (öffentlicher DNS-Name,
Port 443). Ein Test von ganz außerhalb des Netzes braucht später einen externen Host.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

TIMEOUT = 10.0

#: Löst einen Namen auf und gibt die IP-Adressen zurück (wirft ``OSError``).
Aufloeser = Callable[[str], Awaitable[list[str]]]
#: Baut eine TLS-Verbindung auf und gibt das Ablaufdatum des Zertifikats zurück.
ZertifikatLeser = Callable[[str, float], Awaitable[datetime]]


@dataclass(frozen=True)
class DomainErgebnis:
    """Ergebnis einer Prüfung. ``problem`` ist leer, wenn alles in Ordnung ist."""

    domain: str
    problem: str = ""
    adressen: tuple[str, ...] = ()
    http_status: int | None = None
    zertifikat_bis: datetime | None = None

    @property
    def ok(self) -> bool:
        return not self.problem

    def als_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "ok": self.ok,
            "problem": self.problem,
            "adressen": list(self.adressen),
            "http_status": self.http_status,
            "zertifikat_bis": self.zertifikat_bis.isoformat() if self.zertifikat_bis else None,
        }


async def dns_aufloesen(domain: str) -> list[str]:
    """IP-Adressen über den Resolver von ai01 (wie jeder andere Client auch)."""
    loop = asyncio.get_running_loop()
    eintraege = await loop.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    return sorted({str(e[4][0]) for e in eintraege})


async def zertifikat_lesen(domain: str, timeout: float = TIMEOUT) -> datetime:
    """TLS-Verbindung mit voller Prüfung; gibt das Ablaufdatum des Zertifikats zurück.

    Ist das Zertifikat abgelaufen, für einen anderen Namen oder nicht vertrauenswürdig,
    kommt :class:`ssl.SSLCertVerificationError`.
    """
    kontext = ssl.create_default_context()
    _leser, schreiber = await asyncio.wait_for(
        asyncio.open_connection(domain, 443, ssl=kontext, server_hostname=domain),
        timeout=timeout,
    )
    try:
        zertifikat = schreiber.get_extra_info("peercert") or {}
    finally:
        schreiber.close()
        with contextlib.suppress(TimeoutError, OSError, ssl.SSLError):
            await asyncio.wait_for(schreiber.wait_closed(), timeout=2)
    ablauf = zertifikat.get("notAfter")
    if not ablauf:
        raise ssl.SSLError("Zertifikat ohne Ablaufdatum")
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(ablauf), UTC)


class ErreichbarkeitsPruefer:
    """Prüft Domains der Reihe nach: DNS → TLS-Zertifikat → HTTPS-Antwort."""

    def __init__(
        self,
        domains: tuple[str, ...] | list[str],
        *,
        timeout: float = TIMEOUT,
        aufloesen: Aufloeser = dns_aufloesen,
        zertifikat: ZertifikatLeser = zertifikat_lesen,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.domains = tuple(dict.fromkeys(d.strip().casefold() for d in domains if d.strip()))
        self.timeout = timeout
        self._aufloesen = aufloesen
        self._zertifikat = zertifikat
        self.transport = transport

    async def pruefen(self, domain: str) -> DomainErgebnis:
        try:
            adressen = tuple(await asyncio.wait_for(self._aufloesen(domain), self.timeout))
        except (OSError, TimeoutError) as fehler:
            return DomainErgebnis(domain, f"DNS: Name lässt sich nicht auflösen ({fehler})")
        if not adressen:
            return DomainErgebnis(domain, "DNS: keine Adresse gefunden")
        try:
            bis = await self._zertifikat(domain, self.timeout)
        except ssl.SSLCertVerificationError as fehler:
            grund = getattr(fehler, "verify_message", "") or str(fehler)
            return DomainErgebnis(domain, f"Zertifikat ungültig: {grund}", adressen)
        except (OSError, TimeoutError, ssl.SSLError) as fehler:
            grund = str(fehler) or type(fehler).__name__
            return DomainErgebnis(domain, f"Keine HTTPS-Verbindung (Port 443): {grund}", adressen)
        if bis <= datetime.now(UTC):
            return DomainErgebnis(domain, "Zertifikat ist abgelaufen", adressen, None, bis)
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                antwort = await client.get(f"https://{domain}/")
        except httpx.HTTPError as fehler:
            grund = str(fehler) or type(fehler).__name__
            return DomainErgebnis(domain, f"HTTPS antwortet nicht: {grund}", adressen, None, bis)
        if antwort.status_code >= 500:
            return DomainErgebnis(
                domain,
                f"HTTPS antwortet mit Fehler {antwort.status_code}",
                adressen,
                antwort.status_code,
                bis,
            )
        # 2xx, 3xx und auch 401/403 (z. B. API-Key-Prüfung) heißen: der Dienst ist da.
        return DomainErgebnis(domain, "", adressen, antwort.status_code, bis)

    async def alle_pruefen(self) -> list[DomainErgebnis]:
        return list(await asyncio.gather(*(self.pruefen(d) for d in self.domains)))
