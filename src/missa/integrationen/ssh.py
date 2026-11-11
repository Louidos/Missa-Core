"""SSH zu anderen Hosts als Benutzer ``missa`` – nur fest erlaubte Befehle.

Auf jedem Host richtet Louis von Hand ``scripts/einrichten/missa-benutzer.sh`` ein. Der
SSH-Schlüssel von Missa ist dort an einen festen Befehl gebunden
(``command="/usr/local/lib/missa/missa-befehl"``, ``restrict``): Missa kann keine Shell
öffnen, sondern nur die Befehle aus :data:`BEFEHLE` mit geprüften Angaben aufrufen. Per
sudo darf ``missa`` nur zwei feste Programme starten: ``missa-docker`` (``docker ps``,
``docker stats``, ``docker logs``, ``docker restart``) und ``missa-update`` (``apt-get update``,
``apt-get -y upgrade``, ``reboot``); die Logs (journal) liest ``missa`` über die Gruppe
``systemd-journal``.

Auch hier im Code gilt: Nur Befehle aus :data:`BEFEHLE`, jede Angabe muss zu
:data:`SICHER` passen – Text des Modells landet nie ungeprüft auf einem Host.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from missa.core.werkzeuge import WerkzeugFehler

log = logging.getLogger(__name__)

#: Befehle, die ``missa-befehl`` auf dem Host kennt (und nur diese).
BEFEHLE = frozenset(
    {
        "pruefen",
        "docker-ps",
        "docker-stats",
        "docker-logs",
        "docker-restart",
        "journal",
        "ssh-log",
        "nextcloud-log",
        "apt-update",
        "apt-liste",
        "apt-upgrade",
        "neustart-noetig",
        "neustart",
    }
)
#: Erlaubte Zeichen in jeder Angabe: keine Leerzeichen, Anführungszeichen oder Shell-Zeichen.
SICHER = re.compile(r"^[A-Za-z0-9@%._:,+=/*-]{1,200}$")
#: Gültige Container-Namen (wie Docker sie erlaubt).
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
#: So viel Ausgabe wird höchstens gelesen (Zeichen).
MAX_AUSGABE = 2_000_000
TIMEOUT = 20.0

#: Startet einen Prozess und gibt (Rückgabewert, stdout, stderr) zurück – in Tests ersetzbar.
Ausfuehrer = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]


class SshFehler(WerkzeugFehler):
    """Der Host ist nicht erreichbar oder der Befehl ist fehlgeschlagen (Text geht ans Modell)."""


@dataclass(frozen=True)
class SshHost:
    name: str
    adresse: str
    port: int = 22


def hosts_lesen(text: str) -> dict[str, SshHost]:
    """``wb01=10.0.20.10,int01=10.0.30.20:2222`` → Hosts (Name klein geschrieben)."""
    hosts: dict[str, SshHost] = {}
    for teil in re.split(r"[,;\s]+", text or ""):
        if not teil.strip():
            continue
        name, _, ziel = teil.partition("=")
        name = name.strip().casefold()
        ziel = (ziel or name).strip()
        adresse, port = ziel, 22
        if ziel.count(":") == 1:
            adresse, _, port_text = ziel.partition(":")
            if not port_text.isdigit():
                raise ValueError(f"Ungültiger Port in SSH_HOSTS: {teil}")
            port = int(port_text)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name) or not SICHER.match(adresse):
            raise ValueError(f"Ungültiger Eintrag in SSH_HOSTS: {teil}")
        hosts[name] = SshHost(name, adresse, port)
    return hosts


def angabe_pruefen(wert: object) -> str:
    text = str(wert)
    if not SICHER.match(text):
        raise SshFehler(f"Unzulässige Angabe für einen Host-Befehl: {text!r}")
    return text


def container_name_pruefen(name: object) -> str:
    text = str(name or "").strip()
    if not CONTAINER_NAME.match(text):
        raise SshFehler(f"„{text}“ ist kein gültiger Container-Name.")
    return text


async def prozess_ausfuehren(befehl: list[str], timeout: float) -> tuple[int, str, str]:
    """Startet ``befehl`` ohne Shell und wartet höchstens ``timeout`` Sekunden."""
    prozess = await asyncio.create_subprocess_exec(
        *befehl,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        aus, fehler = await asyncio.wait_for(prozess.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            prozess.kill()
        await prozess.wait()
        raise
    return (
        prozess.returncode or 0,
        aus[:MAX_AUSGABE].decode("utf-8", "replace"),
        fehler[:20_000].decode("utf-8", "replace"),
    )


class SshVerbindung:
    """Führt erlaubte Befehle auf konfigurierten Hosts aus (``ssh missa@host <befehl>``)."""

    def __init__(
        self,
        hosts: dict[str, SshHost],
        *,
        benutzer: str = "missa",
        schluessel: Path | None = None,
        known_hosts: Path | None = None,
        timeout: float = TIMEOUT,
        ausfuehrer: Ausfuehrer = prozess_ausfuehren,
    ) -> None:
        self.hosts = hosts
        self.benutzer = benutzer
        self.schluessel = schluessel
        self.known_hosts = known_hosts
        self.timeout = timeout
        self._ausfuehrer = ausfuehrer

    @property
    def namen(self) -> list[str]:
        return sorted(self.hosts)

    def host(self, name: object) -> SshHost:
        gesucht = str(name or "").strip().casefold()
        if gesucht in self.hosts:
            return self.hosts[gesucht]
        raise SshFehler(
            f"Den Host „{name}“ kenne ich nicht. Eingerichtet sind: "
            + (", ".join(self.namen) or "keine (SSH_HOSTS)")
            + "."
        )

    def kommando(self, host: SshHost, befehl: str, argumente: tuple[str, ...]) -> list[str]:
        """Die ``ssh``-Kommandozeile (ohne Shell; Host-Schlüssel wird streng geprüft)."""
        teile = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={max(1, int(min(self.timeout, 15)))}",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(host.port),
        ]
        if self.schluessel is not None:
            teile += ["-i", str(self.schluessel), "-o", "IdentitiesOnly=yes"]
        if self.known_hosts is not None:
            teile += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        return [*teile, f"{self.benutzer}@{host.adresse}", "--", befehl, *argumente]

    async def befehl(
        self, host_name: object, befehl: str, *argumente: object, timeout: float | None = None
    ) -> str:
        """Führt ``befehl`` auf dem Host aus und gibt die Ausgabe zurück."""
        if befehl not in BEFEHLE:
            raise SshFehler(f"Der Befehl „{befehl}“ ist auf den Hosts nicht erlaubt.")
        host = self.host(host_name)
        geprueft = tuple(angabe_pruefen(a) for a in argumente)
        zeit = timeout or self.timeout
        try:
            code, aus, fehler = await self._ausfuehrer(self.kommando(host, befehl, geprueft), zeit)
        except TimeoutError as fehler_zeit:
            raise SshFehler(f"{host.name} antwortet nicht (nach {zeit:g} s abgebrochen).") from (
                fehler_zeit
            )
        except OSError as fehler_os:
            raise SshFehler(f"SSH konnte nicht gestartet werden: {fehler_os}") from fehler_os
        if code != 0:
            grund = (fehler.strip() or aus.strip() or f"Rückgabewert {code}").splitlines()[-1]
            if code == 255:
                raise SshFehler(f"Keine SSH-Verbindung zu {host.name}: {grund}")
            raise SshFehler(f"{befehl} auf {host.name} fehlgeschlagen: {grund}")
        return aus
