"""Erreichbarkeit der eigenen Domains: Prüfung (DNS, Zertifikat, HTTPS), Ausfall und Rückkehr."""

from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.erreichbarkeit import (
    ErreichbarkeitsPruefer,
    PruefZiel,
    ZielFehler,
    ziel_lesen,
    ziele_lesen,
)
from missa.tools.erreichbarkeit import ErreichbarkeitsWaechter, erreichbarkeit_werkzeuge
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

DOMAINS = ("cloud.louis-degiorgi.test", "git.louis-degiorgi.test", "ki.louis-degiorgi.test")


class Netz:
    """Gefälschtes Netz: DNS, Zertifikate und HTTPS-Antworten je Domain."""

    def __init__(self) -> None:
        self.dns_kaputt: set[str] = set()
        self.zertifikat_kaputt: set[str] = set()
        self.http_status: dict[str, int] = {}
        self.offline: set[str] = set()
        #: Ports, auf denen niemand antwortet (z. B. 443, wenn der Dienst auf 8443 läuft).
        self.zu: set[int] = set()
        self.zertifikat_ports: list[int] = []
        self.urls: list[str] = []
        self.ablauf = datetime.now(UTC) + timedelta(days=60)

    async def aufloesen(self, domain: str) -> list[str]:
        if domain in self.dns_kaputt:
            raise OSError("Name or service not known")
        return ["203.0.113.10"]

    async def zertifikat(self, domain: str, _timeout: float, port: int = 443) -> datetime:
        self.zertifikat_ports.append(port)
        if domain in self.offline or port in self.zu:
            raise ConnectionRefusedError("Connection refused")
        if domain in self.zertifikat_kaputt:
            fehler = ssl.SSLCertVerificationError(1, "certificate verify failed")
            fehler.verify_message = "certificate has expired"
            raise fehler
        return self.ablauf

    def antwort(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(self.http_status.get(request.url.host, 200))

    def pruefer(self, domains=DOMAINS) -> ErreichbarkeitsPruefer:
        return ErreichbarkeitsPruefer(
            domains,
            aufloesen=self.aufloesen,
            zertifikat=self.zertifikat,
            transport=httpx.MockTransport(self.antwort),
        )


@pytest.fixture
def netz() -> Netz:
    return Netz()


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def planer(tmp_path: Path, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def waechter(netz, planer) -> ErreichbarkeitsWaechter:
    alarme = Alarme(planer.datei, ZONE, planer)
    return ErreichbarkeitsWaechter(netz.pruefer(), alarme, planer.datei, ZONE)


def runde(waechter: ErreichbarkeitsWaechter, uhr: Uhr) -> list:
    gemeldet = asyncio.run(waechter.pruefen())
    uhr.vor(minutes=5)
    return gemeldet


def test_pruefer_ok_und_probleme(netz):
    pruefer = netz.pruefer()
    ok = asyncio.run(pruefer.pruefen(DOMAINS[0]))
    assert ok.ok and ok.http_status == 200 and ok.zertifikat_bis == netz.ablauf

    netz.dns_kaputt.add(DOMAINS[0])
    assert asyncio.run(pruefer.pruefen(DOMAINS[0])).problem.startswith("DNS:")

    netz.zertifikat_kaputt.add(DOMAINS[1])
    ergebnis = asyncio.run(pruefer.pruefen(DOMAINS[1]))
    assert ergebnis.problem == "Zertifikat ungültig: certificate has expired"

    netz.offline.add(DOMAINS[2])
    assert "Port 443" in asyncio.run(pruefer.pruefen(DOMAINS[2])).problem


def test_401_vom_api_key_gilt_als_erreichbar_502_nicht(netz):
    netz.http_status = {DOMAINS[2]: 401, DOMAINS[1]: 502}
    pruefer = netz.pruefer()
    assert asyncio.run(pruefer.pruefen(DOMAINS[2])).ok
    assert asyncio.run(pruefer.pruefen(DOMAINS[1])).problem == "HTTPS antwortet mit Fehler 502"


def test_ausfall_und_rueckkehr_werden_gemeldet(waechter, netz, planer, kanal, uhr):
    assert runde(waechter, uhr) == []
    netz.http_status[DOMAINS[1]] = 502
    assert runde(waechter, uhr) == []  # ein Aussetzer ist noch kein Ausfall
    ausfall = runde(waechter, uhr)
    assert [(a.stufe, a.titel) for a in ausfall] == [
        (AlarmStufe.WICHTIG, f"{DOMAINS[1]} ist nicht erreichbar")
    ]
    assert "Seit 14:05 Uhr: HTTPS antwortet mit Fehler 502" in ausfall[0].text
    # Solange es kaputt bleibt: keine Wiederholung im Minutentakt.
    for _ in range(6):
        assert runde(waechter, uhr) == []
    assert "git" in waechter.kontext("Was ist los?")

    netz.http_status.clear()
    zurueck = runde(waechter, uhr)
    assert [(a.stufe, a.titel) for a in zurueck] == [
        (AlarmStufe.INFO, f"{DOMAINS[1]} ist wieder erreichbar")
    ]
    assert "Ausfall von 14:05 bis 14:45 Uhr (40 Minuten)" in zurueck[0].text
    assert waechter.kontext("Was ist los?") is None
    asyncio.run(planer.faellige_zustellen())
    assert len(kanal.nachrichten) == 2
    assert kanal.nachrichten[0].startswith("⚠️ Wichtig:")


def test_zustand_ueberlebt_neustart(waechter, netz, planer, uhr):
    netz.dns_kaputt.add(DOMAINS[0])
    runde(waechter, uhr)
    runde(waechter, uhr)
    neu = ErreichbarkeitsWaechter(
        netz.pruefer(), Alarme(planer.datei, ZONE, planer), planer.datei, ZONE
    )
    assert runde(neu, uhr) == []  # nach dem Neustart nicht noch einmal melden
    netz.dns_kaputt.clear()
    assert [a.titel for a in runde(neu, uhr)] == [f"{DOMAINS[0]} ist wieder erreichbar"]
    zustand = neu.zustand()[DOMAINS[0]]
    assert zustand["status"] == "ok" and zustand["zertifikat_bis"]


def test_werkzeug_prueft_sofort(waechter, netz):
    netz.zertifikat_kaputt.add(DOMAINS[1])
    kasten = Werkzeugkasten(erreichbarkeit_werkzeuge(waechter))
    alle = asyncio.run(kasten.ausfuehren("erreichbarkeit_pruefen", {}))
    assert f"{DOMAINS[0]}: erreichbar (HTTP 200, Zertifikat gültig bis" in alle
    assert f"{DOMAINS[1]}: NICHT erreichbar – Zertifikat ungültig" in alle
    nur_git = asyncio.run(kasten.ausfuehren("erreichbarkeit_pruefen", {"domain": "git"}))
    assert DOMAINS[0] not in nur_git
    unbekannt = asyncio.run(kasten.ausfuehren("erreichbarkeit_pruefen", {"domain": "foo"}))
    assert "prüfe ich nicht" in unbekannt


# ----------------------------------------------------------------------------- Port und Pfad


@pytest.mark.parametrize(
    ("text", "ziel", "schluessel"),
    [
        ("cloud.x.test", PruefZiel("cloud.x.test"), "cloud.x.test"),
        ("Cloud.X.test:8443", PruefZiel("cloud.x.test", 8443), "cloud.x.test:8443"),
        ("ki.x.test/health", PruefZiel("ki.x.test", 443, "/health"), "ki.x.test/health"),
        ("ki.x.test:8443/health", PruefZiel("ki.x.test", 8443, "/health"), "ki.x.test:8443/health"),
        ("git.x.test:443/", PruefZiel("git.x.test"), "git.x.test"),
        ("api.x.test:1/status?kurz=1", PruefZiel("api.x.test", 1, "/status?kurz=1"), None),
    ],
)
def test_ziel_lesen_versteht_port_und_pfad(text, ziel, schluessel):
    gelesen = ziel_lesen(text)
    assert gelesen == ziel
    if schluessel:
        assert gelesen.schluessel == schluessel
    assert ziel_lesen(gelesen.schluessel) == gelesen


def test_ziel_url_nennt_port_nur_wenn_noetig():
    assert ziel_lesen("cloud.x.test").url == "https://cloud.x.test/"
    assert ziel_lesen("ki.x.test:8443/health").url == "https://ki.x.test:8443/health"


@pytest.mark.parametrize(
    ("text", "hinweis"),
    [
        ("cloud.x.test:0", "1 bis 65535"),
        ("cloud.x.test:65536", "1 bis 65535"),
        ("cloud.x.test:abc", "1 bis 65535"),
        ("192.168.1.10", "IP-Adresse"),
        ("192.168.1.10:8443", "IP-Adresse"),
        ("[::1]:443", "IP-Adresse"),
        ("fe80::1", "IP-Adresse"),
        ("localhost", "nicht erlaubt"),
        ("localhost:8080", "nicht erlaubt"),
        ("intern.localhost", "nicht erlaubt"),
        ("https://cloud.x.test", "ohne https://"),
        ("http:cloud.x.test", "ohne https://"),
        ("cloud .x.test", "Leerzeichen"),
        ("cloud.x.test/a b", "Leerzeichen"),
        ("cloud.x.test/#anker", "Pfad"),
        ("nas", "kein gültiger Domainname"),
        ("-kaputt.x.test", "kein gültiger Domainname"),
    ],
)
def test_ziel_lesen_lehnt_ungueltiges_ab(text, hinweis):
    with pytest.raises(ZielFehler) as fehler:
        ziel_lesen(text)
    assert hinweis in str(fehler.value)
    assert text.strip() in str(fehler.value)  # die Meldung nennt den falschen Eintrag


def test_ziele_lesen_entfernt_doppelte():
    ziele = ziele_lesen("a.x.test, A.X.test:443, a.x.test:8443;b.x.test/health")
    assert [z.schluessel for z in ziele] == ["a.x.test", "a.x.test:8443", "b.x.test/health"]


def test_pruefung_auf_anderem_port_und_pfad(netz):
    netz.zu.add(443)  # auf 443 antwortet niemand – der Dienst läuft auf 8443
    pruefer = netz.pruefer(("cloud.x.test:8443", "ki.x.test:8443/health"))
    ergebnisse = asyncio.run(pruefer.alle_pruefen())
    assert all(e.ok for e in ergebnisse)
    assert netz.zertifikat_ports == [8443, 8443]
    assert sorted(netz.urls) == ["https://cloud.x.test:8443/", "https://ki.x.test:8443/health"]
    assert [e.schluessel for e in ergebnisse] == ["cloud.x.test:8443", "ki.x.test:8443/health"]
    assert ergebnisse[1].als_dict()["port"] == 8443


def test_meldetext_nennt_den_echten_port(netz):
    netz.zu.add(8443)
    ergebnis = asyncio.run(netz.pruefer().pruefen("cloud.x.test:8443"))
    assert ergebnis.problem.startswith("Keine HTTPS-Verbindung (Port 8443):")


def test_kein_ausfall_wegen_443_wenn_8443_eingetragen(netz, planer, uhr):
    netz.zu.add(443)
    waechter = ErreichbarkeitsWaechter(
        netz.pruefer(("cloud.x.test:8443",)), Alarme(planer.datei, ZONE, planer), planer.datei, ZONE
    )
    for _ in range(3):
        assert runde(waechter, uhr) == []
    assert waechter.zustand()["cloud.x.test:8443"]["status"] == "ok"


def test_alter_zustand_mit_domainnamen_bleibt_erhalten(netz, planer, uhr):
    """Ein Ausfall, den eine ältere Version (Schlüssel = Domainname) gemeldet hat, gilt weiter."""
    from missa.core.datenbank import verbinden
    from missa.tools.erreichbarkeit import SCHEMA

    seit = datetime(2026, 11, 11, 13, 30, tzinfo=UTC).isoformat()
    with verbinden(planer.datei) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO erreichbarkeit (domain, status, seit, fehler_in_folge, gemeldet, "
            "letzte_pruefung, problem) VALUES (?, 'gestoert', ?, 4, 1, ?, 'DNS: kaputt')",
            (DOMAINS[0], seit, seit),
        )
    netz.dns_kaputt.add(DOMAINS[0])
    waechter = ErreichbarkeitsWaechter(
        netz.pruefer((DOMAINS[0], f"{DOMAINS[0]}:8443")),
        Alarme(planer.datei, ZONE, planer),
        planer.datei,
        ZONE,
    )
    assert waechter.pruefer.domains == (DOMAINS[0], f"{DOMAINS[0]}:8443")
    # Der alte Ausfall wird erkannt und nicht noch einmal gemeldet; 8443 ist ein eigenes Ziel.
    titel = [a.titel for a in runde(waechter, uhr) + runde(waechter, uhr)]
    assert titel == [f"{DOMAINS[0]}:8443 ist nicht erreichbar"]
    netz.dns_kaputt.clear()
    zurueck = [a.titel for a in runde(waechter, uhr)]
    assert f"{DOMAINS[0]} ist wieder erreichbar" in zurueck
    assert set(waechter.zustand()) == {DOMAINS[0], f"{DOMAINS[0]}:8443"}
