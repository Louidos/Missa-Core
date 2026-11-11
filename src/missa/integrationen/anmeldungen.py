"""Anmeldungen aus Logs lesen: SSH (journal von sshd) und Nextcloud (nextcloud.log).

Ergebnis ist eine Liste von :class:`LoginEreignis` – erfolgreich oder fehlgeschlagen,
mit Zeit, Benutzer und IP. Gelesen wird über SSH (``missa-befehl ssh-log`` bzw.
``nextcloud-log``/``journal``), hier steckt nur das Auswerten.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Eine Zeile aus ``journalctl -o short-iso``.
JOURNAL_ZEILE = re.compile(
    r"^(?P<zeit>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2}))\s+"
    r"(?P<host>\S+)\s+(?P<programm>[^\s:\[]+)(?:\[\d+\])?:\s?(?P<text>.*)$"
)
SSH_ERFOLG = re.compile(
    r"Accepted (?P<art>\S+) for (?P<benutzer>\S+) from (?P<ip>[0-9a-fA-F.:]+)"
    r"(?: port (?P<port>\d+))?"
)
SSH_FEHLER = re.compile(
    r"Failed (?P<art>\S+) for (?:invalid user )?(?P<benutzer>\S*) from (?P<ip>[0-9a-fA-F.:]+)"
    r"(?: port (?P<port>\d+))?"
)
SSH_UNGUELTIG = re.compile(
    r"Invalid user (?P<benutzer>\S*) from (?P<ip>[0-9a-fA-F.:]+)(?: port (?P<port>\d+))?"
)
SSH_ABGEBROCHEN = re.compile(
    r"Connection closed by (?:authenticating|invalid) user (?P<benutzer>\S*) "
    r"(?P<ip>[0-9a-fA-F.:]+) port (?P<port>\d+) \[preauth\]"
)
NC_FEHLER = re.compile(
    r"Login failed: ['\"]?(?P<benutzer>[^'\"()]*?)['\"]? \(Remote IP: ['\"]?(?P<ip>[0-9a-fA-F.:]+)"
)
NC_ERFOLG = re.compile(r"Login successful: ['\"]?(?P<benutzer>[^'\"]+)['\"]?")


@dataclass(frozen=True)
class LoginEreignis:
    quelle: str  # "ssh" oder "nextcloud"
    host: str
    zeit: datetime
    benutzer: str
    ip: str
    erfolg: bool
    art: str
    zeile: str

    @property
    def kennung(self) -> str:
        return f"{self.quelle}|{self.host}|{self.zeit.isoformat()}|{self.zeile[:400]}"


#: Eigenes Netz: private Bereiche (RFC 1918), CGNAT/Tailscale, ULA, Loopback, Link-Local.
EIGENE_NETZE = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def ist_privat(ip: str) -> bool:
    """``True`` für Adressen aus dem eigenen Netz (10.x, 192.168.x, fd00::/8, localhost …)."""
    try:
        adresse = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(adresse.version == netz.version and adresse in netz for netz in EIGENE_NETZE)


def ip_pruefen(text: Any) -> str:
    """Gültige IP-Adresse als Text, sonst ``ValueError``."""
    return str(ipaddress.ip_address(str(text).strip()))


def _zeit(text: str) -> datetime | None:
    roh = text.replace(",", ".")
    if re.search(r"[+-]\d{4}$", roh):
        roh = roh[:-2] + ":" + roh[-2:]
    try:
        zeit = datetime.fromisoformat(roh.replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeit if zeit.tzinfo is not None else None


def ssh_lesen(host: str, text: str) -> list[LoginEreignis]:
    """SSH-Anmeldungen aus dem journal von sshd.

    Jeder fehlgeschlagene Versuch zählt einmal: „Failed …“-Zeilen zählen immer, „Invalid
    user …“ und „Connection closed … [preauth]“ nur, wenn es für dieselbe Verbindung
    (IP und Port) keine „Failed“-Zeile gibt (so zählen auch Server ohne Passwort-Login).
    """
    erfolge: list[LoginEreignis] = []
    fehler: list[LoginEreignis] = []
    ersatz: list[tuple[tuple[str, str], LoginEreignis]] = []
    mit_failed: set[tuple[str, str]] = set()
    for zeile in text.splitlines():
        treffer = JOURNAL_ZEILE.match(zeile.strip())
        if not treffer or not treffer.group("programm").startswith("sshd"):
            continue
        zeit = _zeit(treffer.group("zeit"))
        if zeit is None:
            continue
        inhalt = treffer.group("text")
        if m := SSH_ERFOLG.search(inhalt):
            erfolge.append(
                LoginEreignis("ssh", host, zeit, m["benutzer"], m["ip"], True, m["art"], zeile)
            )
        elif m := SSH_FEHLER.search(inhalt):
            mit_failed.add((m["ip"], m["port"] or ""))
            fehler.append(
                LoginEreignis("ssh", host, zeit, m["benutzer"], m["ip"], False, m["art"], zeile)
            )
        elif (m := SSH_UNGUELTIG.search(inhalt)) or (m := SSH_ABGEBROCHEN.search(inhalt)):
            ereignis = LoginEreignis(
                "ssh", host, zeit, m["benutzer"], m["ip"], False, "abgewiesen", zeile
            )
            ersatz.append(((m["ip"], m["port"] or ""), ereignis))
    gezaehlt: set[tuple[str, str]] = set()
    for verbindung, ereignis in ersatz:
        if verbindung in mit_failed or verbindung in gezaehlt:
            continue
        gezaehlt.add(verbindung)
        fehler.append(ereignis)
    return sorted(erfolge + fehler, key=lambda e: e.zeit)


def nextcloud_lesen(host: str, text: str) -> list[LoginEreignis]:
    """Nextcloud-Anmeldungen aus nextcloud.log (JSON-Zeilen, auch aus dem journal).

    Fehlversuche stehen dort immer („Login failed: …“), erfolgreiche Anmeldungen nur mit
    der App „admin_audit“ („Login successful: …“).
    """
    ereignisse = []
    for zeile in text.splitlines():
        anfang = zeile.find("{")
        if anfang < 0:
            continue
        try:
            daten = json.loads(zeile[anfang:])
        except ValueError:
            continue
        if not isinstance(daten, dict):
            continue
        nachricht = daten.get("message")
        if not isinstance(nachricht, str):
            continue
        zeit = _zeit(str(daten.get("time", "")))
        if zeit is None:
            continue
        ip = str(daten.get("remoteAddr") or "")
        if m := NC_FEHLER.search(nachricht):
            ereignisse.append(
                LoginEreignis(
                    "nextcloud",
                    host,
                    zeit,
                    m["benutzer"].strip(),
                    m["ip"] or ip,
                    False,
                    "passwort",
                    zeile,
                )
            )
        elif m := NC_ERFOLG.search(nachricht):
            ereignisse.append(
                LoginEreignis(
                    "nextcloud", host, zeit, m["benutzer"].strip(), ip, True, "login", zeile
                )
            )
    return sorted(ereignisse, key=lambda e: e.zeit)


NC_STUFEN = {0: "debug", 1: "info", 2: "warnung", 3: "fehler", 4: "fatal"}


def nextcloud_fehler(text: str, ab_stufe: int = 3) -> list[str]:
    """Fehler aus nextcloud.log (Stufe 3 = error, 4 = fatal) als lesbare Zeilen."""
    zeilen = []
    for zeile in text.splitlines():
        anfang = zeile.find("{")
        if anfang < 0:
            continue
        try:
            daten = json.loads(zeile[anfang:])
        except ValueError:
            continue
        if not isinstance(daten, dict):
            continue
        try:
            stufe = int(daten.get("level", 0))
        except (TypeError, ValueError):
            continue
        if stufe < ab_stufe:
            continue
        nachricht = daten.get("message")
        if isinstance(nachricht, dict):
            nachricht = nachricht.get("Message") or nachricht.get("Exception") or str(nachricht)
        ausnahme = daten.get("exception")
        if isinstance(ausnahme, dict) and ausnahme.get("Message"):
            nachricht = f"{nachricht} – {ausnahme.get('Exception', '')}: {ausnahme['Message']}"
        zeilen.append(
            f"{daten.get('time', '?')} [{NC_STUFEN.get(stufe, stufe)}] "
            f"{daten.get('app', '?')}: {nachricht}"
        )
    return zeilen


@dataclass(frozen=True)
class NextcloudLog:
    """Wo das Nextcloud-Log liegt: eigene Datei (``datei``) oder im journal (``journal``)."""

    host: str
    modus: str = "datei"
    ident: str = "Nextcloud"

    @classmethod
    def aus_text(cls, host: str, quelle: str) -> NextcloudLog | None:
        """``datei``, ``journal`` oder ``journal:<Kennung>`` – leer = kein Nextcloud-Log."""
        if not host or not quelle:
            return None
        modus, _, ident = quelle.strip().partition(":")
        modus = modus.casefold()
        if modus not in ("datei", "journal"):
            raise ValueError("NEXTCLOUD_LOG_QUELLE muss datei, journal oder journal:<Kennung> sein")
        return cls(host.casefold(), modus, ident.strip() or "Nextcloud")

    async def lesen(self, ssh: Any, zeilen: int = 2000, seit: datetime | None = None) -> str:
        if self.modus == "datei":
            return await ssh.befehl(self.host, "nextcloud-log", str(zeilen), timeout=40)
        angaben = [f"ident={self.ident}", f"zeilen={zeilen}"]
        if seit is not None:
            angaben.append(f"seit=@{int(seit.timestamp())}")
        return await ssh.befehl(self.host, "journal", *angaben, timeout=40)
