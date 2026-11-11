"""Zertifikate und Domains: Warnungen 21 und 7 Tage vorher, RDAP, DNS gegen die Erwartung."""

from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.config import Einstellungen, KonfigurationsFehler
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.domains import (
    RdapClient,
    erwartet_lesen,
    registrierte_domain,
    wert_normal,
)
from missa.integrationen.monitoring import QuellenFehler
from missa.tools.zertifikate import ZertifikatsWaechter, zertifikat_werkzeuge
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

BOOTSTRAP = {
    "services": [
        [["ch", "li"], ["https://rdap.nic.ch/"]],
        [["com", "net"], ["http://rdap.verisign.test/", "https://rdap.verisign.test/"]],
    ]
}


class Netz:
    """Gefälschte Zertifikate und DNS-Antworten."""

    def __init__(self, jetzt: datetime) -> None:
        self.ablauf: dict[str, datetime] = {}
        self.ungueltig: set[str] = set()
        self.dns: dict[tuple[str, str], list[str]] = {}
        self.jetzt = jetzt

    async def zertifikat(self, domain: str, _timeout: float) -> datetime:
        if domain in self.ungueltig:
            fehler = ssl.SSLCertVerificationError(1, "certificate verify failed")
            fehler.verify_message = "Hostname mismatch"
            raise fehler
        return self.ablauf.get(domain, self.jetzt + timedelta(days=80))

    async def abfragen(self, name: str, typ: str) -> list[str]:
        if (name, typ) == ("kaputt.test", "A"):
            raise QuellenFehler("DNS-Abfrage kaputt.test A fehlgeschlagen: Timeout")
        return sorted(self.dns.get((name, typ), []))


def rdap_transport(ablauf: str | None) -> httpx.MockTransport:
    def antwort(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.iana.org":
            return httpx.Response(200, json=BOOTSTRAP)
        if request.url.path.endswith("/domain/gibtsnicht.ch"):
            return httpx.Response(404)
        events = [{"eventAction": "registration", "eventDate": "2015-01-01T00:00:00Z"}]
        if ablauf:
            events.append({"eventAction": "expiration", "eventDate": ablauf})
        return httpx.Response(200, json={"ldhName": "x", "events": events})

    return httpx.MockTransport(antwort)


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 11, 7, 10, tzinfo=ZONE))


@pytest.fixture
def planer(tmp_path: Path, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)


@pytest.fixture
def alarme(planer) -> Alarme:
    return Alarme(planer.datei, ZONE, planer, ruhe_von=None, ruhe_bis=None)


@pytest.fixture
def netz(uhr) -> Netz:
    return Netz(uhr.jetzt)


def waechter_bauen(alarme, planer, netz, uhr, **extra) -> ZertifikatsWaechter:
    return ZertifikatsWaechter(
        alarme,
        planer.datei,
        zertifikat=netz.zertifikat,
        dns=netz.abfragen,
        uhr=uhr,
        **extra,
    )


# ----------------------------------------------------------------------------- Zertifikate


def test_zertifikat_mit_ablauf_in_7_tagen_warnt(alarme, planer, netz, uhr):
    netz.ablauf["git.louis-degiorgi.test"] = uhr.jetzt + timedelta(days=7, hours=3)
    waechter = waechter_bauen(
        alarme, planer, netz, uhr, zertifikat_domains=["git.louis-degiorgi.test"]
    )
    alarme_jetzt = asyncio.run(waechter.pruefen())
    assert [(a.stufe, a.titel) for a in alarme_jetzt] == [
        (AlarmStufe.WICHTIG, "Zertifikat git.louis-degiorgi.test läuft in 7 Tagen ab")
    ]
    assert "Gültig bis 18.11.2026" in alarme_jetzt[0].text
    uhr.vor(days=1)
    assert asyncio.run(waechter.pruefen()) == []  # dieselbe Warnung nur einmal


def test_warnung_bei_21_und_bei_7_tagen_und_neues_zertifikat(alarme, planer, netz, uhr):
    netz.ablauf["cloud.x.test"] = uhr.jetzt + timedelta(days=30)
    waechter = waechter_bauen(alarme, planer, netz, uhr, zertifikat_domains=["cloud.x.test"])
    tage = []
    for _ in range(30):
        for alarm in asyncio.run(waechter.pruefen()):
            tage.append((alarm.titel, (netz.ablauf["cloud.x.test"] - uhr.jetzt).days))
        uhr.vor(days=1)
    assert [t for _, t in tage] == [21, 7]
    # Erneuert: neues Ablaufdatum, wieder alles ruhig – und später wieder gewarnt
    netz.ablauf["cloud.x.test"] = uhr.jetzt + timedelta(days=20)
    assert [a.titel for a in asyncio.run(waechter.pruefen())] == [
        "Zertifikat cloud.x.test läuft in 20 Tagen ab"
    ]


def test_abgelaufen_ist_notfall_und_ungueltig_wird_berichtet(alarme, planer, netz, uhr):
    netz.ablauf["alt.test"] = uhr.jetzt - timedelta(days=1)
    netz.ungueltig.add("falsch.test")
    waechter = waechter_bauen(
        alarme, planer, netz, uhr, zertifikat_domains=["alt.test", "falsch.test"]
    )
    assert [(a.stufe, a.titel) for a in asyncio.run(waechter.pruefen())] == [
        (AlarmStufe.NOTFALL, "Zertifikat alt.test ist abgelaufen")
    ]
    bericht = asyncio.run(waechter.bericht())
    assert (
        "Zertifikat falsch.test: nicht prüfbar – Zertifikat ungültig: Hostname mismatch" in bericht
    )


# ----------------------------------------------------------------------------- Domains (RDAP)


def test_rdap_ablauf_und_bootstrap():
    rdap = RdapClient(transport=rdap_transport("2027-03-01T00:00:00Z"))
    assert asyncio.run(rdap.ablauf("louis-degiorgi.ch")) == datetime(2027, 3, 1, tzinfo=UTC)
    assert asyncio.run(rdap.server("com")) == "https://rdap.verisign.test/"  # https bevorzugt
    with pytest.raises(QuellenFehler, match="kennt die Domain nicht"):
        asyncio.run(rdap.ablauf("gibtsnicht.ch"))
    with pytest.raises(QuellenFehler, match="keinen RDAP-Server"):
        asyncio.run(rdap.ablauf("beispiel.xyz"))


def test_domain_ohne_veroeffentlichtes_ablaufdatum(alarme, planer, netz, uhr):
    waechter = waechter_bauen(
        alarme,
        planer,
        netz,
        uhr,
        domain_namen=["louis-degiorgi.ch"],
        rdap=RdapClient(transport=rdap_transport(None)),
    )
    assert asyncio.run(waechter.pruefen()) == []
    assert "Ablaufdatum wird von der Registry nicht veröffentlicht" in asyncio.run(
        waechter.bericht()
    )


def test_domain_laeuft_bald_ab(alarme, planer, netz, uhr):
    bald = (uhr.jetzt + timedelta(days=6)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    waechter = waechter_bauen(
        alarme,
        planer,
        netz,
        uhr,
        domain_namen=["beispiel.com"],
        rdap=RdapClient(transport=rdap_transport(bald)),
    )
    alarme_jetzt = asyncio.run(waechter.pruefen())
    assert [a.titel for a in alarme_jetzt] == ["Domain beispiel.com läuft in 6 Tagen ab"]
    assert "beim Anbieter verlängern" in alarme_jetzt[0].text


# ----------------------------------------------------------------------------- DNS


def test_erwartet_lesen():
    erwartet = erwartet_lesen(
        "cloud.x.ch A 203.0.113.10; cloud.x.ch A 203.0.113.11; x.ch MX 10 Mail.X.ch.;"
        ' x.ch TXT "v=spf1 -all"'
    )
    assert erwartet == {
        ("cloud.x.ch", "A"): {"203.0.113.10", "203.0.113.11"},
        ("x.ch", "MX"): {"10 mail.x.ch"},
        ("x.ch", "TXT"): {"v=spf1 -all"},
    }
    with pytest.raises(ValueError):
        erwartet_lesen("cloud.x.ch A")
    with pytest.raises(ValueError):
        erwartet_lesen("cloud.x.ch SRV 1 2 3 x")
    assert wert_normal("CNAME", "Cloud.X.ch.") == "cloud.x.ch"
    assert registrierte_domain("cloud.louis-degiorgi.ch") == "louis-degiorgi.ch"


def test_dns_abweichung_und_rueckkehr(alarme, planer, netz, uhr):
    netz.dns[("cloud.x.ch", "A")] = ["203.0.113.10"]
    waechter = waechter_bauen(
        alarme,
        planer,
        netz,
        uhr,
        dns_erwartet=erwartet_lesen("cloud.x.ch A 203.0.113.10; kaputt.test A 1.2.3.4"),
    )
    assert asyncio.run(waechter.pruefen()) == []
    netz.dns[("cloud.x.ch", "A")] = ["198.51.100.66"]
    alarme_jetzt = asyncio.run(waechter.pruefen())
    assert [(a.stufe, a.titel) for a in alarme_jetzt] == [
        (AlarmStufe.WICHTIG, "DNS cloud.x.ch A weicht ab")
    ]
    assert "Erwartet: 203.0.113.10. Gefunden: 198.51.100.66" in alarme_jetzt[0].text
    assert asyncio.run(waechter.pruefen()) == []  # gleiche Abweichung: nicht nochmal
    netz.dns[("cloud.x.ch", "A")] = ["203.0.113.10"]
    assert [a.titel for a in asyncio.run(waechter.pruefen())] == ["DNS cloud.x.ch A stimmt wieder"]
    bericht = asyncio.run(
        Werkzeugkasten(zertifikat_werkzeuge(waechter)).ausfuehren("zertifikate_pruefen", {})
    )
    assert "cloud.x.ch A: wie erwartet (203.0.113.10)" in bericht
    assert "kaputt.test A: nicht prüfbar" in bericht


# ----------------------------------------------------------------------------- Einstellungen


def test_einstellungen():
    e = Einstellungen.aus_umgebung(
        {
            "ERREICHBARKEIT_DOMAINS": "cloud.louis-degiorgi.ch,git.louis-degiorgi.ch",
            "ZERTIFIKAT_WARNUNG_TAGE": "7, 30, 14",
            "DNS_ERWARTET": "cloud.louis-degiorgi.ch A 203.0.113.10",
            "WARTUNG_TAGE": "Fr,Sa",
            "UPDATES_EIGENER_HOST": "AI01",
        }
    )
    assert e.zertifikat_domain_liste == ("cloud.louis-degiorgi.ch", "git.louis-degiorgi.ch")
    assert e.domain_namen_liste == ("louis-degiorgi.ch",)
    assert e.zertifikat_warnung_tage == (30, 14, 7)
    assert e.updates_eigener_host == "ai01"
    with pytest.raises(KonfigurationsFehler):
        Einstellungen.aus_umgebung({"ZERTIFIKAT_WARNUNG_TAGE": "bald"})
    from dataclasses import replace

    gueltig = replace(e, api_key="x")
    gueltig.pruefen()
    for feld, wert in (("wartung_tage", "Feiertag"), ("dns_erwartet", "x A")):
        with pytest.raises(KonfigurationsFehler):
            replace(gueltig, **{feld: wert}).pruefen()
