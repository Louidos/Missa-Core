"""Pausen und Puffer: Erkennung, eingetragene Blöcke, Hinweis bei neuen Terminen."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from missa.core.werkzeuge import aktuelle_anfrage
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.farben import Farbsystem
from missa.tools.kalender import Belegung, Kalender, freie_luecke
from missa.tools.kalender_protokoll import KalenderProtokoll
from missa.tools.nextcloud import NextcloudVerbindung
from missa.tools.pausen import PausenRegeln, PausenWaechter, pausen_werkzeuge, tag_pruefen
from tests.caldav_fake import FakeDAV, FakeSammlung, ereignis
from tests.test_zeitplaner import FakeKanal, Uhr

ZONE = ZoneInfo("Europe/Zurich")
TAG = date(2026, 11, 11)  # Mittwoch
# Die alten Tests rechnen mit 10 Min. Puffer (Standard ist seit Gruppe 2: 15 Min.).
REGELN = PausenRegeln(puffer=timedelta(minutes=10))


def um(stunde: int, minute: int = 0, tag: date = TAG) -> datetime:
    return datetime(tag.year, tag.month, tag.day, stunde, minute, tzinfo=ZONE)


def termin(
    von: datetime, bis: datetime, titel: str = "Termin", ort: str = "", **weiter
) -> Belegung:
    werte = {"kalender": "Persönlich", "uid": titel, "ganztaegig": False, "frei": False, "art": ""}
    werte.update(weiter)
    return Belegung(von, bis, titel, ort, **werte)


def pruefen(*termine: Belegung, regeln: PausenRegeln = REGELN):
    befunde, bloecke = tag_pruefen(TAG, list(termine), regeln, ZONE)
    return (
        [b.art for b in befunde],
        [(b.art, f"{b.start:%H:%M}", f"{b.ende:%H:%M}") for b in bloecke],
        befunde,
    )


# ---------------------------------------------------------------------- Erkennung


def test_drei_termine_direkt_hintereinander():
    arten, bloecke, befunde = pruefen(
        termin(um(14), um(15), "A"), termin(um(15), um(16), "B"), termin(um(16), um(17), "C")
    )
    assert arten.count("kein_puffer") == 2
    assert "„A“ (14:00–15:00) und „B“ (15:00–16:00) liegen direkt hintereinander" in befunde[0].text
    assert ("puffer", "13:50", "14:00") in bloecke
    assert ("puffer", "17:00", "17:10") in bloecke


def test_kurze_luecke_wird_mit_puffer_gefuellt():
    _, bloecke, _ = pruefen(termin(um(8), um(9), "A"), termin(um(9, 5), um(10), "B"))
    assert ("puffer", "09:00", "09:05") in bloecke


def test_genug_puffer_kein_befund():
    arten, bloecke, _ = pruefen(termin(um(8), um(9), "A"), termin(um(9, 10), um(10), "B"))
    assert "kein_puffer" not in arten
    assert not [b for b in bloecke if b[0] == "puffer"]


def test_tag_ohne_mittagspause_bekommt_pausenblock():
    arten, bloecke, _ = pruefen(termin(um(9), um(12), "Kurs"), termin(um(12, 30), um(17), "Kurs 2"))
    assert "keine_mittagspause" in arten
    assert ("mittag", "12:00", "12:30") in bloecke


def test_mittagspause_laesst_puffer_frei():
    """Termine bis 12:00 direkt hintereinander: Puffer 12:00–12:10, Pause danach."""
    _, bloecke, _ = pruefen(termin(um(10), um(11), "A"), termin(um(11), um(12), "B"))
    assert ("puffer", "12:00", "12:10") in bloecke
    assert ("mittag", "12:10", "12:40") in bloecke


def test_eingeplante_mittagspause_zaehlt():
    arten, bloecke, _ = pruefen(
        termin(um(9), um(12), "Kurs"),
        termin(um(12), um(13), "Mittagessen mit Anna"),
        termin(um(13, 30), um(17), "Kurs 2"),
    )
    assert "keine_mittagspause" not in arten
    assert not [b for b in bloecke if b[0] == "mittag"]


def test_kein_platz_fuer_mittag():
    arten, bloecke, befunde = pruefen(termin(um(11), um(14, 30), "Prüfung"))
    assert "kein_platz_mittag" in arten
    assert "„Prüfung“ (11:00–14:30) füllen alles" in befunde[0].text
    assert bloecke == []


def test_ein_termin_am_abend_braucht_keine_mittagspause():
    arten, bloecke, _ = pruefen(termin(um(19), um(20), "Kino"))
    assert arten == []
    assert bloecke == []


def test_ueberschneidung_und_wegzeit():
    arten, _, befunde = pruefen(
        termin(um(8), um(9), "A", ort="Bern"),
        termin(um(8, 30), um(9, 30), "B", ort="Bern"),
        termin(um(9, 45), um(10, 30), "C", ort="Zürich"),
    )
    assert "ueberlappung" in arten
    assert "wegzeit" in arten
    wegzeit = next(b for b in befunde if b.art == "wegzeit")
    assert "nur 15 Min." in wegzeit.text and "Bern" in wegzeit.text and "Zürich" in wegzeit.text


def test_zu_voller_tag():
    arten, _, befunde = pruefen(termin(um(7), um(12), "A"), termin(um(12, 30), um(17, 30), "B"))
    assert "zu_voll" in arten
    assert "10.0 Stunden" in next(b for b in befunde if b.art == "zu_voll").text


def test_eigene_bloecke_und_ganztaegiges_zaehlen_nicht_als_termine():
    arten, _, _ = pruefen(
        termin(um(8), um(9), "A"),
        termin(um(9), um(9, 10), "⏸️ Puffer", art="puffer", kalender="Pausen"),
        termin(um(9, 10), um(10), "B"),
        termin(um(0), um(23, 59), "Ferien", ganztaegig=True),
    )
    assert "kein_puffer" not in arten


def test_grenzwerte_sind_einstellbar():
    streng = PausenRegeln(puffer=timedelta(minutes=30), max_stunden=4)
    arten, _, _ = pruefen(
        termin(um(8), um(10), "A"), termin(um(10, 15), um(13), "B"), regeln=streng
    )
    assert "kein_puffer" in arten and "zu_voll" in arten


def test_freie_luecke():
    belegt = [termin(um(17), um(18, 30), "Chor")]
    assert freie_luecke(belegt, um(17), um(20), timedelta(minutes=30)) == um(18, 30)
    assert freie_luecke(belegt, um(17), um(18, 45), timedelta(minutes=30)) is None


# ---------------------------------------------------------------------- mit Kalender


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)


@pytest.fixture
def persoenlich() -> FakeSammlung:
    return FakeSammlung("Persönlich")


@pytest.fixture
def dav(persoenlich) -> FakeDAV:
    return FakeDAV(persoenlich)


@pytest.fixture
def kalender(dav, tmp_path) -> Kalender:
    verbindung = NextcloudVerbindung("https://cloud.example.ch", "louis", "pw", client_fabrik=dav)
    return Kalender(
        verbindung,
        "Persönlich",
        ZONE,
        Bestaetigungen(),
        KalenderProtokoll(tmp_path / "missa.db", ZONE),
        Farbsystem(tmp_path / "missa.db"),
    )


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(um(19, 0, date(2026, 11, 10)))  # Abend vorher


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def planer(tmp_path, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def waechter(kalender, planer, uhr) -> PausenWaechter:
    w = PausenWaechter(kalender, REGELN, ZONE, planer, uhr=uhr)
    kalender.nach_pruefungen.append(w.nach_termin)
    return w


def drei_hintereinander(sammlung: FakeSammlung) -> None:
    for nummer, stunde in enumerate((14, 15, 16)):
        sammlung.hinzufuegen(
            ereignis(f"t{nummer}", f"Sitzung {nummer + 1}", um(stunde), um(stunde + 1))
        )


def test_abends_nur_vorschlag_und_erst_nach_ja_eingetragen(
    waechter, dav, persoenlich, planer, kanal
):
    drei_hintereinander(persoenlich)
    text = asyncio.run(waechter.abends_pruefen())
    # Nichts ungefragt eingetragen.
    assert not any(s.name == "Pausen" for s in dav.sammlungen)
    assert "liegen direkt hintereinander" in (text or "")
    asyncio.run(planer.faellige_zustellen())
    [nachricht] = kanal.nachrichten
    assert nachricht.startswith("☕ Kurzer Blick auf morgen (Mi 11.11.2026):")
    assert "Mein Vorschlag:" in nachricht and "Soll ich dir das" in nachricht

    # Louis: „Ja, trag ein“ → Missa ruft tag_pruefen mit eintragen=true auf.
    aktuelle_anfrage.set("anfrage-2")
    werkzeug = pausen_werkzeuge(waechter)[0]
    erledigt = werkzeug.funktion("2026-11-11", eintragen=True)
    assert "Eingetragen im Kalender „Pausen“" in erledigt
    pausen = next(s for s in dav.sammlungen if s.name == "Pausen")
    assert pausen.farbe == "#40E0D0"  # türkis
    bloecke = sorted(
        (o.icalendar_component["dtstart"].dt, o.icalendar_component) for o in pausen.objekte
    )
    zeiten = [(f"{s:%H:%M}", str(k["summary"])) for s, k in bloecke]
    assert ("13:50", "⏸️ Puffer") in zeiten
    assert ("17:00", "⏸️ Puffer") in zeiten
    assert ("12:00", "🍽️ Mittagspause") in zeiten
    for _, komponente in bloecke:
        assert str(komponente["x-missa-art"]) in ("puffer", "mittag")
        assert str(komponente["color"]) == "turquoise"


def test_eingetragene_bloecke_sind_rueckgaengig_machbar(waechter, kalender, dav, persoenlich):
    persoenlich.hinzufuegen(ereignis("k1", "Kurs", um(9), um(12)))
    persoenlich.hinzufuegen(ereignis("k2", "Kurs 2", um(12, 30), um(17)))
    asyncio.run(waechter.abends_pruefen())
    aktuelle_anfrage.set("anfrage-2")
    pausen_werkzeuge(waechter)[0].funktion("2026-11-11", eintragen=True)
    pausen = next(s for s in dav.sammlungen if s.name == "Pausen")
    [mittag] = pausen.objekte
    assert mittag.icalendar_component["dtstart"].dt == um(12)
    kalender.rueckgaengig()
    assert pausen.objekte == []


def test_ruhiger_tag_keine_nachricht(waechter, persoenlich, planer):
    persoenlich.hinzufuegen(ereignis("k", "Kino", um(19), um(21)))
    assert asyncio.run(waechter.abends_pruefen()) is None
    assert planer.offene() == []


def test_neuer_termin_ohne_puffer_gibt_hinweis_und_alternative(kalender, waechter, persoenlich):
    persoenlich.hinzufuegen(ereignis("a", "Sitzung", um(10), um(11)))
    text = kalender.termin_eintragen("Telefonat", "2026-11-11T11:00", "2026-11-11T11:30")
    assert text.startswith("Eingetragen")
    assert "PAUSEN-HINWEIS für Mittwoch, 11. November 2026" in text
    assert "liegen direkt hintereinander" in text
    assert "Alternative: Der Termin hätte um 11:10 Uhr mit genug Puffer Platz" in text


def test_neuer_termin_mit_puffer_ohne_hinweis(kalender, waechter, persoenlich):
    persoenlich.hinzufuegen(ereignis("a", "Sitzung", um(10), um(11)))
    text = kalender.termin_eintragen("Telefonat", "2026-11-11T15:00", "2026-11-11T15:30")
    assert "PAUSEN-HINWEIS" not in text


def test_verschieben_prueft_auch(kalender, waechter, persoenlich):
    from missa.tools.nextcloud import kurz_id

    persoenlich.hinzufuegen(ereignis("a", "Sitzung", um(10), um(11)))
    persoenlich.hinzufuegen(ereignis("b", "Telefonat", um(15), um(15, 30)))
    kalender.termin_verschieben(kurz_id("b"), start="2026-11-11T11:00")
    aktuelle_anfrage.set("anfrage-2")  # Louis: „Ja“
    text = kalender.termin_verschieben(kurz_id("b"), start="2026-11-11T11:00", bestaetigt=True)
    assert "PAUSEN-HINWEIS" in text


def test_werkzeug_tag_pruefen(waechter, persoenlich, dav):
    drei_hintereinander(persoenlich)
    werkzeug = pausen_werkzeuge(waechter)[0]
    vorschau = werkzeug.funktion("2026-11-11")
    assert "Pausen-Prüfung für Mittwoch, 11. November 2026" in vorschau
    assert "Vorschlag:" in vorschau and "Frag Louis" in vorschau
    assert not any(s.name == "Pausen" for s in dav.sammlungen)
    # Im selben Zug eintragen geht nicht – erst nach Louis' Antwort.
    assert "NOCH NICHTS EINGETRAGEN" in werkzeug.funktion("2026-11-11", eintragen=True)
    assert not any(s.name == "Pausen" for s in dav.sammlungen)
    aktuelle_anfrage.set("anfrage-2")
    erledigt = werkzeug.funktion("2026-11-11", eintragen=True)
    assert "Eingetragen im Kalender „Pausen“" in erledigt


def test_einstellungen_pausen():
    from missa.core.config import Einstellungen

    e = Einstellungen.aus_umgebung(
        {
            "PAUSEN_PUFFER_MINUTEN": "15",
            "PAUSEN_MITTAG_MINUTEN": "45",
            "PAUSEN_MITTAG_VON": "12:00",
            "PAUSEN_MITTAG_BIS": "13:30",
            "PAUSEN_MAX_STUNDEN": "7,5",
            "PAUSEN_PRUEFZEIT": "20:15",
            "PAUSEN_AKTIV": "nein",
        }
    )
    regeln = e.pausen_regeln()
    assert regeln.puffer == timedelta(minutes=15)
    assert regeln.mittag_dauer == timedelta(minutes=45)
    assert (regeln.mittag_von, regeln.mittag_bis) == (time(12), time(13, 30))
    assert regeln.max_stunden == 7.5
    assert e.pausen_pruefzeit == time(20, 15)
    assert not e.pausen_aktiv
    assert Einstellungen().pausen_aktiv


def test_app_registriert_pausen(einstellungen, llama, waechter):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app

    app = create_app(einstellungen, llama_transport=llama.transport, pausen=waechter)
    with TestClient(app) as client:
        assert "tag_pruefen" in client.get("/health").json()["werkzeuge"]
    assert "pausen" in app.state.zeitplaner._auftraege
    assert waechter.formulierer is not None
