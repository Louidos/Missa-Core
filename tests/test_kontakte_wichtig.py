# ruff: noqa: F811 – Fixtures aus test_kontakte werden als Parameter wiederverwendet
"""Gruppe 4: Geburtstage wichtiger Kontakte, letzter Kontakt, Anschluss für die Telefonie."""

from __future__ import annotations

import asyncio
from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.werkzeuge import im_hintergrund
from missa.tools.kontakte import Kontaktpflege, kontakt_satz, telefon_schluessel
from tests.conftest import API_KEY
from tests.test_kontakte import (  # noqa: F401 – Fixtures wiederverwenden
    ZONE,
    adressbuch,
    anfragen,
    kalender,
    kanal,
    planer,
    termine,
    uhr,
)


@pytest.fixture
def pflege(tmp_path, planer, adressbuch, kalender, uhr) -> Kontaktpflege:
    # Standardwerte: nur wichtige Kontakte, am Vortag und am Morgen, 30 Tage Abstand.
    return Kontaktpflege(
        tmp_path / "missa.db", ZONE, planer, adressbuch=adressbuch, kalender=kalender, uhr=uhr
    )


def zustellen(planer) -> None:
    asyncio.run(planer.faellige_zustellen())


def test_geburtstag_nur_fuer_wichtige_kontakte(pflege, planer, uhr):
    uhr.jetzt = datetime(2026, 11, 12, 7, 0, tzinfo=ZONE)  # Oma hat am 13.11. Geburtstag
    assert pflege.geburtstage_planen() == 0  # Oma ist (noch) nicht wichtig
    pflege.wichtig_markieren("Oma")
    assert pflege.geburtstage_planen() == 2


def test_wichtiger_kontakt_vortag_und_morgen(pflege, planer, kanal, uhr):
    pflege.wichtig_markieren("Oma")
    uhr.jetzt = datetime(2026, 11, 11, 9, 0, tzinfo=ZONE)
    assert pflege.geburtstage_planen() == 0  # zwei Tage vorher noch nichts

    uhr.jetzt = datetime(2026, 11, 12, 7, 0, tzinfo=ZONE)  # Vortag, vor der Prüfzeit
    pflege.geburtstage_planen()
    offene = {e.schluessel: e for e in planer.offene()}
    vortag = offene["geburtstag:uid:oma-1:2026-11-13:vorher"]
    morgen = offene["geburtstag:uid:oma-1:2026-11-13:tag"]
    assert vortag.faellig == datetime(2026, 11, 12, 8, 0, tzinfo=ZONE)
    assert morgen.faellig == datetime(2026, 11, 13, 8, 0, tzinfo=ZONE)
    assert "Morgen (Freitag, 13.11.) hat Maria Degiorgi Geburtstag – 85 Jahre!" in (
        vortag.nachricht or ""
    )

    uhr.jetzt = datetime(2026, 11, 13, 8, 0, tzinfo=ZONE)
    zustellen(planer)
    assert kanal.nachrichten[-1].startswith("🎉 Heute hat Maria Degiorgi Geburtstag (85)!")


def test_wichtig_ohne_abstand_heisst_30_tage(pflege):
    assert (
        "alle 30 Tage" in pflege.wichtig_markieren("Oma") or "Monat" in pflege.wichtige_anzeigen()
    )
    [eintrag] = pflege.alle(nur_wichtige=True)
    assert eintrag.abstand_tage == 30


def test_hinweis_nach_30_tagen(pflege, planer, kanal, uhr):
    pflege.wichtig_markieren("Oma")
    pflege.gemeldet("Oma", "2026-11-01", "Anruf")
    uhr.jetzt = datetime(2026, 11, 30, 9, 0, tzinfo=ZONE)
    assert asyncio.run(pflege.abstaende_pruefen()) == 0  # 29 Tage
    uhr.jetzt = datetime(2026, 12, 2, 9, 0, tzinfo=ZONE)
    assert asyncio.run(pflege.abstaende_pruefen()) == 1
    zustellen(planer)
    assert any("Maria Degiorgi" in n or "Oma" in n for n in kanal.nachrichten)


def test_wann_hab_ich_oma_zuletzt_angerufen(pflege, uhr):
    assert "keinen Kontakt vermerkt" in pflege.letzter_kontakt("Oma")
    pflege.gemeldet("Oma", "gestern", "Anruf")
    text = pflege.letzter_kontakt("Oma")
    assert "Montag, 9. November 2026 (Anruf) – gestern" in text


@pytest.mark.parametrize(
    ("satz", "erwartet"),
    [
        ("hab heute Oma angerufen", ("Oma", "heute", "Anruf")),
        ("Ich hab gestern mit Oma telefoniert", ("Oma", "gestern", "Anruf")),
        ("habe gerade Lea getroffen", ("Lea", "heute", "Treffen")),
        ("Hast du Oma angerufen?", None),
        ("Wann hab ich Oma angerufen?", None),
    ],
)
def test_kontakt_satz(satz, erwartet):
    assert kontakt_satz(satz) == erwartet


def test_kontext_hinweis(pflege):
    assert "kontakt_gemeldet (name=Oma, wann=heute, art=Anruf)" in (
        pflege.kontext("hab heute Oma angerufen") or ""
    )
    marke = im_hintergrund.set(True)
    try:
        assert pflege.kontext("hab heute Oma angerufen") is None
    finally:
        im_hintergrund.reset(marke)


def test_telefonnummern_vergleichen():
    assert telefon_schluessel("+41 79 000 00 00") == telefon_schluessel("079 000 00 00")
    assert telefon_schluessel("0041790000000") == telefon_schluessel("+41790000000")


def test_telefonie_fuellt_letzten_kontakt(pflege, uhr):
    eintrag = pflege.kontakt_von_aussen(
        telefon="079 000 00 00", zeitpunkt=datetime(2026, 11, 10, 7, 30, tzinfo=ZONE)
    )
    assert eintrag is not None and eintrag.name.startswith(("Oma", "Maria"))
    assert eintrag.letzte_art == "Anruf"
    assert "(Anruf)" in pflege.letzter_kontakt("Oma")
    assert pflege.kontakt_von_aussen(telefon="+1 555 123 4567") is None


def test_api_fuer_die_telefonie(einstellungen, llama, pflege):
    app = create_app(einstellungen, llama_transport=llama.transport, kontaktpflege=pflege)
    kopf = {"Authorization": f"Bearer {API_KEY}"}
    with TestClient(app) as client:
        antwort = client.post(
            "/api/kontakte/kontakt",
            headers=kopf,
            json={"telefon": "+41790000000", "zeit": "2026-11-10T07:30:00+01:00"},
        ).json()
        assert antwort["vermerkt"] is True
        assert antwort["letzter_kontakt"] == "2026-11-10"
        assert antwort["art"] == "Anruf"
        assert client.post("/api/kontakte/kontakt", headers=kopf, json={}).status_code == 400
        assert client.post("/api/kontakte/kontakt", json={"name": "Oma"}).status_code == 401
        werkzeuge = client.get("/health").json()["werkzeuge"]
    assert "letzter_kontakt" in werkzeuge


def test_einstellungen():
    from missa.core.config import Einstellungen

    e = Einstellungen()
    assert (e.kontakte_geburtstag_vorlauf, e.kontakte_abstand_tage) == (1, 30)
    assert e.kontakte_alle_geburtstage is False
    e = Einstellungen.aus_umgebung({"KONTAKTE_GEBURTSTAGE": "alle", "KONTAKTE_ABSTAND_TAGE": "21"})
    assert e.kontakte_alle_geburtstage and e.kontakte_abstand_tage == 21
    assert time(8, 0) == e.kontakte_pruefzeit
