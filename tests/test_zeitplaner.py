"""Zeitplaner: Speichern, Zustellen, Nachholen nach Ausfall, Pünktlichkeit, Werkzeuge, API."""

from __future__ import annotations

import asyncio
import time as zeitmessung
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten, aktuelle_anfrage
from missa.core.zeitangaben import Wiederholung
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import ZustellFehler, Zustellung
from missa.tools.erinnerungen import ErinnerungsWerkzeuge, erinnerungs_werkzeuge
from tests.conftest import API_KEY, JETZT

ZONE = ZoneInfo("Europe/Zurich")


class FakeKanal:
    """Merkt sich alle zugestellten Nachrichten (mit Zeitpunkt)."""

    name = "test"

    def __init__(self) -> None:
        self.nachrichten: list[str] = []
        self.zeiten: list[float] = []
        self.kaputt = False

    async def senden(self, text: str) -> None:
        if self.kaputt:
            raise ZustellFehler("Nextcloud nicht erreichbar")
        self.nachrichten.append(text)
        self.zeiten.append(zeitmessung.monotonic())


class Uhr:
    def __init__(self, jetzt: datetime) -> None:
        self.jetzt = jetzt

    def __call__(self) -> datetime:
        return self.jetzt

    def vor(self, **abstand: float) -> None:
        self.jetzt += timedelta(**abstand)


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 10, 19, 0, tzinfo=ZONE))


@pytest.fixture
def datei(tmp_path: Path) -> Path:
    return tmp_path / "missa.db"


@pytest.fixture
def planer(datei, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)


def zustellen(planer: Zeitplaner) -> int:
    return asyncio.run(planer.faellige_zustellen())


# ---------------------------------------------------------------------- Speichern


def test_anlegen_und_wieder_lesen(planer, uhr):
    e = planer.anlegen("Training", datetime(2026, 11, 11, 6, 30, tzinfo=ZONE))
    assert e.id == 1
    assert e.faellig == datetime(2026, 11, 11, 6, 30, tzinfo=ZONE)
    assert e.status == "offen"
    assert [x.text for x in planer.offene()] == ["Training"]


def test_erinnerungen_ueberleben_einen_neustart(datei, kanal, uhr):
    erster = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    erster.anlegen("Training", datetime(2026, 11, 11, 6, 30, tzinfo=ZONE))
    erster.anlegen("Müll", datetime(2026, 11, 12, 7, 0, tzinfo=ZONE), Wiederholung("werktags"))

    zweiter = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    offene = zweiter.offene()
    assert [e.text for e in offene] == ["Training", "Müll"]
    assert offene[1].wiederholung == Wiederholung("werktags")


def test_leerer_text_ist_ein_fehler(planer):
    with pytest.raises(WerkzeugFehler):
        planer.anlegen("  ", datetime(2026, 11, 11, 6, 30, tzinfo=ZONE))


def test_schluessel_verhindert_doppelte(planer):
    zeitpunkt = datetime(2026, 11, 11, 8, tzinfo=ZONE)
    a = planer.anlegen("Geburtstag Oma", zeitpunkt, schluessel="geb:oma:2026")
    b = planer.anlegen("Geburtstag Oma", zeitpunkt, schluessel="geb:oma:2026")
    assert a.id == b.id
    assert len(planer.offene()) == 1


# ---------------------------------------------------------------------- Zustellen


def test_nichts_ist_vor_der_zeit_faellig(planer, kanal, uhr):
    planer.anlegen("Training", uhr.jetzt + timedelta(minutes=5))
    assert zustellen(planer) == 0
    assert kanal.nachrichten == []


def test_zustellung_zum_zeitpunkt(planer, kanal, uhr):
    planer.anlegen("Training", uhr.jetzt + timedelta(minutes=5))
    uhr.vor(minutes=5)
    assert zustellen(planer) == 1
    assert kanal.nachrichten == ["⏰ Erinnerung: Training"]
    assert planer.offene() == []
    assert zustellen(planer) == 0  # nicht doppelt


def test_eigene_nachricht_im_stil_von_missa(planer, kanal, uhr):
    planer.anlegen("Training", uhr.jetzt, nachricht="⏰ Hopp, ab ins Training! 💪")
    zustellen(planer)
    assert kanal.nachrichten == ["⏰ Hopp, ab ins Training! 💪"]


def test_wiederkehrende_bleibt_offen_mit_naechstem_termin(planer, kanal, uhr):
    erster = datetime(2026, 11, 11, 7, tzinfo=ZONE)
    planer.anlegen("Medikament", erster, Wiederholung("taeglich"))
    uhr.jetzt = erster
    zustellen(planer)
    [offen] = planer.offene()
    assert offen.faellig == datetime(2026, 11, 12, 7, tzinfo=ZONE)
    assert len(kanal.nachrichten) == 1


def test_wiederkehrend_ueber_die_zeitumstellung_hinweg(planer, kanal, uhr):
    """Täglich 7:00 vom 24. bis 26.10.2026 – Umstellung auf Winterzeit am 25.10."""
    erster = datetime(2026, 10, 24, 7, tzinfo=ZONE)
    uhr.jetzt = erster - timedelta(hours=1)
    planer.anlegen("Medikament", erster, Wiederholung("taeglich"))
    utc_stunden = []
    for _ in range(3):
        [offen] = planer.offene()
        uhr.jetzt = offen.faellig
        utc_stunden.append(offen.faellig.astimezone(UTC).hour)
        assert offen.faellig.hour == 7
        zustellen(planer)
    assert utc_stunden == [5, 6, 6]
    assert len(kanal.nachrichten) == 3
    assert not any("Verspätet" in n for n in kanal.nachrichten)


# ---------------------------------------------------------------------- Nachholen nach Ausfall


def test_verpasste_erinnerung_wird_nach_dem_start_nachgeholt(datei, kanal):
    vorher = Uhr(datetime(2026, 11, 11, 6, 0, tzinfo=ZONE))
    Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=vorher).anlegen(
        "Training", datetime(2026, 11, 11, 6, 30, tzinfo=ZONE)
    )
    # Server war aus – neuer Start um 8:10
    nachher = Uhr(datetime(2026, 11, 11, 8, 10, tzinfo=ZONE))
    neu = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=nachher)
    assert zustellen(neu) == 1
    [nachricht] = kanal.nachrichten
    assert nachricht.startswith("⏰ Erinnerung: Training")
    assert "Verspätet" in nachricht
    assert "06:30 Uhr" in nachricht


def test_verpasste_wiederholungen_kommen_nur_einmal(datei, kanal):
    vorher = Uhr(datetime(2026, 11, 9, 6, 0, tzinfo=ZONE))
    Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=vorher).anlegen(
        "Medikament", datetime(2026, 11, 9, 7, tzinfo=ZONE), Wiederholung("taeglich")
    )
    # drei Tage aus
    nachher = Uhr(datetime(2026, 11, 12, 9, 0, tzinfo=ZONE))
    neu = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=nachher)
    assert zustellen(neu) == 1
    assert len(kanal.nachrichten) == 1
    [offen] = neu.offene()
    assert offen.faellig == datetime(2026, 11, 13, 7, tzinfo=ZONE)


def test_kleine_verspaetung_ohne_hinweis(planer, kanal, uhr):
    planer.anlegen("Training", uhr.jetzt)
    uhr.vor(seconds=20)
    zustellen(planer)
    assert kanal.nachrichten == ["⏰ Erinnerung: Training"]


def test_fehlgeschlagene_zustellung_wird_wiederholt(planer, kanal, uhr):
    planer.anlegen("Training", uhr.jetzt)
    kanal.kaputt = True
    assert zustellen(planer) == 0
    [offen] = planer.offene()
    assert offen.versuche == 1
    assert zustellen(planer) == 0  # wartet erst
    kanal.kaputt = False
    uhr.vor(seconds=31)
    assert zustellen(planer) == 1
    assert planer.offene() == []


def test_ohne_kanal_bleibt_die_erinnerung_liegen(datei, uhr):
    planer = Zeitplaner(datei, ZONE, Zustellung(), uhr=uhr)
    planer.anlegen("Training", uhr.jetzt)
    assert zustellen(planer) == 0
    assert len(planer.offene()) == 1


# ---------------------------------------------------------------------- Pünktlichkeit (echte Uhr)


def test_hintergrund_task_stellt_puenktlich_zu(datei, kanal):
    """Echte Uhr: Zustellung höchstens 0,5 s nach dem Zeitpunkt (verlangt: 5 s)."""

    async def ablauf() -> tuple[float, float]:
        planer = Zeitplaner(datei, ZONE, Zustellung([kanal]))
        planer.starten()
        await asyncio.sleep(0.05)
        faellig = datetime.now(ZONE).replace(microsecond=0) + timedelta(seconds=2)
        ziel = zeitmessung.monotonic() + (faellig - datetime.now(ZONE)).total_seconds()
        planer.anlegen("Training", faellig)
        for _ in range(60):
            if kanal.nachrichten:
                break
            await asyncio.sleep(0.1)
        await planer.stoppen()
        return ziel, kanal.zeiten[0] if kanal.zeiten else float("inf")

    ziel, zugestellt = asyncio.run(ablauf())
    assert kanal.nachrichten == ["⏰ Erinnerung: Training"]
    assert -0.05 <= zugestellt - ziel <= 0.5


def test_hintergrund_task_holt_beim_start_nach(datei, kanal):
    vorher = Uhr(datetime.now(ZONE) - timedelta(hours=2))
    Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=vorher).anlegen(
        "Training", datetime.now(ZONE) - timedelta(hours=1)
    )

    async def ablauf() -> None:
        planer = Zeitplaner(datei, ZONE, Zustellung([kanal]))
        planer.starten()
        for _ in range(30):
            if kanal.nachrichten:
                break
            await asyncio.sleep(0.05)
        await planer.stoppen()

    asyncio.run(ablauf())
    assert len(kanal.nachrichten) == 1
    assert "Verspätet" in kanal.nachrichten[0]


# ---------------------------------------------------------------------- Werkzeuge


@pytest.fixture
def ew(planer) -> ErinnerungsWerkzeuge:
    return ErinnerungsWerkzeuge(planer)


def test_werkzeug_bestaetigt_datum_und_uhrzeit(ew, planer):
    ergebnis = ew.anlegen("Training", "um 6:30 Uhr")
    assert "Erinnerung gespeichert #1" in ergebnis
    assert "Mittwoch, 11. November 2026 um 06:30 Uhr" in ergebnis
    assert "in 11 Stunden 30 Minuten" in ergebnis
    assert planer.offene()[0].faellig == datetime(2026, 11, 11, 6, 30, tzinfo=ZONE)


def test_werkzeug_wiederkehrend(ew):
    ergebnis = ew.anlegen("Wochenplanung", "jeden Montag um 7")
    assert "Montag, 16. November 2026 um 07:00 Uhr" in ergebnis
    assert "jeden Montag um 07:00 Uhr" in ergebnis


def test_werkzeug_fragt_bei_mehrdeutigkeit_nach(ew, planer, uhr):
    uhr.jetzt = datetime(2026, 11, 10, 7, 0, tzinfo=ZONE)
    ergebnis = ew.anlegen("Training", "um 6")
    assert ergebnis.startswith("NOCH NICHT GESPEICHERT")
    assert "18:00" in ergebnis
    assert planer.offene() == []


def test_werkzeug_warnt_ohne_kanal(datei, uhr):
    w = ErinnerungsWerkzeuge(Zeitplaner(datei, ZONE, Zustellung(), uhr=uhr))
    assert "kein Kanal" in w.anlegen("Training", "in 5 Minuten")


def test_werkzeug_anzeigen_aendern_loeschen(ew, planer):
    ew.anlegen("Training", "morgen um 7")
    ew.anlegen("Zahnarzt", "am Freitag um 14 Uhr")
    liste = ew.anzeigen()
    assert "#1 „Training“" in liste and "#2 „Zahnarzt“" in liste

    geaendert = ew.aendern(1, wann="morgen um 8", was="Lauftraining")
    assert "Lauftraining" in geaendert and "08:00 Uhr" in geaendert

    # Löschen erst nach einem Ja in einer späteren Nachricht.
    marke = aktuelle_anfrage.set("anfrage-1")
    vorschau = ew.loeschen("#2")
    assert vorschau.startswith("NOCH NICHT GELÖSCHT")
    assert ew.loeschen(2, bestaetigt=True).startswith("NICHT GELÖSCHT")
    assert len(planer.offene()) == 2
    aktuelle_anfrage.reset(marke)
    marke = aktuelle_anfrage.set("anfrage-2")
    assert "Erinnerung gelöscht" in ew.loeschen("#2", bestaetigt=True)
    aktuelle_anfrage.reset(marke)
    assert [e.text for e in planer.offene()] == ["Lauftraining"]
    with pytest.raises(WerkzeugFehler):
        ew.loeschen(2)
    assert ew.aendern(1, wiederholung_beenden=True)


def test_leere_liste(ew):
    assert ew.anzeigen() == "Es sind keine Erinnerungen geplant."


def test_schlummern_der_zuletzt_zugestellten(ew, planer, kanal, uhr):
    ew.anlegen("Training", "in 1 Minute")
    uhr.vor(minutes=1)
    zustellen(planer)
    assert planer.offene() == []
    assert "Training" in (ew.kontext("ok") or "")

    ergebnis = ew.schlummern("in 10 Minuten")
    assert "Ich erinnere dich nochmal #1" in ergebnis
    uhr.vor(minutes=10)
    zustellen(planer)
    assert kanal.nachrichten == ["⏰ Erinnerung: Training"] * 2


def test_schlummern_einer_wiederkehrenden_macht_eine_kopie(ew, planer, uhr):
    ew.anlegen("Medikament", "täglich um 19:01")
    uhr.vor(minutes=1)
    zustellen(planer)
    ew.schlummern(10)
    offene = planer.offene()
    assert len(offene) == 2
    assert {e.wiederholung is None for e in offene} == {True, False}


def test_schlummern_ohne_vorherige_erinnerung(ew):
    with pytest.raises(WerkzeugFehler, match="keine Erinnerung"):
        ew.schlummern(10)


def test_werkzeuge_haben_namen(planer):
    namen = [w.name for w in erinnerungs_werkzeuge(planer)]
    assert namen == [
        "erinnerung_anlegen",
        "erinnerungen_anzeigen",
        "erinnerung_aendern",
        "erinnerung_loeschen",
        "erinnerung_schlummern",
    ]


def test_ausfuehren_ueber_den_werkzeugkasten(planer):
    kasten = Werkzeugkasten(erinnerungs_werkzeuge(planer))
    ergebnis = asyncio.run(
        kasten.ausfuehren("erinnerung_anlegen", '{"was": "Training", "wann": "um 6:30 Uhr"}')
    )
    assert "06:30 Uhr" in ergebnis


# ---------------------------------------------------------------------- App


def test_app_registriert_werkzeuge_und_zeigt_erinnerungen(einstellungen, llama):
    kanal = FakeKanal()
    app = create_app(
        einstellungen,
        llama_transport=llama.transport,
        uhr=lambda: JETZT,
        zustellung=Zustellung([kanal]),
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        gesund = client.get("/health").json()
        assert "erinnerung_anlegen" in gesund["werkzeuge"]
        app.state.zeitplaner.anlegen("Training", datetime.now(ZONE) + timedelta(days=1))
        antwort = client.get("/api/erinnerungen").json()
    assert antwort["anzahl"] == 1
    assert antwort["kanaele"] == ["test"]
    assert antwort["erinnerungen"][0]["text"] == "Training"
    assert antwort["erinnerungen"][0]["status"] == "offen"


def test_api_braucht_anmeldung(client):
    client.headers.pop("Authorization")
    assert client.get("/api/erinnerungen").status_code == 401


def test_wiederholung_aus_uhrzeit(planer):
    e = planer.anlegen(
        "X", datetime(2026, 11, 11, 6, 30, tzinfo=ZONE), Wiederholung("woechentlich", (2,))
    )
    assert e.uhrzeit == time(6, 30)


def test_zustellung_ueber_nextcloud_talk(datei, uhr):
    from missa.tools.talk import TalkZustellkanal
    from tests.test_nextcloud_talk import FakeNextcloud
    from tests.test_talk_meldungen import ZIEL, melder

    nextcloud = FakeNextcloud()
    planer = Zeitplaner(datei, ZONE, Zustellung([TalkZustellkanal(melder(nextcloud))]), uhr=uhr)
    planer.anlegen("Training", uhr.jetzt)
    assert zustellen(planer) == 1
    [anfrage] = nextcloud.anfragen
    assert anfrage["pfad"].endswith(f"/{ZIEL}/message")
    assert anfrage["daten"]["message"] == "⏰ Erinnerung: Training"
