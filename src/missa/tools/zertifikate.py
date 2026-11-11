"""Zertifikate und Domains: rechtzeitig vor dem Ablauf warnen, DNS gegen die Erwartung prüfen.

Jeden Tag (``ZERTIFIKATE_PRUEFZEIT``) prüft Missa

- das **HTTPS-Zertifikat** jeder Domain aus ``ZERTIFIKAT_DOMAINS`` (Standard: die Domains
  der Erreichbarkeit) – volle Prüfung, Ablaufdatum aus dem Zertifikat,
- das **Ablaufdatum der Domains** (``DOMAIN_NAMEN``) per RDAP und
- die **DNS-Einträge** aus ``DNS_ERWARTET`` gegen die tatsächlichen Werte.

Gewarnt wird je Schwelle aus ``ZERTIFIKAT_WARNUNG_TAGE`` (Standard 21 und 7 Tage vorher)
genau einmal als ``wichtig``; ist etwas schon abgelaufen, als ``notfall``. Ein erneuertes
Zertifikat (neues Ablaufdatum) setzt die Warnungen zurück. Eine DNS-Abweichung meldet Missa
als ``wichtig``, die Rückkehr zum erwarteten Wert als ``info``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug
from missa.integrationen.domains import DnsAbfrage, RdapClient, dns_abfragen
from missa.integrationen.erreichbarkeit import ZertifikatLeser, zertifikat_lesen
from missa.integrationen.monitoring import QuellenFehler

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dns_zustand (
    name TEXT NOT NULL,
    typ TEXT NOT NULL,
    ok INTEGER NOT NULL,
    werte TEXT NOT NULL,
    geprueft TEXT NOT NULL,
    PRIMARY KEY (name, typ)
);
"""


@dataclass(frozen=True)
class Ablauf:
    """Wann etwas abläuft (oder warum es nicht prüfbar war)."""

    art: str  # "Zertifikat" oder "Domain"
    name: str
    bis: datetime | None
    problem: str = ""

    def tage(self, jetzt: datetime) -> int | None:
        if self.bis is None:
            return None
        return (self.bis - jetzt).days

    def zeile(self, jetzt: datetime) -> str:
        if self.problem:
            return f"- {self.art} {self.name}: nicht prüfbar – {self.problem}"
        if self.bis is None:
            return (
                f"- {self.art} {self.name}: Ablaufdatum wird von der Registry nicht veröffentlicht"
            )
        tage = self.tage(jetzt)
        warnung = " ⚠️" if tage is not None and tage <= 21 else ""
        return (
            f"- {self.art} {self.name}: gültig bis {self.bis:%d.%m.%Y} (noch {tage} Tage){warnung}"
        )


class ZertifikatsWaechter:
    """Prüft Zertifikate, Domain-Ablauf und DNS und warnt rechtzeitig."""

    def __init__(
        self,
        alarme: Alarme,
        datei: Path,
        *,
        zertifikat_domains: Iterable[str] = (),
        domain_namen: Iterable[str] = (),
        dns_erwartet: dict[tuple[str, str], set[str]] | None = None,
        warn_tage: Iterable[int] = (21, 7),
        rdap: RdapClient | None = None,
        zertifikat: ZertifikatLeser = zertifikat_lesen,
        dns: DnsAbfrage = dns_abfragen,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.alarme = alarme
        self.datei = datei
        self.zertifikat_domains = tuple(dict.fromkeys(d.casefold() for d in zertifikat_domains))
        self.domain_namen = tuple(dict.fromkeys(d.casefold() for d in domain_namen))
        self.dns_erwartet = dns_erwartet or {}
        self.warn_tage = tuple(sorted({max(1, int(t)) for t in warn_tage}, reverse=True)) or (21, 7)
        self.rdap = rdap
        self._zertifikat = zertifikat
        self._dns = dns
        self._uhr = uhr or alarme.jetzt
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr()

    # -------------------------------------------------------------- Lesen

    async def _zertifikat_pruefen(self, domain: str) -> Ablauf:
        try:
            bis = await self._zertifikat(domain, 15.0)
        except ssl.SSLCertVerificationError as fehler:
            grund = getattr(fehler, "verify_message", "") or str(fehler)
            if "expired" in grund:
                return Ablauf("Zertifikat", domain, None, "Zertifikat ist abgelaufen")
            return Ablauf("Zertifikat", domain, None, f"Zertifikat ungültig: {grund}")
        except (OSError, TimeoutError, ssl.SSLError) as fehler:
            return Ablauf("Zertifikat", domain, None, f"keine HTTPS-Verbindung: {fehler}")
        return Ablauf("Zertifikat", domain, bis)

    async def _domain_pruefen(self, domain: str) -> Ablauf:
        if self.rdap is None:
            return Ablauf("Domain", domain, None, "RDAP ist ausgeschaltet")
        try:
            return Ablauf("Domain", domain, await self.rdap.ablauf(domain))
        except QuellenFehler as fehler:
            return Ablauf("Domain", domain, None, str(fehler))

    async def ablaeufe(self) -> list[Ablauf]:
        aufgaben = [self._zertifikat_pruefen(d) for d in self.zertifikat_domains]
        aufgaben += [self._domain_pruefen(d) for d in self.domain_namen]
        return list(await asyncio.gather(*aufgaben))

    async def dns_pruefen(self) -> list[tuple[str, str, set[str], set[str] | None, str]]:
        """(Name, Typ, erwartet, tatsächlich oder None bei Fehler, Fehlertext)."""
        ergebnisse = []
        for (name, typ), erwartet in sorted(self.dns_erwartet.items()):
            try:
                werte = set(await self._dns(name, typ))
                ergebnisse.append((name, typ, erwartet, werte, ""))
            except QuellenFehler as fehler:
                ergebnisse.append((name, typ, erwartet, None, str(fehler)))
        return ergebnisse

    # -------------------------------------------------------------- Melden

    def _einmal(self, stufe: AlarmStufe, titel: str, text: str, schluessel: str) -> Alarm | None:
        """Meldet genau einmal pro Schlüssel (nicht erst nach dem Ruhefenster wieder)."""
        if self.alarme.zuletzt(schluessel) is not None:
            return None
        return self.alarme.melden(stufe, titel, text, schluessel=schluessel, quelle="zertifikate")

    def _ablauf_melden(self, ablauf: Ablauf, jetzt: datetime) -> Alarm | None:
        tage = ablauf.tage(jetzt)
        if ablauf.bis is None or tage is None:
            return None
        stichtag = f"{ablauf.bis:%Y-%m-%d}"
        if ablauf.bis <= jetzt:
            return self._einmal(
                AlarmStufe.NOTFALL,
                f"{ablauf.art} {ablauf.name} ist abgelaufen",
                f"Abgelaufen am {ablauf.bis:%d.%m.%Y}.",
                f"ablauf:{ablauf.art}:{ablauf.name}:abgelaufen:{stichtag}",
            )
        schwellen = [s for s in self.warn_tage if tage <= s]
        if not schwellen:
            return None
        schwelle = min(schwellen)
        was = (
            "die Erneuerung prüfen (bei Caddy läuft sie normalerweise automatisch – dann "
            "stimmt dort etwas nicht)"
            if ablauf.art == "Zertifikat"
            else "beim Anbieter verlängern"
        )
        return self._einmal(
            AlarmStufe.WICHTIG,
            f"{ablauf.art} {ablauf.name} läuft in {tage} Tagen ab",
            f"Gültig bis {ablauf.bis:%d.%m.%Y}. Bitte {was}.",
            f"ablauf:{ablauf.art}:{ablauf.name}:{schwelle}:{stichtag}",
        )

    def _dns_zustand(self, name: str, typ: str) -> dict | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM dns_zustand WHERE name = ? AND typ = ?", (name, typ)
            ).fetchone()
        return dict(zeile) if zeile else None

    def _dns_melden(self, name: str, typ: str, erwartet: set[str], werte: set[str]) -> Alarm | None:
        ok = werte == erwartet
        vorher = self._dns_zustand(name, typ)
        jetzt = self.jetzt().astimezone(UTC).isoformat(timespec="seconds")
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO dns_zustand (name, typ, ok, werte, geprueft) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (name, typ) DO UPDATE SET ok = excluded.ok, werte = excluded.werte, "
                "geprueft = excluded.geprueft",
                (name, typ, int(ok), json.dumps(sorted(werte)), jetzt),
            )
        if not ok and (
            vorher is None or vorher["ok"] or json.loads(vorher["werte"]) != sorted(werte)
        ):
            return self.alarme.melden(
                AlarmStufe.WICHTIG,
                f"DNS {name} {typ} weicht ab",
                f"Erwartet: {', '.join(sorted(erwartet))}. Gefunden: "
                + (", ".join(sorted(werte)) or "kein Eintrag")
                + ". Das kann eine Fehlkonfiguration sein – oder jemand hat den Eintrag geändert.",
                schluessel=f"dns:{name}:{typ}:{'|'.join(sorted(werte))}",
                quelle="dns",
            )
        if ok and vorher is not None and not vorher["ok"]:
            return self.alarme.melden(
                AlarmStufe.INFO,
                f"DNS {name} {typ} stimmt wieder",
                f"Wieder wie erwartet: {', '.join(sorted(erwartet))}.",
                schluessel=f"dns:{name}:{typ}:ok",
                quelle="dns",
            )
        return None

    async def pruefen(self) -> list[Alarm]:
        """Täglicher Auftrag: alles prüfen und rechtzeitig warnen."""
        jetzt = self.jetzt()
        gemeldet: list[Alarm] = []
        for ablauf in await self.ablaeufe():
            if ablauf.problem:
                log.info("%s %s: %s", ablauf.art, ablauf.name, ablauf.problem)
            elif alarm := self._ablauf_melden(ablauf, jetzt):
                gemeldet.append(alarm)
        for name, typ, erwartet, werte, fehler in await self.dns_pruefen():
            if werte is None:
                log.info("DNS %s %s: %s", name, typ, fehler)
            elif alarm := self._dns_melden(name, typ, erwartet, werte):
                gemeldet.append(alarm)
        return gemeldet

    async def bericht(self) -> str:
        """Werkzeug: aktueller Stand von Zertifikaten, Domains und DNS."""
        jetzt = self.jetzt()
        zeilen = ["Zertifikate und Domains:"]
        zeilen += [a.zeile(jetzt) for a in await self.ablaeufe()] or ["- (keine eingerichtet)"]
        dns = await self.dns_pruefen()
        if dns:
            zeilen.append("DNS:")
            for name, typ, erwartet, werte, fehler in dns:
                if werte is None:
                    zeilen.append(f"- {name} {typ}: nicht prüfbar – {fehler}")
                elif werte == erwartet:
                    zeilen.append(f"- {name} {typ}: wie erwartet ({', '.join(sorted(werte))})")
                else:
                    zeilen.append(
                        f"- {name} {typ}: ABWEICHUNG – erwartet {', '.join(sorted(erwartet))}, "
                        f"gefunden {', '.join(sorted(werte)) or 'kein Eintrag'}"
                    )
        zeilen.append(f"(Gewarnt wird {' und '.join(str(t) for t in self.warn_tage)} Tage vorher.)")
        return "\n".join(zeilen)


def zertifikat_werkzeuge(waechter: ZertifikatsWaechter) -> list[Werkzeug]:
    return [
        Werkzeug(
            "zertifikate_pruefen",
            "Zeigt, wann die HTTPS-Zertifikate und Domains ablaufen (RDAP) und ob die DNS-Einträge "
            "den erwarteten Werten entsprechen.",
            {"type": "object", "properties": {}},
            waechter.bericht,
            stufe=Stufe.LESEN,
        )
    ]
