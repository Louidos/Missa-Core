"""Subdomains in Caddy vorbereiten – aktiv erst nach Louis' Freigabe.

Missa schreibt nur eigene Dateien ``missa-<name>.caddy`` in einen eigenen Ordner auf rp01
(``/etc/caddy/missa``), den das bestehende Caddyfile einmal importiert. Am bestehenden
Caddyfile und an seiner API-Key-Prüfung ändert sie nichts.

1. ``subdomain_vorschlagen`` zeigt den fertigen Eintrag und prüft, ob es einen DNS-Eintrag
   gibt (lesen).
2. ``subdomain_aktivieren`` (zerstoererisch, nur nach Ja): Datei schreiben →
   ``caddy validate`` → bei Erfolg ``systemctl reload caddy``; bei einem Fehler wird die
   Datei sofort wieder entfernt, Caddy läuft unverändert weiter, und Missa erklärt den Fehler.
3. ``subdomain_entfernen`` (zerstoererisch): Datei löschen, prüfen, neu laden.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime

from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.monitoring import QuellenFehler
from missa.integrationen.ssh import SshFehler, SshVerbindung

log = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
ZIEL = re.compile(r"^(?:(https?)://)?([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\]):(\d{1,5})$")
#: Fragt DNS ab: (Name, Typ) → Werte.
DnsAbfrage = Callable[[str, str], Awaitable[list[str]]]


def name_pruefen(name: object) -> str:
    text = str(name or "").strip().casefold()
    if not NAME.match(text):
        raise WerkzeugFehler(
            f"„{name}“ geht nicht als Subdomain: nur a–z, 0–9 und -, z. B. „spiel“ oder „mc“."
        )
    return text


def ziel_pruefen(ziel: object) -> str:
    """``10.0.70.120:8080`` oder ``http://mc01:8080`` – Host und Port sind Pflicht."""
    text = str(ziel or "").strip()
    treffer = ZIEL.match(text)
    if not treffer or not 0 < int(treffer.group(3)) < 65536:
        raise WerkzeugFehler(
            f"„{ziel}“ ist kein Ziel, das ich verstehe – bitte Host und Port, "
            "z. B. 10.0.70.120:8080."
        )
    host = treffer.group(2).strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,250}[A-Za-z0-9])?", host):
            raise WerkzeugFehler(f"„{host}“ ist kein gültiger Host.") from None
    return text


class CaddyVerwaltung:
    """Missas eigene Caddy-Einträge auf rp01 (über SSH, nur fest erlaubte Befehle)."""

    def __init__(
        self,
        ssh: SshVerbindung,
        host: str,
        domain: str,
        dns: DnsAbfrage | None = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.ssh = ssh
        self.host = host
        self.domain = domain.strip().casefold().strip(".")
        self.dns = dns
        self._uhr = uhr or (lambda: datetime.now().astimezone())

    @staticmethod
    def datei(name: str) -> str:
        return f"missa-{name}.caddy"

    def fqdn(self, name: str) -> str:
        return f"{name}.{self.domain}"

    def eintrag(self, name: str, ziel: str) -> str:
        return (
            f"# Von Missa angelegt am {self._uhr():%d.%m.%Y} – {self.datei(name)}\n"
            f"{self.fqdn(name)} {{\n"
            f"\treverse_proxy {ziel}\n"
            "}"
        )

    async def eintraege(self) -> dict[str, str]:
        """Missas aktive Einträge: Dateiname → Inhalt."""
        text = await self.ssh.befehl(self.host, "caddy-liste")
        eintraege: dict[str, str] = {}
        aktuell = ""
        for zeile in text.splitlines():
            if zeile.startswith("### "):
                aktuell = zeile[4:].strip()
                eintraege[aktuell] = ""
            elif aktuell:
                eintraege[aktuell] += zeile + "\n"
        return eintraege

    async def _dns_hinweis(self, name: str) -> str:
        if self.dns is None:
            return ""
        fqdn = self.fqdn(name)
        try:
            gefunden = [w for typ in ("A", "AAAA", "CNAME") for w in await self.dns(fqdn, typ)]
        except QuellenFehler as fehler:
            return f"DNS konnte ich nicht prüfen ({fehler})."
        if gefunden:
            return f"DNS: {fqdn} zeigt schon auf {', '.join(sorted(set(gefunden)))}."
        return (
            f"DNS-EINTRAG NÖTIG: {fqdn} gibt es im DNS noch nicht. Lege beim DNS-Anbieter einen "
            f"CNAME {fqdn} → {self.domain} (oder einen A-Eintrag auf deine öffentliche IP) an, "
            "sonst bekommt Caddy kein Zertifikat."
        )

    async def vorschlagen(self, name: str, ziel: str) -> str:
        name, ziel = name_pruefen(name), ziel_pruefen(ziel)
        vorhanden = await self.eintraege()
        zeilen = [
            f"Vorschlag für {self.fqdn(name)} → {ziel} (Datei {self.datei(name)} auf {self.host}):"
        ]
        zeilen += ["```", self.eintrag(name, ziel), "```"]
        if self.datei(name) in vorhanden:
            zeilen.append(f"ACHTUNG: {self.datei(name)} gibt es schon – er würde ersetzt.")
        if hinweis := await self._dns_hinweis(name):
            zeilen.append(hinweis)
        zeilen.append(
            "NOCH NICHT AKTIV. Zeig Louis den Eintrag. Erst nach seinem Ja rufe "
            f"subdomain_aktivieren mit name={name} und ziel={ziel} auf."
        )
        return "\n".join(zeilen)

    async def aktivieren_vorschau(self, name: str, ziel: str) -> str:
        name, ziel = name_pruefen(name), ziel_pruefen(ziel)
        return (
            f"Auf {self.host} wird {self.datei(name)} geschrieben und Caddy neu geladen – danach "
            f"leitet {self.fqdn(name)} auf {ziel} weiter:\n{self.eintrag(name, ziel)}\n"
            "Ist der Eintrag ungültig, entferne ich ihn sofort wieder; Caddy läuft dann "
            "unverändert weiter."
        )

    async def aktivieren(self, name: str, ziel: str) -> str:
        """Schreibt, prüft und lädt neu – oder nimmt den Eintrag sofort wieder heraus."""
        name, ziel = name_pruefen(name), ziel_pruefen(ziel)
        datei = self.datei(name)
        vorher = (await self.eintraege()).get(datei)
        await self.ssh.befehl(self.host, "caddy-schreiben", datei, eingabe=self.eintrag(name, ziel))
        try:
            await self.ssh.befehl(self.host, "caddy-pruefen", timeout=60)
        except SshFehler as fehler:
            await self._zuruecknehmen(datei, vorher)
            raise WerkzeugFehler(
                f"Caddy hat den Eintrag für {self.fqdn(name)} abgelehnt: {fehler}. Ich habe "
                f"{datei} {'zurückgesetzt' if vorher else 'wieder entfernt'} – Caddy läuft "
                "unverändert mit der alten Konfiguration weiter. Erklär Louis den Fehler "
                "verständlich."
            ) from fehler
        try:
            await self.ssh.befehl(self.host, "caddy-neu-laden", timeout=60)
        except SshFehler as fehler:
            await self._zuruecknehmen(datei, vorher)
            raise WerkzeugFehler(
                f"Caddy ließ sich nicht neu laden: {fehler}. Ich habe {datei} "
                f"{'zurückgesetzt' if vorher else 'wieder entfernt'}; Caddy läuft mit der "
                "bisherigen Konfiguration weiter."
            ) from fehler
        text = f"{self.fqdn(name)} ist aktiv und leitet auf {ziel} weiter."
        if hinweis := await self._dns_hinweis(name):
            text += f" {hinweis}"
        return text

    async def _zuruecknehmen(self, datei: str, vorher: str | None) -> None:
        """Stellt den alten Stand der Datei wieder her (oder entfernt sie, wenn sie neu war)."""
        if vorher:
            await self.ssh.befehl(self.host, "caddy-schreiben", datei, eingabe=vorher.rstrip("\n"))
        else:
            await self.ssh.befehl(self.host, "caddy-loeschen", datei)

    async def entfernen_vorschau(self, name: str) -> str:
        name = name_pruefen(name)
        if self.datei(name) not in await self.eintraege():
            raise WerkzeugFehler(f"Einen Missa-Eintrag für {self.fqdn(name)} gibt es nicht.")
        return (
            f"{self.datei(name)} wird auf {self.host} gelöscht und Caddy neu geladen – "
            f"{self.fqdn(name)} ist dann nicht mehr erreichbar."
        )

    async def entfernen(self, name: str) -> str:
        name = name_pruefen(name)
        await self.ssh.befehl(self.host, "caddy-loeschen", self.datei(name))
        await self.ssh.befehl(self.host, "caddy-pruefen", timeout=60)
        await self.ssh.befehl(self.host, "caddy-neu-laden", timeout=60)
        return f"{self.fqdn(name)} ist entfernt."

    async def anzeigen(self) -> str:
        eintraege = await self.eintraege()
        if not eintraege:
            return f"Ich habe auf {self.host} noch keine eigenen Subdomains angelegt."
        zeilen = [f"Missas Subdomains auf {self.host}:"]
        for datei, inhalt in sorted(eintraege.items()):
            ziel = re.search(r"reverse_proxy\s+(\S+)", inhalt)
            adresse = re.search(r"^([^\s#{]+)\s*\{", inhalt, re.MULTILINE)
            zeilen.append(
                f"- {adresse.group(1) if adresse else datei} → {ziel.group(1) if ziel else '?'}"
            )
        return "\n".join(zeilen)


def caddy_werkzeuge(caddy: CaddyVerwaltung) -> list[Werkzeug]:
    angaben = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Subdomain, z. B. spiel"},
            "ziel": {"type": "string", "description": "Host:Port dahinter, z. B. 10.0.70.120:8080"},
        },
        "required": ["name", "ziel"],
    }
    return [
        Werkzeug(
            "subdomain_vorschlagen",
            "Schlägt eine neue Subdomain in Caddy vor und zeigt den fertigen Eintrag – noch "
            "nicht aktiv. Prüft auch, ob es einen DNS-Eintrag gibt.",
            angaben,
            caddy.vorschlagen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "subdomain_aktivieren",
            "Aktiviert die vorgeschlagene Subdomain (Datei schreiben, caddy validate, neu laden) – "
            "nur nach Louis' Ja. Ungültiges wird sofort wieder entfernt.",
            angaben,
            caddy.aktivieren,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=caddy.aktivieren_vorschau,
        ),
        Werkzeug(
            "subdomain_entfernen",
            "Entfernt eine von Missa angelegte Subdomain wieder – nur nach Louis' Ja.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            caddy.entfernen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=caddy.entfernen_vorschau,
        ),
        Werkzeug(
            "subdomains_anzeigen",
            "Zeigt die Subdomains, die Missa in Caddy angelegt hat.",
            {"type": "object", "properties": {}},
            caddy.anzeigen,
            stufe=Stufe.LESEN,
        ),
    ]
