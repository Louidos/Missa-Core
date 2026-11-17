"""Briefing nach einer Auszeit und Sammelmeldung am Ende der Ruhezeit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from missa.core.alarme import Alarme
from missa.core.app import create_app
from missa.core.briefing import Briefing, briefing_lagebild, dauer_text
from missa.core.hintergrund import Hintergrund
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from tests.conftest import FakeLlama
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr


class FakeKalender:
    def termine_anzeigen(self, von, bis):
        return f"Termine für {von}:\n- 10:00 Team-Meeting"


class Umgebung:
    def __init__(self, tmp_path: Path, start: datetime, **optionen: Any) -> None:
        self.uhr = Uhr(start)
        self.datei = tmp_path / "missa.db"
        self.kanal = FakeKanal()
        self.planer = Zeitplaner(self.datei, ZONE, Zustellung([self.kanal]), uhr=self.uhr)
        self.alarme = Alarme(self.datei, ZONE, self.planer)
        self.hintergrund = Hintergrund(self.datei, ZONE, self.planer, uhr=self.uhr)
        self.formuliert: list[str] = []
        self.optionen = optionen

    async def formulieren(self, auftrag: str, ersatz: str) -> str:
        self.formuliert.append(auftrag)
        return "Huhu, da bin ich wieder! " + ersatz

    def briefing(self, **optionen: Any) -> Briefing:
        werte = {
            "alarme": self.alarme,
            "hintergrund": self.hintergrund,
            "lagebild": briefing_lagebild(ZONE, uhr=self.uhr, kalender=FakeKalender()),
            "formulierer": self.formulieren,
            "uhr": self.uhr,
            **self.optionen,
            **optionen,
        }
        return Briefing(self.datei, ZONE, self.planer, **werte)

    def neustart(self, nach: timedelta, **optionen: Any) -> Briefing:
        """Herzschlag jetzt, dann ``nach`` später neu starten und Briefing ausarbeiten."""
        self.briefing(**optionen).herzschlag()
        self.uhr.vor(seconds=nach.total_seconds())
        briefing = self.briefing(**optionen)
        erinnerung = briefing.beim_start()
        if erinnerung is not None:
            asyncio.run(briefing.ausarbeiten(erinnerung))
        return briefing

    def zustellen(self) -> list[str]:
        asyncio.run(self.planer.faellige_zustellen())
        return self.kanal.nachrichten


@pytest.fixture
def umgebung(tmp_path: Path) -> Umgebung:
    return Umgebung(tmp_path, datetime(2026, 11, 11, 9, 0, tzinfo=ZONE))


def test_dauer_text():
    assert dauer_text(timedelta(hours=8, minutes=5)) == "8 Stunden 5 Minuten"
    assert dauer_text(timedelta(days=2, hours=1, minutes=3)) == "2 Tage 1 Stunde"
    assert dauer_text(timedelta(minutes=45)) == "45 Minuten"


def test_nach_8_stunden_auszeit_genau_eine_briefing_nachricht(umgebung):
    umgebung.planer.anlegen("Training", umgebung.uhr() + timedelta(hours=2))
    umgebung.hintergrund.einreihen("Mails sortieren", "Sortiere", art="nacht")
    umgebung.neustart(timedelta(hours=8))
    nachrichten = umgebung.zustellen()
    briefings = [n for n in nachrichten if "Briefing" in n]
    assert len(briefings) == 1
    briefing = briefings[0]
    assert briefing.startswith("Huhu, da bin ich wieder! ☀️ Briefing: Ich war 8 Stunden weg")
    assert "Erinnerung „Training“ (war für 11:00 Uhr)" in briefing
    assert "Team-Meeting" in briefing
    # das Nachholen bleibt wie es war: die Erinnerung kommt zusätzlich einzeln
    assert any(n.startswith("⏰ Erinnerung: Training") for n in nachrichten)
    assert "<<<FREMDE_DATEN" in umgebung.formuliert[0]


def test_kurzer_neustart_kein_briefing(umgebung):
    briefing = umgebung.neustart(timedelta(minutes=20))
    assert briefing._erinnerung is None
    assert umgebung.zustellen() == []


def test_erster_start_ohne_herzschlag_kein_briefing(umgebung):
    assert umgebung.briefing().beim_start() is None
    assert umgebung.briefing().zuletzt_gelaufen() == umgebung.uhr()


def test_briefing_aus(umgebung):
    umgebung.neustart(timedelta(hours=8), aktiv=False)
    assert umgebung.zustellen() == []


def test_modell_nicht_erreichbar_briefing_kommt_trotzdem(tmp_path, umgebung):
    from missa.core.chat import ChatDienst
    from missa.core.config import Einstellungen
    from missa.core.llama import LlamaClient
    from missa.core.verlauf import Verlauf
    from missa.core.werkzeuge import Werkzeugkasten

    llama = FakeLlama()
    llama.erreichbar = False
    charakter = tmp_path / "charakter.md"
    charakter.write_text("Du bist Missa.", encoding="utf-8")
    dienst = ChatDienst(
        Einstellungen(charakter_datei=charakter, daten=tmp_path),
        LlamaClient("http://llama.test", None, transport=llama.transport),
        Werkzeugkasten(),
        Verlauf(umgebung.datei),
    )
    umgebung.neustart(timedelta(hours=8), formulierer=dienst.formulieren)
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 1
    assert nachrichten[0].startswith("☀️ Briefing: Ich war 8 Stunden weg")


def test_schlichte_fassung_geht_raus_auch_wenn_ausarbeiten_haengt(umgebung):
    umgebung.briefing().herzschlag()
    umgebung.uhr.vor(hours=5)
    briefing = umgebung.briefing()
    assert briefing.beim_start() is not None  # Ausarbeiten läuft nie
    assert umgebung.zustellen() == []  # wartet die Verzögerung ab
    umgebung.uhr.vor(minutes=3, seconds=1)
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 1 and "Ich war 5 Stunden weg" in nachrichten[0]


def test_briefing_und_ende_der_ruhezeit_eine_nachricht(tmp_path):
    umgebung = Umgebung(tmp_path, datetime(2026, 11, 11, 23, 30, tzinfo=ZONE))
    umgebung.alarme.melden("wichtig", "git nicht erreichbar", schluessel="git")
    umgebung.uhr.vor(minutes=10)
    umgebung.alarme.melden("info", "Update für wb01", schluessel="update")
    # Stromausfall 23:40 bis 05:00, Start mitten in der Ruhezeit
    briefing = umgebung.neustart(timedelta(hours=5, minutes=20))
    umgebung.alarme.melden("wichtig", "Fremder Login", schluessel="login")  # nach dem Start
    assert umgebung.zustellen() == []  # nachts nichts
    umgebung.uhr.jetzt = datetime(2026, 11, 12, 7, 0, 10, tzinfo=ZONE)
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 1
    nachricht = nachrichten[0]
    assert "Ich war 5 Stunden 20 Minuten weg" in nachricht
    for titel in ("git nicht erreichbar", "Update für wb01", "Fremder Login"):
        assert titel in nachricht
    assert briefing._offen() is None


def test_notfall_kommt_auch_bei_wartendem_briefing_sofort(tmp_path):
    umgebung = Umgebung(tmp_path, datetime(2026, 11, 11, 23, 30, tzinfo=ZONE))
    umgebung.neustart(timedelta(hours=3))
    umgebung.alarme.melden("notfall", "ai01 brennt", schluessel="n")
    assert umgebung.zustellen() == ["🚨 NOTFALL: ai01 brennt"]


def test_app_legt_briefing_beim_start_an_und_vermerkt_herzschlag(einstellungen):
    from missa.core.datenbank import verbinden

    app = create_app(einstellungen, llama_transport=FakeLlama().transport)
    briefing = app.state.briefing
    assert "herzschlag" in app.state.zeitplaner._intervalle
    alt = briefing.jetzt() - timedelta(hours=8)
    briefing.herzschlag()
    with verbinden(einstellungen.datenbank) as db:
        db.execute("UPDATE herzschlag SET zeit = ?", (alt.isoformat(timespec="seconds"),))
    with TestClient(app):
        assert briefing._erinnerung is not None
        assert briefing._erinnerung.herkunft == "briefing"
        assert "Ich war 8 Stunden weg" in (briefing._erinnerung.nachricht or "")
    assert briefing.zuletzt_gelaufen() > alt


def test_app_ohne_auszeit_kein_briefing(einstellungen):
    app = create_app(einstellungen, llama_transport=FakeLlama().transport)
    with TestClient(app):
        assert app.state.briefing._erinnerung is None
