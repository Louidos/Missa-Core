"""Docker-Container auf den Hosts: Übersicht, Logs, Neustart nach Ja und Hänger-Erkennung.

Alles läuft über SSH als Benutzer ``missa`` mit fest erlaubten Befehlen
(:mod:`missa.integrationen.ssh`). Lesen ist ``lesen``; ein Neustart ist
``zerstoererisch`` und läuft nur nach Louis' ausdrücklichem Ja – die Rechte-Schicht
fragt nach und schreibt jeden Schritt ins Aktionsprotokoll.

Der :class:`ContainerDienst` schaut außerdem regelmäßig (Standard: alle 5 Minuten),
ob ein Container hängt (Neustart-Schleife, ungesund, hängt beim Start, abgestürzt), und
meldet das als ``wichtig`` – die Rückkehr als ``info``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.docker import Container, ps_lesen
from missa.integrationen.logs import in_stuecke, relevante_zeilen
from missa.integrationen.ssh import SshFehler, SshVerbindung, container_name_pruefen

log = logging.getLogger(__name__)

#: Antwortet ein Host so lange nicht per SSH, gibt es einen Hinweis.
HOST_STILL = timedelta(minutes=15)


def zeilen_lesen(wert: object, standard: int = 200) -> int:
    try:
        zahl = int(str(wert)) if wert not in (None, "") else standard
    except ValueError as fehler:
        raise WerkzeugFehler("zeilen muss eine Zahl sein.") from fehler
    return max(1, min(zahl, 5000))


def teil_lesen(wert: object) -> int:
    try:
        return max(1, int(str(wert))) if wert not in (None, "") else 1
    except ValueError as fehler:
        raise WerkzeugFehler("teil muss eine Zahl sein (1 = die neuesten Zeilen).") from fehler


def teil_text(zeilen: list[str], teil: int, kopf: str, weiter: str) -> str:
    """Ein Stück eines langen Logs mit Hinweis auf weitere Teile."""
    stuecke = in_stuecke(zeilen)
    if teil > len(stuecke):
        return f"{kopf}\nEs gibt nur {len(stuecke)} Teil(e)."
    text = [f"{kopf} – Teil {teil} von {len(stuecke)} (Teil 1 = die neuesten Zeilen):"]
    text += stuecke[teil - 1] or ["(keine Zeilen)"]
    if teil < len(stuecke):
        text.append(
            f"(Ältere Zeilen: {weiter} mit teil={teil + 1}. Für eine gründliche Analyse sehr "
            "langer Logs kannst du Louis anbieten, das heute Nacht zu machen – "
            "nachtaufgabe_anlegen.)"
        )
    return "\n".join(text)


class ContainerDienst:
    """Container lesen und – nach Ja – neu starten; Hänger erkennen und melden."""

    def __init__(
        self,
        ssh: SshVerbindung,
        alarme: Alarme | None = None,
        *,
        ignorieren: Iterable[str] = (),
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.ssh = ssh
        self.alarme = alarme
        self.ignorieren = {i.strip().casefold() for i in ignorieren if i.strip()}
        self._uhr = uhr or (alarme.jetzt if alarme is not None else datetime.now)
        #: Letzter bekannter Stand je Host (für die API und die Hänger-Erkennung).
        self.zuletzt: dict[str, list[Container]] = {}
        self.stand: dict[str, datetime] = {}
        self._vorher: dict[tuple[str, str], Container] = {}
        self._gemeldet: set[tuple[str, str]] = set()
        self._ssh_fehler_seit: dict[str, datetime] = {}
        self._ssh_gemeldet: set[str] = set()

    def jetzt(self) -> datetime:
        return self._uhr()

    def ignoriert(self, container: Container) -> bool:
        return bool(
            {container.name.casefold(), f"{container.host}:{container.name}".casefold()}
            & self.ignorieren
        )

    # -------------------------------------------------------------- Lesen

    async def liste(self, host: str, mit_werten: bool = True) -> list[Container]:
        name = self.ssh.host(host).name
        ps = await self.ssh.befehl(name, "docker-ps")
        stats = ""
        if mit_werten:
            try:
                stats = await self.ssh.befehl(name, "docker-stats", timeout=30)
            except SshFehler as fehler:
                log.info("docker stats auf %s: %s", name, fehler)
        container = ps_lesen(name, ps, stats)
        self.zuletzt[name] = container
        self.stand[name] = self.jetzt()
        return container

    async def finden(self, host: str, gesucht: str) -> Container:
        """Container nach Name; auch ein eindeutiger Teil des Namens reicht („nextcloud“)."""
        name = container_name_pruefen(gesucht)
        container = await self.liste(host, mit_werten=False)
        genau = [c for c in container if c.name == name]
        if genau:
            return genau[0]
        passend = [c for c in container if name.casefold() in c.name.casefold()]
        if len(passend) == 1:
            return passend[0]
        host_name = self.ssh.host(host).name
        if not passend:
            namen = ", ".join(c.name for c in container) or "keine"
            raise WerkzeugFehler(
                f"Auf {host_name} gibt es keinen Container „{gesucht}“. Vorhanden: {namen}."
            )
        raise WerkzeugFehler(
            f"„{gesucht}“ passt auf {host_name} zu mehreren Containern: "
            + ", ".join(c.name for c in passend)
            + ". Frag Louis, welcher gemeint ist."
        )

    async def anzeigen(self, host: str | None = None) -> str:
        hosts = [self.ssh.host(host).name] if host else self.ssh.namen
        ergebnisse = await asyncio.gather(*(self.liste(h) for h in hosts), return_exceptions=True)
        zeilen = []
        for name, ergebnis in zip(hosts, ergebnisse, strict=True):
            if isinstance(ergebnis, SshFehler):
                zeilen.append(f"{name}: nicht abrufbar – {ergebnis}")
                continue
            if isinstance(ergebnis, BaseException):
                raise ergebnis
            laufend = sum(1 for c in ergebnis if c.zustand == "running")
            zeilen.append(
                f"Container auf {name} ({laufend} von {len(ergebnis)} laufen, Stand "
                f"{self.stand[name]:%H:%M} Uhr):"
            )
            zeilen += [c.zeile() for c in ergebnis] or ["- (keine Container)"]
        return "\n".join(zeilen)

    async def logs(
        self,
        host: str,
        container: str,
        zeilen: int | str | None = None,
        teil: int | str | None = None,
        nur_probleme: bool = False,
    ) -> str:
        ziel = await self.finden(host, container)
        text = await self.ssh.befehl(
            ziel.host, "docker-logs", ziel.name, str(zeilen_lesen(zeilen)), timeout=40
        )
        log_zeilen = text.splitlines()
        if nur_probleme:
            log_zeilen = [
                f"{z}" + (f"  ({n}×)" if n > 1 else "")
                for z, n in relevante_zeilen(log_zeilen, 200)
            ]
        kopf = f"Logs von {ziel.name} auf {ziel.host} ({ziel.status})"
        if nur_probleme:
            kopf += ", nur auffällige Zeilen"
        return teil_text(log_zeilen, teil_lesen(teil), kopf, "container_logs")

    # -------------------------------------------------------------- Neustart

    async def neustart_vorschau(self, host: str, container: str) -> str:
        ziel = await self.finden(host, container)
        return (
            f"Der Container „{ziel.name}“ auf {ziel.host} wird neu gestartet (docker restart). "
            f"Aktueller Status: {ziel.status}. Der Dienst ist dabei kurz nicht erreichbar."
        )

    async def neustarten(self, host: str, container: str) -> str:
        ziel = await self.finden(host, container)
        await self.ssh.befehl(ziel.host, "docker-restart", ziel.name, timeout=120)
        nachher = await self.finden(ziel.host, ziel.name)
        text = (
            f"Container „{ziel.name}“ auf {ziel.host} neu gestartet. "
            f"Status jetzt: {nachher.status}."
        )
        if problem := nachher.problem():
            text += f" ACHTUNG: Er {problem} – schau dir die Logs an (container_logs)."
        return text

    # -------------------------------------------------------------- Hänger erkennen

    def _ssh_problem(self, host: str, fehler: SshFehler, jetzt: datetime) -> Alarm | None:
        seit = self._ssh_fehler_seit.setdefault(host, jetzt)
        if self.alarme is None or jetzt - seit < HOST_STILL:
            return None
        alarm = self.alarme.melden(
            AlarmStufe.INFO,
            f"Ich komme per SSH nicht auf {host}",
            f"Seit {seit:%H:%M} Uhr: {fehler}. Container und Logs dort kann ich gerade nicht "
            "prüfen.",
            schluessel=f"ssh:{host}",
            quelle="ssh",
            ruhefenster=timedelta(hours=12),
        )
        if alarm is not None:
            self._ssh_gemeldet.add(host)
        return alarm

    async def pruefen(self) -> list[Alarm]:
        """Regelmäßiger Auftrag: alle Hosts lesen und hängende Container melden."""
        jetzt = self.jetzt()
        gemeldet: list[Alarm] = []
        for host in self.ssh.namen:
            erster_blick = host not in self.stand
            try:
                container = await self.liste(host, mit_werten=False)
            except SshFehler as fehler:
                if "nicht installiert" in str(fehler):
                    continue  # Host ohne Docker
                if alarm := self._ssh_problem(host, fehler, jetzt):
                    gemeldet.append(alarm)
                continue
            self._ssh_fehler_seit.pop(host, None)
            if host in self._ssh_gemeldet and self.alarme is not None:
                self._ssh_gemeldet.discard(host)
                if alarm := self.alarme.melden(
                    AlarmStufe.INFO,
                    f"{host} ist per SSH wieder erreichbar",
                    schluessel=f"ssh:{host}:wieder",
                    quelle="ssh",
                ):
                    gemeldet.append(alarm)
            gemeldet += self._auswerten(host, container, erster_blick)
        return gemeldet

    def _auswerten(self, host: str, container: list[Container], erster_blick: bool) -> list[Alarm]:
        gemeldet: list[Alarm] = []
        for c in container:
            schluessel = (host, c.name)
            vorher = self._vorher.get(schluessel)
            self._vorher[schluessel] = c
            if self.alarme is None or self.ignoriert(c):
                continue
            problem = c.problem()
            # Ein beendeter Container ist nur neu abgestürzt, wenn er vorher lief.
            if (
                problem
                and c.zustand == "exited"
                and (erster_blick or vorher is None or vorher.zustand == "exited")
            ):
                continue
            if problem:
                alarm = self.alarme.melden(
                    AlarmStufe.WICHTIG,
                    f"Container {c.name} auf {host} {problem}",
                    f"Status: {c.status} (Image {c.image}). Frag „Zeig die Logs von {c.name}“ "
                    "oder „Warum geht … nicht?“ – neu starten mache ich nur nach deinem Ja.",
                    schluessel=f"container:{host}:{c.name}",
                    quelle="docker",
                )
                if alarm is not None:
                    self._gemeldet.add(schluessel)
                    gemeldet.append(alarm)
            elif schluessel in self._gemeldet:
                self._gemeldet.discard(schluessel)
                alarm = self.alarme.melden(
                    AlarmStufe.INFO,
                    f"Container {c.name} auf {host} läuft wieder normal",
                    f"Status: {c.status}.",
                    schluessel=f"container:{host}:{c.name}:ok",
                    quelle="docker",
                )
                if alarm is not None:
                    gemeldet.append(alarm)
        return gemeldet


def container_werkzeuge(dienst: ContainerDienst) -> list[Werkzeug]:
    host_schema = {"type": "string", "description": "Host, z. B. wb01"}
    return [
        Werkzeug(
            "container_anzeigen",
            "Zeigt die Docker-Container eines Hosts (oder aller Hosts) mit Status, CPU und RAM "
            "und markiert hängende oder abgestürzte Container. Für „Welche Container laufen auf "
            "wb01?“.",
            {"type": "object", "properties": {"host": host_schema}},
            dienst.anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "container_logs",
            "Zeigt die letzten Log-Zeilen eines Containers, auf Wunsch nur die auffälligen. "
            "Lange Logs kommen in Teilen (teil=1 sind die neuesten).",
            {
                "type": "object",
                "properties": {
                    "host": host_schema,
                    "container": {"type": "string", "description": "Name, z. B. nextcloud"},
                    "zeilen": {"type": "integer", "description": "Standard 200, höchstens 5000"},
                    "teil": {"type": "integer", "description": "1 = neueste Zeilen"},
                    "nur_probleme": {"type": "boolean", "description": "nur auffällige Zeilen"},
                },
                "required": ["host", "container"],
            },
            dienst.logs,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "container_neustarten",
            "Startet einen Docker-Container neu (docker restart) – nur nach Louis' "
            "ausdrücklichem Ja.",
            {
                "type": "object",
                "properties": {
                    "host": host_schema,
                    "container": {"type": "string", "description": "Name des Containers"},
                },
                "required": ["host", "container"],
            },
            dienst.neustarten,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=dienst.neustart_vorschau,
        ),
    ]
