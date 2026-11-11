"""Gefälschte Hosts für Tests der SSH-Anbindung (kein echtes ssh)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from missa.integrationen.ssh import SshVerbindung, hosts_lesen

HOSTS = "wb01=10.0.20.10,int01=10.0.30.20,rp01=10.0.70.10"

Antwort = str | tuple[int, str, str] | Callable[[list[str]], Any] | Exception


class FakeHosts:
    """Ersetzt den ssh-Prozess: Antworten je (Host, Befehl), alle Aufrufe werden gemerkt."""

    def __init__(self, hosts: str = HOSTS) -> None:
        self.hosts = hosts_lesen(hosts)
        self.adressen = {h.adresse: h.name for h in self.hosts.values()}
        self.antworten: dict[tuple[str, str], Antwort] = {}
        self.aufrufe: list[tuple[str, str, list[str]]] = []
        self.kommandos: list[list[str]] = []
        self.offline: set[str] = set()

    async def __call__(self, kommando: list[str], timeout: float) -> tuple[int, str, str]:
        self.kommandos.append(kommando)
        trenner = kommando.index("--")
        host = self.adressen[kommando[trenner - 1].split("@", 1)[1]]
        befehl, *argumente = kommando[trenner + 1 :]
        self.aufrufe.append((host, befehl, argumente))
        if host in self.offline:
            return 255, "", f"ssh: connect to host {host} port 22: Connection refused"
        antwort = self.antworten.get((host, befehl), "")
        if callable(antwort) and not isinstance(antwort, Exception):
            antwort = antwort(argumente)
        if isinstance(antwort, Exception):
            raise antwort
        if isinstance(antwort, tuple):
            return antwort
        return 0, str(antwort), ""

    def verbindung(self) -> SshVerbindung:
        return SshVerbindung(self.hosts, ausfuehrer=self)

    def befehle(self, befehl: str) -> list[tuple[str, list[str]]]:
        return [(h, a) for h, b, a in self.aufrufe if b == befehl]


def ps(*container: dict[str, str]) -> str:
    """Ausgabe von ``docker ps --format '{{json .}}'``."""
    return "\n".join(json.dumps(c) for c in container)


def container(name: str, status: str, zustand: str = "running", image: str = "") -> dict:
    return {"Names": name, "Status": status, "State": zustand, "Image": image or f"{name}:latest"}
