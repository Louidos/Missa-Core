"""Erreichbarkeit über die öffentliche Domain: DNS, HTTPS-Antwort und gültiges Zertifikat.

Geprüft wird von ai01 aus über den normalen Weg ins Internet (öffentlicher DNS-Name,
Standard Port 443 und Pfad ``/``). Ein Ziel kann einen eigenen Port und Pfad haben,
z. B. ``cloud.example.ch:8443`` oder ``ki.example.ch:8443/health`` (:class:`PruefZiel`).
Ein Test von ganz außerhalb des Netzes braucht später einen externen Host.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

TIMEOUT = 10.0
STANDARD_PORT = 443
STANDARD_PFAD = "/"
#: Längster erlaubter Pfad (mit Abfrage), damit niemand beliebig lange URLs einträgt.
MAX_PFAD = 200

#: Löst einen Namen auf und gibt die IP-Adressen zurück (wirft ``OSError``).
Aufloeser = Callable[[str], Awaitable[list[str]]]
#: Baut eine TLS-Verbindung zu (Name, Zeitlimit, Port) auf und gibt das Ablaufdatum zurück.
ZertifikatLeser = Callable[[str, float, int], Awaitable[datetime]]

_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_PFAD = re.compile(r"/[A-Za-z0-9._~!$&'()*+;=:@%/?-]*")
_BEISPIEL = "z. B. cloud.example.ch, cloud.example.ch:8443 oder ki.example.ch:8443/health"
_NUR_DOMAINS = f"Geprüft werden nur öffentliche Domainnamen ({_BEISPIEL})."


class ZielFehler(ValueError):
    """Ein Prüfziel ist ungültig; die Meldung nennt den Eintrag und was daran falsch ist."""


@dataclass(frozen=True, order=True)
class PruefZiel:
    """Was geprüft wird: öffentlicher Domainname, Port (Standard 443) und Pfad (Standard ``/``)."""

    domain: str
    port: int = STANDARD_PORT
    pfad: str = STANDARD_PFAD

    @property
    def schluessel(self) -> str:
        """Eindeutiger Name, z. B. für den Zustand in der Datenbank.

        Für Port 443 und Pfad ``/`` ist das der reine Domainname – so bleiben Einträge aus
        älteren Versionen gültig. Sonst ``domain:port/pfad`` (Port nur, wenn nicht 443).
        """
        text = self.domain if self.port == STANDARD_PORT else f"{self.domain}:{self.port}"
        return text if self.pfad == STANDARD_PFAD else text + self.pfad

    @property
    def url(self) -> str:
        host = self.domain if self.port == STANDARD_PORT else f"{self.domain}:{self.port}"
        return f"https://{host}{self.pfad}"

    def __str__(self) -> str:
        return self.schluessel


def _ist_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return False
    return True


def domain_pruefen(text: object) -> str:
    """Prüft einen öffentlichen Domainnamen (keine IP-Adresse, kein ``localhost``)."""
    roh = str(text if text is not None else "").strip()
    domain = roh.casefold().rstrip(".")
    if not domain:
        raise ZielFehler(f"Der Domainname fehlt ({_BEISPIEL}).")
    if any(z.isspace() for z in roh):
        raise ZielFehler(f"„{roh}“ enthält Leerzeichen – erlaubt ist nur ein Domainname.")
    if _ist_ip(domain):
        raise ZielFehler(f"„{roh}“ ist eine IP-Adresse. {_NUR_DOMAINS}")
    if domain == "localhost" or domain.endswith(".localhost"):
        raise ZielFehler(f"„{roh}“ ist nicht erlaubt. {_NUR_DOMAINS}")
    teile = domain.split(".")
    if (
        len(domain) > 253
        or len(teile) < 2
        or not all(_LABEL.fullmatch(t) for t in teile)
        or teile[-1].isdigit()
    ):
        raise ZielFehler(f"„{roh}“ ist kein gültiger Domainname ({_BEISPIEL}).")
    return domain


def port_pruefen(wert: object) -> int:
    """Port zwischen 1 und 65535."""
    text = str(wert if wert is not None else "").strip()
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise ZielFehler(f"Port „{text}“ ist ungültig – erlaubt sind 1 bis 65535.")
    return int(text)


def pfad_pruefen(wert: object) -> str:
    """Pfad ab ``/`` (z. B. ``/health``), ohne Leerzeichen und ohne ``#``."""
    text = str(wert if wert is not None else "").strip() or STANDARD_PFAD
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > MAX_PFAD or not _PFAD.fullmatch(text):
        raise ZielFehler(
            f"Pfad „{text}“ ist ungültig – erlaubt ist ein Pfad wie /health (ohne Leerzeichen, "
            f"höchstens {MAX_PFAD} Zeichen)."
        )
    return text


def ziel_lesen(text: object) -> PruefZiel:
    """Versteht ``domain``, ``domain:port``, ``domain/pfad`` und ``domain:port/pfad``.

    Wirft :class:`ZielFehler` mit einer Meldung, die den falschen Eintrag nennt.
    """
    if isinstance(text, PruefZiel):
        return text
    roh = str(text if text is not None else "").strip()
    if not roh:
        raise ZielFehler(f"Das Ziel fehlt ({_BEISPIEL}).")
    if any(z.isspace() for z in roh):
        raise ZielFehler(f"„{roh}“ enthält Leerzeichen ({_BEISPIEL}).")
    if _ist_ip(roh.partition("/")[0]):
        raise ZielFehler(f"„{roh}“ ist eine IP-Adresse. {_NUR_DOMAINS}")
    if "://" in roh or re.match(r"[a-z][a-z0-9+-]*:(?!\d)", roh, re.IGNORECASE):
        raise ZielFehler(f"„{roh}“: bitte ohne https:// angeben ({_BEISPIEL}).")
    host, schraegstrich, rest = roh.partition("/")
    pfad = "/" + rest if schraegstrich else STANDARD_PFAD
    port: object = STANDARD_PORT
    if host.startswith("["):
        raise ZielFehler(f"„{roh}“ ist eine IP-Adresse. {_NUR_DOMAINS}")
    if _ist_ip(host):
        raise ZielFehler(f"„{roh}“ ist eine IP-Adresse. {_NUR_DOMAINS}")
    if host.count(":") == 1:
        host, port = host.split(":")
    try:
        return PruefZiel(domain_pruefen(host), port_pruefen(port), pfad_pruefen(pfad))
    except ZielFehler as fehler:
        if str(fehler).startswith(f"„{roh}“"):
            raise
        raise ZielFehler(f"„{roh}“: {fehler}") from None


def ziele_lesen(eintraege: object) -> tuple[PruefZiel, ...]:
    """Liste (oder kommagetrennter Text) → eindeutige Ziele in der gegebenen Reihenfolge."""
    if isinstance(eintraege, str):
        eintraege = eintraege.replace(";", ",").split(",")
    ziele = (ziel_lesen(e) for e in eintraege if str(e).strip())  # type: ignore[attr-defined]
    return tuple(dict.fromkeys(ziele))


@dataclass(frozen=True)
class DomainErgebnis:
    """Ergebnis einer Prüfung. ``problem`` ist leer, wenn alles in Ordnung ist."""

    domain: str
    problem: str = ""
    adressen: tuple[str, ...] = ()
    http_status: int | None = None
    zertifikat_bis: datetime | None = None
    port: int = STANDARD_PORT
    pfad: str = STANDARD_PFAD

    @property
    def ok(self) -> bool:
        return not self.problem

    @property
    def ziel(self) -> PruefZiel:
        return PruefZiel(self.domain, self.port, self.pfad)

    @property
    def schluessel(self) -> str:
        """Name des Ziels (reiner Domainname bei Port 443 und Pfad ``/``)."""
        return self.ziel.schluessel

    def als_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "port": self.port,
            "pfad": self.pfad,
            "ziel": self.schluessel,
            "ok": self.ok,
            "problem": self.problem,
            "adressen": list(self.adressen),
            "http_status": self.http_status,
            "zertifikat_bis": self.zertifikat_bis.isoformat() if self.zertifikat_bis else None,
        }


async def dns_aufloesen(domain: str) -> list[str]:
    """IP-Adressen über den Resolver von ai01 (wie jeder andere Client auch)."""
    loop = asyncio.get_running_loop()
    eintraege = await loop.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    return sorted({str(e[4][0]) for e in eintraege})


async def zertifikat_lesen(
    domain: str, timeout: float = TIMEOUT, port: int = STANDARD_PORT
) -> datetime:
    """TLS-Verbindung mit voller Prüfung; gibt das Ablaufdatum des Zertifikats zurück.

    Ist das Zertifikat abgelaufen, für einen anderen Namen oder nicht vertrauenswürdig,
    kommt :class:`ssl.SSLCertVerificationError`.
    """
    kontext = ssl.create_default_context()
    _leser, schreiber = await asyncio.wait_for(
        asyncio.open_connection(domain, port, ssl=kontext, server_hostname=domain),
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
    """Prüft Ziele der Reihe nach: DNS → TLS-Zertifikat → HTTPS-Antwort (Port und Pfad je Ziel)."""

    def __init__(
        self,
        ziele: tuple[str | PruefZiel, ...] | list[str | PruefZiel] = (),
        *,
        timeout: float = TIMEOUT,
        aufloesen: Aufloeser = dns_aufloesen,
        zertifikat: ZertifikatLeser = zertifikat_lesen,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.ziele = ziele_lesen(ziele)
        self.timeout = timeout
        self._aufloesen = aufloesen
        self._zertifikat = zertifikat
        self.transport = transport

    @property
    def domains(self) -> tuple[str, ...]:
        """Namen der Ziele (reiner Domainname bei Port 443 und Pfad ``/``)."""
        return tuple(z.schluessel for z in self.ziele)

    async def pruefen(self, ziel: str | PruefZiel) -> DomainErgebnis:
        ziel = ziel_lesen(ziel)
        domain, port, pfad = ziel.domain, ziel.port, ziel.pfad

        def ergebnis(problem: str = "", *rest: Any) -> DomainErgebnis:
            return DomainErgebnis(domain, problem, *rest, port=port, pfad=pfad)

        try:
            adressen = tuple(await asyncio.wait_for(self._aufloesen(domain), self.timeout))
        except (OSError, TimeoutError) as fehler:
            return ergebnis(f"DNS: Name lässt sich nicht auflösen ({fehler})")
        if not adressen:
            return ergebnis("DNS: keine Adresse gefunden")
        try:
            bis = await self._zertifikat(domain, self.timeout, port)
        except ssl.SSLCertVerificationError as fehler:
            grund = getattr(fehler, "verify_message", "") or str(fehler)
            return ergebnis(f"Zertifikat ungültig: {grund}", adressen)
        except (OSError, TimeoutError, ssl.SSLError) as fehler:
            grund = str(fehler) or type(fehler).__name__
            return ergebnis(f"Keine HTTPS-Verbindung (Port {port}): {grund}", adressen)
        if bis <= datetime.now(UTC):
            return ergebnis("Zertifikat ist abgelaufen", adressen, None, bis)
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                antwort = await client.get(ziel.url)
        except httpx.HTTPError as fehler:
            grund = str(fehler) or type(fehler).__name__
            return ergebnis(f"HTTPS antwortet nicht: {grund}", adressen, None, bis)
        if antwort.status_code >= 500:
            return ergebnis(
                f"HTTPS antwortet mit Fehler {antwort.status_code}",
                adressen,
                antwort.status_code,
                bis,
            )
        # 2xx, 3xx und auch 401/403 (z. B. API-Key-Prüfung) heißen: der Dienst ist da.
        return ergebnis("", adressen, antwort.status_code, bis)

    async def alle_pruefen(self) -> list[DomainErgebnis]:
        return list(await asyncio.gather(*(self.pruefen(z) for z in self.ziele)))
