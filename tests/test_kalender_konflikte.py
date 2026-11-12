"""Gruppe 2: Kalender mit Rückfrage vor Löschen/Verschieben, Konflikten, Pausen und Puffern."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from missa.core.vorgaben import Vorgaben
from missa.core.werkzeuge import Werkzeugkasten, aktuelle_anfrage
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.farben import Farbsystem
from missa.tools.kalender import Kalender, kalender_werkzeuge
from missa.tools.kalender_protokoll import KalenderProtokoll
from missa.tools.nextcloud import NextcloudVerbindung, kurz_id
from missa.tools.pausen import PausenRegeln, PausenWaechter, pausen_werkzeuge, tag_pruefen
from tests.caldav_fake import FakeDAV, FakeSammlung, ereignis
from tests.test_pausen import termin, um

ZONE = ZoneInfo("Europe/Zurich")
TAG = date(2026, 11, 11)  # Mittwoch („morgen“ aus Sicht von JETZT in conftest)


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)


@pytest.fixture
def sammlung() -> FakeSammlung:
    return FakeSammlung("Persönlich")


@pytest.fixture
def dav(sammlung) -> FakeDAV:
    return FakeDAV(sammlung, FakeSammlung("Arbeit"))


@pytest.fixture
def datei(tmp_path):
    return tmp_path / "missa.db"


@pytest.fixture
def kalender(dav, datei) -> Kalender:
    verbindung = NextcloudVerbindung("https://cloud.example.ch", "louis", "pw", client_fabrik=dav)
    return Kalender(
        verbindung,
        "Persönlich",
        ZONE,
        Bestaetigungen(),
        KalenderProtokoll(datei, ZONE),
        Farbsystem(datei),
    )


@pytest.fixture
def waechter(kalender, datei) -> PausenWaechter:
    w = PausenWaechter(
        kalender,
        PausenRegeln(),
        ZONE,
        uhr=lambda: datetime(2026, 11, 10, 19, 0, tzinfo=ZONE),
        vorgaben=Vorgaben(datei),
    )
    kalender.nach_pruefungen.append(w.nach_termin)
    kalender.vor_pruefungen.append(w.vor_termin)
    return w


def neue_nachricht(kennung: str) -> None:
    aktuelle_anfrage.set(kennung)


# ---------------------------------------------------------------------- Fertig-wenn


def test_zahnarzt_morgen_14_uhr_rot(kalender, waechter, sammlung):
    """„Trag mir morgen 14 Uhr Zahnarzt ein, rot“."""
    text = kalender.termin_eintragen("Zahnarzt", "2026-11-11T14:00", farbe="rot")
    assert text.startswith("Eingetragen im Kalender „Persönlich“")
    [objekt] = sammlung.objekte
    komponente = objekt.icalendar_component
    assert str(komponente["summary"]) == "Zahnarzt"
    assert komponente["dtstart"].dt == um(14)
    assert str(komponente["color"]) == "red"


def test_doppelbuchung_wird_vor_dem_eintragen_gemeldet(kalender, waechter, sammlung):
    sammlung.hinzufuegen(ereignis("s", "Sitzung", um(14), um(15)))
    text = kalender.termin_eintragen("Zahnarzt", "2026-11-11T14:30", farbe="rot")
    assert "NICHTS EINGETRAGEN" in text
    assert "Doppelbuchung: überschneidet sich mit „Sitzung“ (14:00–15:00)" in text
    assert "Lösungsvorschlag: um 15:15 Uhr wäre Platz" in text
    assert len(sammlung.objekte) == 1

    # Louis: „Trag ihn trotzdem ein“ → nächste Nachricht, gleiche Angaben, bestaetigt.
    neue_nachricht("anfrage-2")
    text = kalender.termin_eintragen("Zahnarzt", "2026-11-11T14:30", farbe="rot", bestaetigt=True)
    assert text.startswith("Eingetragen")
    assert len(sammlung.objekte) == 2


def test_bestaetigt_im_selben_zug_reicht_nicht(kalender, waechter, sammlung):
    sammlung.hinzufuegen(ereignis("s", "Sitzung", um(14), um(15)))
    kalender.termin_eintragen("Zahnarzt", "2026-11-11T14:30")
    assert "NICHTS EINGETRAGEN" in kalender.termin_eintragen(
        "Zahnarzt", "2026-11-11T14:30", bestaetigt=True
    )
    assert len(sammlung.objekte) == 1


def test_konflikt_in_einem_anderen_kalender(kalender, waechter, dav):
    dav.sammlungen[1].hinzufuegen(ereignis("a", "Sprint", um(9), um(10)))
    assert "Doppelbuchung" in kalender.termin_eintragen("Arzt", "2026-11-11T09:30")


def test_ortswechsel_braucht_30_minuten(kalender, waechter, sammlung):
    sammlung.hinzufuegen(ereignis("b", "Kurs", um(9), um(10), location="Bern"))
    text = kalender.termin_eintragen("Treffen", "2026-11-11T10:15", ort="Zürich")
    assert "NICHTS EINGETRAGEN" in text
    assert "Ortswechsel zu knapp" in text and "nur 15 Min." in text
    assert "mindestens 30 Min." in text
    # Am gleichen Ort ist 15 Min. Abstand in Ordnung.
    assert kalender.termin_eintragen("Mittag", "2026-11-11T10:15", ort="Bern").startswith(
        "Eingetragen"
    )


def test_ganztaegige_termine_blockieren_nichts(kalender, waechter, sammlung):
    sammlung.hinzufuegen(ereignis("f", "Ferien", date(2026, 11, 11), date(2026, 11, 12)))
    assert kalender.termin_eintragen("Zahnarzt", "2026-11-11T14:00").startswith("Eingetragen")


def test_tag_ohne_luecke_gibt_hinweis_auf_pause():
    befunde, _ = tag_pruefen(
        TAG,
        [termin(um(8), um(10), "A"), termin(um(10), um(12), "B")],
        PausenRegeln(),
        ZONE,
    )
    pause = next(b for b in befunde if b.art == "keine_pause")
    assert "Von 08:00 bis 12:00 sind 4.0 Stunden Termine am Stück ohne Pause" in pause.text
    assert "Vorschlag: eine Pause ab 10:00" in pause.text


def test_genau_drei_stunden_oder_mit_pause_ist_in_ordnung():
    regeln = PausenRegeln()
    befunde, _ = tag_pruefen(TAG, [termin(um(8), um(11), "A")], regeln, ZONE)
    assert "keine_pause" not in [b.art for b in befunde]
    befunde, _ = tag_pruefen(
        TAG, [termin(um(8), um(10), "A"), termin(um(10, 15), um(12, 15), "B")], regeln, ZONE
    )
    assert "keine_pause" not in [b.art for b in befunde]


def test_tagesuebersicht_weist_auf_fehlende_pause_hin(kalender, waechter, sammlung):
    for nummer, stunde in enumerate((8, 9, 10, 11)):
        sammlung.hinzufuegen(ereignis(f"t{nummer}", f"Block {nummer}", um(stunde), um(stunde + 1)))
    text = kalender.termine_anzeigen("2026-11-11")
    assert "PAUSEN-HINWEIS für Mittwoch, 11. November 2026" in text
    assert "am Stück ohne Pause" in text
    assert "trag nichts ungefragt ein" in text
    assert sammlung.objekte and len(sammlung.objekte) == 4  # nichts dazugekommen
    # Lange Zeiträume bekommen keine Hinweise (sonst wird es unübersichtlich).
    assert "PAUSEN-HINWEIS" not in kalender.termine_anzeigen("2026-11-09", "2026-11-15")


def test_neuer_termin_ohne_puffer_hinweis_aber_eingetragen(kalender, waechter, sammlung):
    sammlung.hinzufuegen(ereignis("s", "Sitzung", um(10), um(11)))
    text = kalender.termin_eintragen("Telefonat", "2026-11-11T11:05", "2026-11-11T11:30")
    assert text.startswith("Eingetragen")
    assert "liegen direkt hintereinander (5 Min. statt mindestens 15 Min. Puffer)" in text


# ---------------------------------------------------------------------- Verschieben und Löschen


def test_verschieben_zeigt_konflikt_in_der_rueckfrage(kalender, waechter, sammlung):
    sammlung.hinzufuegen(ereignis("s", "Sitzung", um(14), um(15)))
    sammlung.hinzufuegen(ereignis("z", "Zahnarzt", um(9), um(10)))
    text = kalender.termin_verschieben(kurz_id("z"), start="2026-11-11T14:00")
    assert "NOCH NICHT VERSCHOBEN" in text
    assert "Doppelbuchung" in text and "Sitzung" in text
    assert sammlung.objekte[1].icalendar_component["dtstart"].dt == um(9)


def test_loeschen_und_verschieben_sind_zerstoererisch():
    from missa.core.werkzeuge import Stufe

    dav = FakeDAV(FakeSammlung("Persönlich"))
    verbindung = NextcloudVerbindung("https://x", "louis", "pw", client_fabrik=dav)
    stufen = {
        w.name: w.stufe
        for w in kalender_werkzeuge(verbindung, "Persönlich", ZONE, Bestaetigungen())
    }
    assert stufen["termin_loeschen"] is Stufe.ZERSTOERERISCH
    assert stufen["termin_verschieben"] is Stufe.ZERSTOERERISCH
    assert stufen["termin_eintragen"] is Stufe.SCHREIBEN
    assert stufen["termin_aendern"] is Stufe.SCHREIBEN


def test_loeschen_ueber_die_rechte_schicht_nur_nach_ja(kalender, sammlung):
    sammlung.hinzufuegen(ereignis("z", "Zahnarzt", um(9), um(10)))
    kasten = Werkzeugkasten(
        kalender_werkzeuge(
            kalender.verbindung, "Persönlich", ZONE, Bestaetigungen(), kalender=kalender
        )
    )
    argumente = {"termin_id": kurz_id("z")}
    antwort = asyncio.run(
        kasten.ausfuehren("termin_loeschen", argumente, "a1", nachricht="Lösch den Zahnarzt")
    )
    assert antwort.startswith("NICHT AUSGEFÜHRT")
    assert "Zahnarzt" in antwort
    assert len(sammlung.objekte) == 1
    # Eine Rückfrage statt eines Ja löscht nichts.
    antwort = asyncio.run(
        kasten.ausfuehren("termin_loeschen", argumente, "a2", nachricht="Welcher Zahnarzt?")
    )
    assert len(sammlung.objekte) == 1
    asyncio.run(kasten.ausfuehren("termin_loeschen", argumente, "a3", nachricht="Nein"))
    assert len(sammlung.objekte) == 1
    asyncio.run(kasten.ausfuehren("termin_loeschen", argumente, "a4", nachricht="x"))
    antwort = asyncio.run(kasten.ausfuehren("termin_loeschen", argumente, "a5", nachricht="Ja"))
    assert antwort.startswith("Gelöscht")
    assert sammlung.objekte == []


# ---------------------------------------------------------------------- Regeln


def test_standardwerte():
    from missa.core.config import Einstellungen

    regeln = Einstellungen().pausen_regeln()
    assert regeln.puffer == timedelta(minutes=15)
    assert regeln.pause_nach == timedelta(hours=3)
    assert regeln.pause_dauer == timedelta(minutes=15)
    assert regeln.wegzeit == timedelta(minutes=30)
    e = Einstellungen.aus_umgebung({"PAUSEN_NACH_STUNDEN": "2,5", "PAUSEN_PAUSE_MINUTEN": "20"})
    assert e.pausen_regeln().pause_nach == timedelta(hours=2.5)
    assert e.pausen_regeln().pause_dauer == timedelta(minutes=20)


def test_regeln_per_gespraech_aendern_und_gespeichert(kalender, waechter, datei, sammlung):
    werkzeuge = {w.name: w for w in pausen_werkzeuge(waechter)}
    assert "Puffer zwischen Terminen: 15 Min." in werkzeuge["pausen_regeln_anzeigen"].funktion()
    text = werkzeuge["pausen_regeln_einstellen"].funktion(
        puffer_minuten=20, pause_nach_stunden=2, ortswechsel_minuten=45
    )
    assert "Puffer zwischen Terminen: 20 Min." in text
    assert "nach spätestens 2 Std." in text
    assert "Abstand bei Ortswechsel: 45 Min." in text

    # Neuer Wächter (z. B. nach einem Neustart) liest die gespeicherten Werte.
    neu = PausenWaechter(kalender, PausenRegeln(), ZONE, vorgaben=Vorgaben(datei))
    assert neu.regeln.puffer == timedelta(minutes=20)
    assert neu.regeln.wegzeit == timedelta(minutes=45)
    sammlung.hinzufuegen(ereignis("b", "Kurs", um(9), um(10), location="Bern"))
    assert "Ortswechsel zu knapp" in (neu.vor_termin(um(10, 35), um(11), "X", "Zürich", None) or "")


def test_regeln_pruefen_die_werte(waechter):
    from missa.core.werkzeuge import WerkzeugFehler

    with pytest.raises(WerkzeugFehler):
        waechter.regeln_einstellen(pause_nach_stunden=0)
    with pytest.raises(WerkzeugFehler):
        waechter.regeln_einstellen(puffer_minuten="viel")
    with pytest.raises(WerkzeugFehler):
        waechter.regeln_einstellen()
