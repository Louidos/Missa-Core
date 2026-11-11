"""Domains prüfen: Ablaufdatum per RDAP und DNS-Einträge gegen die erwarteten Werte.

**RDAP** (Registration Data Access Protocol) ist der Nachfolger von WHOIS. Welcher Server
für eine Endung (``.ch``, ``.com`` …) zuständig ist, steht in der offiziellen Liste der IANA
(``RDAP_BOOTSTRAP_URL``). Das ist die einzige Abfrage nach außen bei Zertifikaten und
Domains. Manche Registries (z. B. SWITCH für ``.ch``) veröffentlichen kein Ablaufdatum –
dann sagt Missa das ehrlich.

**DNS** fragt der Resolver von ai01 (dnspython) – A, AAAA, CNAME, MX, TXT, NS, CAA.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

from missa.integrationen.monitoring import QuellenFehler

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
TIMEOUT = 15.0
DNS_TYPEN = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "CAA")

#: Fragt einen DNS-Eintrag ab: (Name, Typ) → Werte (leer, wenn es keinen gibt).
DnsAbfrage = Callable[[str, str], Awaitable[list[str]]]


def wert_normal(typ: str, wert: str) -> str:
    """Vergleichbare Form: klein, ohne Punkt am Ende, TXT ohne Anführungszeichen."""
    text = " ".join(str(wert).split())
    if typ == "TXT":
        return text.replace('" "', "").strip('"')
    return text.casefold().rstrip(".")


def erwartet_lesen(text: str) -> dict[tuple[str, str], set[str]]:
    """``cloud.example.ch A 203.0.113.10; example.ch MX 10 mail.example.ch`` → Erwartung."""
    erwartet: dict[tuple[str, str], set[str]] = {}
    for eintrag in (text or "").split(";"):
        teile = eintrag.split(None, 2)
        if not teile:
            continue
        if len(teile) < 3:
            raise ValueError(
                f"Ungültiger Eintrag in DNS_ERWARTET (Name Typ Wert): {eintrag.strip()}"
            )
        name, typ, wert = teile[0].casefold().rstrip("."), teile[1].upper(), teile[2]
        if typ not in DNS_TYPEN:
            raise ValueError(
                f"DNS_ERWARTET: Typ {typ} wird nicht unterstützt ({', '.join(DNS_TYPEN)})"
            )
        erwartet.setdefault((name, typ), set()).add(wert_normal(typ, wert))
    return erwartet


async def dns_abfragen(name: str, typ: str) -> list[str]:
    """DNS-Abfrage über den Resolver von ai01. Kein Eintrag → leere Liste."""
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    try:
        antwort = await dns.asyncresolver.resolve(name, typ, lifetime=10)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as fehler:
        raise QuellenFehler(f"DNS-Abfrage {name} {typ} fehlgeschlagen: {fehler}") from fehler
    return sorted(wert_normal(typ, r.to_text()) for r in antwort)


def registrierte_domain(name: str) -> str:
    """``cloud.louis-degiorgi.ch`` → ``louis-degiorgi.ch`` (die letzten beiden Teile)."""
    teile = name.casefold().rstrip(".").split(".")
    return ".".join(teile[-2:])


class RdapClient:
    """Fragt das Ablaufdatum einer Domain beim zuständigen RDAP-Server ab."""

    def __init__(
        self,
        bootstrap_url: str = BOOTSTRAP_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.bootstrap_url = bootstrap_url
        self.transport = transport
        self.timeout = timeout
        self._server: dict[str, str] | None = None
        self._sperre = asyncio.Lock()

    async def _json(self, url: str) -> Any:
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout, follow_redirects=True
            ) as client:
                antwort = await client.get(
                    url, headers={"Accept": "application/rdap+json, application/json"}
                )
        except httpx.HTTPError as fehler:
            raise QuellenFehler(f"RDAP nicht erreichbar ({url}): {fehler}") from fehler
        if antwort.status_code == 404:
            return None
        if antwort.status_code >= 400:
            raise QuellenFehler(f"RDAP antwortet mit HTTP {antwort.status_code} ({url})")
        try:
            return antwort.json()
        except ValueError as fehler:
            raise QuellenFehler(f"RDAP liefert kein JSON ({url})") from fehler

    async def server(self, endung: str) -> str | None:
        """Basis-URL des RDAP-Servers für eine Endung (aus der IANA-Liste, einmal geladen)."""
        async with self._sperre:
            if self._server is None:
                daten = await self._json(self.bootstrap_url) or {}
                server: dict[str, str] = {}
                for eintrag in daten.get("services", []):
                    with contextlib.suppress(TypeError, ValueError, IndexError):
                        endungen, urls = eintrag[0], eintrag[1]
                        https = [u for u in urls if u.startswith("https://")] or urls
                        for e in endungen:
                            server[str(e).casefold()] = https[0]
                self._server = server
        return self._server.get(endung.casefold())

    async def ablauf(self, domain: str) -> datetime | None:
        """Ablaufdatum der Domain; ``None``, wenn die Registry keins veröffentlicht."""
        endung = domain.rsplit(".", 1)[-1]
        basis = await self.server(endung)
        if basis is None:
            raise QuellenFehler(f"Für .{endung} gibt es keinen RDAP-Server in der IANA-Liste")
        daten = await self._json(f"{basis.rstrip('/')}/domain/{domain}")
        if not isinstance(daten, dict):
            raise QuellenFehler(f"{domain}: Die Registry kennt die Domain nicht (RDAP)")
        for ereignis in daten.get("events", []):
            if isinstance(ereignis, dict) and ereignis.get("eventAction") == "expiration":
                with contextlib.suppress(TypeError, ValueError):
                    return datetime.fromisoformat(str(ereignis["eventDate"]).replace("Z", "+00:00"))
        return None
