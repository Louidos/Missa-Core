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
from missa.integrationen.erreichbarkeit import ErreichbarkeitsPruefer
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
        self.ablauf = datetime.now(UTC) + timedelta(days=60)

    async def aufloesen(self, domain: str) -> list[str]:
        if domain in self.dns_kaputt:
            raise OSError("Name or service not known")
        return ["203.0.113.10"]

    async def zertifikat(self, domain: str, _timeout: float) -> datetime:
        if domain in self.offline:
            raise ConnectionRefusedError("Connection refused")
        if domain in self.zertifikat_kaputt:
            fehler = ssl.SSLCertVerificationError(1, "certificate verify failed")
            fehler.verify_message = "certificate has expired"
            raise fehler
        return self.ablauf

    def antwort(self, request: httpx.Request) -> httpx.Response:
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
