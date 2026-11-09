"""Aufgaben-Werkzeuge mit gemocktem CalDAV-Client."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from missa.core.werkzeuge import WerkzeugFehler, aktuelle_anfrage
from missa.tools.aufgaben import Aufgaben, aufgaben_werkzeuge, prioritaet_lesen
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.nextcloud import NextcloudVerbindung, kurz_id
from tests.caldav_fake import FakeDAV, FakeSammlung, aufgabe

ZONE = ZoneInfo("Europe/Zurich")


@pytest.fixture
def liste() -> FakeSammlung:
    return FakeSammlung("Aufgaben")


@pytest.fixture
def verbindung(liste: FakeSammlung) -> NextcloudVerbindung:
    dav = FakeDAV(FakeSammlung("Persönlich"), liste)
    return NextcloudVerbindung("https://cloud.example.ch", "louis", "pw", client_fabrik=dav)


@pytest.fixture
def aufgaben(verbindung: NextcloudVerbindung) -> Aufgaben:
    return Aufgaben(verbindung, "Aufgaben", ZONE, Bestaetigungen())


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)


@pytest.fixture
def befuellt(liste: FakeSammlung) -> FakeSammlung:
    liste.hinzufuegen(aufgabe("milch", "Milch kaufen", due=date(2099, 11, 10), priority=1))
    liste.hinzufuegen(aufgabe("velo", "Velo putzen", priority=9))
    liste.hinzufuegen(
        aufgabe("steuer", "Steuererklärung", due=date(2099, 3, 31), description="Belege")
    )
    liste.hinzufuegen(aufgabe("alt", "Altpapier", status="COMPLETED"))
    return liste


# ---------------------------------------------------------------------- anzeigen


def test_offene_aufgaben_sortiert(aufgaben, befuellt):
    text = aufgaben.aufgaben_anzeigen()
    assert text.splitlines() == [
        "3 offene Aufgaben in der Liste „Aufgaben“:",
        f"- [ ] Steuererklärung | fällig: Di 31.03.2099 | Notiz: Belege [ID {kurz_id('steuer')}]",
        f"- [ ] Milch kaufen | fällig: Di 10.11.2099 | Priorität: hoch [ID {kurz_id('milch')}]",
        f"- [ ] Velo putzen | Priorität: niedrig [ID {kurz_id('velo')}]",
    ]


def test_auch_erledigte(aufgaben, befuellt):
    text = aufgaben.aufgaben_anzeigen(nur_offene=False)
    assert text.startswith("4 Aufgaben in der Liste")
    assert text.splitlines()[-1].startswith("- [x] Altpapier")


def test_ueberfaellig_und_uhrzeit(aufgaben, liste):
    liste.hinzufuegen(aufgabe("mama", "Mama anrufen", due=datetime(2020, 1, 1, 18, 0, tzinfo=ZONE)))
    text = aufgaben.aufgaben_anzeigen()
    assert text.startswith("1 offene Aufgabe in der Liste")
    assert "fällig: Mi 01.01.2020 18:00 (überfällig)" in text


def test_leere_liste(aufgaben):
    assert aufgaben.aufgaben_anzeigen() == "Keine offenen Aufgaben in der Liste „Aufgaben“."


# ---------------------------------------------------------------------- erstellen


def test_aufgabe_erstellen(aufgaben, liste):
    text = aufgaben.aufgabe_erstellen("Steuererklärung", "2026-11-30", "hoch", "Belege sammeln")
    assert len(liste.objekte) == 1
    komponente = liste.objekte[0].icalendar_component
    assert komponente.name == "VTODO"
    assert str(komponente["summary"]) == "Steuererklärung"
    assert str(komponente["status"]) == "NEEDS-ACTION"
    assert komponente["due"].dt == date(2026, 11, 30)
    assert int(komponente["priority"]) == 1
    assert str(komponente["description"]) == "Belege sammeln"
    assert text.startswith("Neue Aufgabe in der Liste „Aufgaben“:\n- [ ] Steuererklärung")


def test_aufgabe_nur_mit_titel(aufgaben, liste):
    aufgaben.aufgabe_erstellen("Velo putzen")
    komponente = liste.objekte[0].icalendar_component
    assert komponente.get("due") is None
    assert komponente.get("priority") is None


def test_faellig_mit_uhrzeit_in_zuerich(aufgaben, liste):
    aufgaben.aufgabe_erstellen("Anrufen", "2026-11-13T17:00")
    due = liste.objekte[0].icalendar_component["due"]
    assert due.dt == datetime(2026, 11, 13, 17, 0, tzinfo=ZONE)
    assert due.params["TZID"] == "Europe/Zurich"


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [(None, None), ("", None), ("hoch", 1), ("Mittel", 5), ("niedrig", 9), (3, 3), ("7", 7)],
)
def test_prioritaet_lesen(wert, erwartet):
    assert prioritaet_lesen(wert) == erwartet


@pytest.mark.parametrize("wert", ["dringend", 0, 10, "-1"])
def test_ungueltige_prioritaet(wert):
    with pytest.raises(WerkzeugFehler, match="prioritaet"):
        prioritaet_lesen(wert)


def test_ohne_titel(aufgaben, liste):
    with pytest.raises(WerkzeugFehler, match="Titel"):
        aufgaben.aufgabe_erstellen("  ")
    assert liste.objekte == []


# ---------------------------------------------------------------------- erledigt


def test_aufgabe_erledigt_hakt_in_nextcloud_ab(aufgaben, befuellt):
    text = aufgaben.aufgabe_erledigt(kurz_id("milch"))
    assert text.startswith("Abgehakt in der Liste „Aufgaben“:\n- [x] Milch kaufen")
    komponente = next(o for o in befuellt.objekte if o.uid == "milch").icalendar_component
    assert str(komponente["status"]) == "COMPLETED"
    assert int(komponente["percent-complete"]) == 100
    assert komponente.get("completed") is not None
    assert "Milch kaufen" not in aufgaben.aufgaben_anzeigen()


def test_erledigt_ueber_titel_und_volle_uid(aufgaben, befuellt):
    aufgaben.aufgabe_erledigt("velo putzen")
    aufgaben.aufgabe_erledigt("steuer")
    assert aufgaben.aufgaben_anzeigen().startswith("1 offene Aufgabe")


def test_schon_erledigt(aufgaben, befuellt):
    assert aufgaben.aufgabe_erledigt("Altpapier").startswith("Diese Aufgabe ist schon erledigt")


def test_wiederkehrende_aufgabe_nutzt_caldav_complete(aufgaben, liste):
    objekt = liste.hinzufuegen(aufgabe("giessen", "Blumen gießen", rrule={"freq": "weekly"}))
    aufgaben.aufgabe_erledigt("Blumen gießen")
    assert objekt.erledigt_aufgerufen


def test_mehrdeutiger_titel(aufgaben, liste):
    liste.hinzufuegen(aufgabe("a", "Einkaufen"))
    liste.hinzufuegen(aufgabe("b", "Einkaufen"))
    with pytest.raises(WerkzeugFehler, match="mehrere"):
        aufgaben.aufgabe_erledigt("Einkaufen")


def test_unbekannte_aufgabe(aufgaben, befuellt):
    with pytest.raises(WerkzeugFehler, match="Keine Aufgabe mit der ID"):
        aufgaben.aufgabe_erledigt("gibtsnicht")


# ---------------------------------------------------------------------- löschen


def test_loeschen_braucht_ja_von_louis(aufgaben, befuellt):
    vorher = len(befuellt.objekte)
    text = aufgaben.aufgabe_loeschen(kurz_id("velo"))
    assert text.startswith("NOCH NICHT GELÖSCHT")
    assert f"aufgabe_id={kurz_id('velo')} und bestaetigt=true" in text
    assert aufgaben.aufgabe_loeschen(kurz_id("velo"), bestaetigt=True).startswith("NICHT GELÖSCHT")
    assert len(befuellt.objekte) == vorher

    aktuelle_anfrage.set("anfrage-2")  # Louis: "Ja, weg damit"
    text = aufgaben.aufgabe_loeschen(kurz_id("velo"), bestaetigt=True)
    assert text.startswith("Gelöscht aus der Liste „Aufgaben“:")
    assert len(befuellt.objekte) == vorher - 1
    assert all(o.uid != "velo" for o in befuellt.objekte)


def test_bestaetigt_ohne_rueckfrage_loescht_nicht(aufgaben, befuellt):
    aktuelle_anfrage.set("anfrage-2")
    assert aufgaben.aufgabe_loeschen("velo", bestaetigt=True).startswith("NICHT GELÖSCHT")
    assert any(o.uid == "velo" for o in befuellt.objekte)


# ---------------------------------------------------------------------- Werkzeuge


def test_werkzeuge(verbindung):
    namen = [w.name for w in aufgaben_werkzeuge(verbindung, "Aufgaben", ZONE, Bestaetigungen())]
    assert namen == [
        "aufgaben_anzeigen",
        "aufgabe_erstellen",
        "aufgabe_erledigt",
        "aufgabe_loeschen",
    ]


def test_liste_wird_ueber_gemeinsame_verbindung_gefunden(verbindung, liste):
    Aufgaben(verbindung, "aufgaben", ZONE, Bestaetigungen()).aufgaben_anzeigen()
    assert verbindung.sammlung("Aufgaben") is liste
