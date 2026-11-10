"""Volle Kalenderverwaltung: ändern, verschieben, Farben, Kategorien, Erinnerungen,
Wiederholungen mit Ausnahmen, eigene Kalender und Rückgängig."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest

from missa.core.werkzeuge import WerkzeugFehler, aktuelle_anfrage
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.farben import Farbsystem, farbe_lesen, farbe_name
from missa.tools.kalender import Kalender, erinnerungen_lesen, rrule_lesen, rrule_text
from missa.tools.kalender_protokoll import KalenderProtokoll
from missa.tools.nextcloud import NextcloudVerbindung, kurz_id
from tests.caldav_fake import FakeDAV, FakeSammlung, aufgabe, ereignis

ZONE = ZoneInfo("Europe/Zurich")


@pytest.fixture
def sammlung() -> FakeSammlung:
    return FakeSammlung("Persönlich", farbe="#0082C9FF")


@pytest.fixture
def aufgabenliste() -> FakeSammlung:
    liste = FakeSammlung("Aufgaben", art="VTODO")
    liste.hinzufuegen(aufgabe("t1", "Milch"))
    return liste


@pytest.fixture
def dav(sammlung, aufgabenliste) -> FakeDAV:
    return FakeDAV(sammlung, FakeSammlung("Arbeit", farbe="#4169E1"), aufgabenliste)


@pytest.fixture
def kalender(dav, tmp_path) -> Kalender:
    verbindung = NextcloudVerbindung("https://cloud.example.ch", "louis", "pw", client_fabrik=dav)
    datei = tmp_path / "missa.db"
    return Kalender(
        verbindung,
        "Persönlich",
        ZONE,
        Bestaetigungen(),
        KalenderProtokoll(datei, ZONE),
        Farbsystem(datei),
    )


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)


@pytest.fixture
def meeting(sammlung) -> str:
    sammlung.hinzufuegen(
        ereignis(
            "uid-meeting",
            "Meeting",
            datetime(2026, 11, 11, 10, 0, tzinfo=ZONE),
            datetime(2026, 11, 11, 11, 0, tzinfo=ZONE),
            location="Büro",
        )
    )
    return kurz_id("uid-meeting")


def komponente(sammlung: FakeSammlung, nummer: int = 0):
    return sammlung.objekte[nummer].icalendar_component


# ---------------------------------------------------------------------- Ändern und Verschieben


def test_verschieben_auf_15_uhr_und_rot_faerben(kalender, sammlung, meeting):
    """„Verschieb das Meeting morgen auf 15 Uhr und mach es rot“."""
    text = kalender.termin_aendern(meeting, start="2026-11-11T15:00", farbe="rot")
    termin = komponente(sammlung)
    assert termin["dtstart"].dt == datetime(2026, 11, 11, 15, 0, tzinfo=ZONE)
    assert termin["dtend"].dt == datetime(2026, 11, 11, 16, 0, tzinfo=ZONE)  # Dauer bleibt
    assert str(termin["color"]) == "red"  # COLOR nach RFC 7986, so zeigt es Nextcloud an
    assert str(termin["location"]) == "Büro"
    assert sammlung.objekte[0].gespeichert == 1
    assert "vorher: Mi 11.11.2026 10:00–11:00: Meeting" in text
    assert "jetzt:  Mi 11.11.2026 15:00–16:00: Meeting" in text
    assert "Farbe: rot" in text
    assert "Änderung #1" in text


def test_titel_ort_notiz_aendern_und_entfernen(kalender, sammlung, meeting):
    kalender.termin_aendern(meeting, titel="Teamsitzung", ort="", notiz="Agenda mitbringen")
    termin = komponente(sammlung)
    assert str(termin["summary"]) == "Teamsitzung"
    assert "location" not in termin
    assert str(termin["description"]) == "Agenda mitbringen"


def test_neues_ende(kalender, sammlung, meeting):
    kalender.termin_aendern(meeting, ende="2026-11-11T12:30")
    assert komponente(sammlung)["dtend"].dt == datetime(2026, 11, 11, 12, 30, tzinfo=ZONE)


def test_ganztaegig_machen(kalender, sammlung, meeting):
    kalender.termin_aendern(meeting, ganztaegig=True)
    termin = komponente(sammlung)
    assert termin["dtstart"].dt == date(2026, 11, 11)
    assert termin["dtend"].dt == date(2026, 11, 12)


def test_nichts_zu_aendern(kalender, meeting):
    with pytest.raises(WerkzeugFehler, match="nichts zu ändern"):
        kalender.termin_aendern(meeting)


def test_termin_in_anderem_kalender_wird_gefunden(kalender, dav):
    arbeit = dav.sammlungen[1]
    arbeit.hinzufuegen(ereignis("uid-a", "Sprint", datetime(2026, 11, 12, 9, 0, tzinfo=ZONE)))
    text = kalender.termin_aendern(kurz_id("uid-a"), farbe="blau")
    assert "Kalender „Arbeit“" in text
    assert str(arbeit.objekte[0].icalendar_component["color"]) == "royalblue"


# ---------------------------------------------------------------------- Farben, Kategorien, Alarme


def test_eintragen_mit_allem(kalender, sammlung):
    text = kalender.termin_eintragen(
        "Vorlesung",
        "2026-11-12T08:15",
        "2026-11-12T10:00",
        ort="Raum 101",
        kategorien=["Lehre"],
        erinnerungen=[15, 1440],
    )
    termin = komponente(sammlung)
    assert str(termin["color"]) == "green"  # Farbsystem: Lehre = grün
    assert "Lehre" in termin["categories"].to_ical().decode()
    alarme = [c for c in termin.subcomponents if c.name == "VALARM"]
    assert sorted(a["trigger"].dt for a in alarme) == [timedelta(days=-1), timedelta(minutes=-15)]
    assert all(str(a["action"]) == "DISPLAY" for a in alarme)
    assert "Kategorien: Lehre" in text
    assert "Erinnerung: 15 Min. vorher, 1 Tag vorher" in text


def test_eigene_farbe_schlaegt_farbsystem(kalender, sammlung):
    kalender.termin_eintragen("X", "2026-11-12T08:00", kategorien="Lehre", farbe="#E53935")
    assert str(komponente(sammlung)["color"]) == "crimson"


def test_farbe_und_alarme_entfernen(kalender, sammlung, meeting):
    kalender.termin_aendern(meeting, farbe="rot", erinnerungen=[10])
    kalender.termin_aendern(meeting, farbe="keine", erinnerungen=[])
    termin = komponente(sammlung)
    assert "color" not in termin
    assert not [c for c in termin.subcomponents if c.name == "VALARM"]


def test_ganztaegiger_termin_mit_flag(kalender, sammlung):
    kalender.termin_eintragen("Ferien", "2026-12-21T00:00", "2026-12-24T00:00", ganztaegig=True)
    termin = komponente(sammlung)
    assert termin["dtstart"].dt == date(2026, 12, 21)
    assert termin["dtend"].dt == date(2026, 12, 25)


def test_farbsystem_festlegen(kalender, sammlung):
    assert "Server = rot" in kalender.farbsystem_festlegen("Server", "rot")
    kalender.termin_eintragen("Backup prüfen", "2026-11-12T20:00", kategorien=["Server"])
    assert str(komponente(sammlung)["color"]) == "red"
    assert "Server" not in kalender.farbsystem_festlegen("Server")


@pytest.mark.parametrize(
    ("eingabe", "css", "hex_wert"),
    [
        ("rot", "red", "#FF0000"),
        ("Hellblau", "lightblue", "#ADD8E6"),
        ("türkis", "turquoise", "#40E0D0"),
        ("red", "red", "#FF0000"),
        ("#e53935", "crimson", "#E53935"),
        ("0f0", "limegreen", "#00FF00"),
    ],
)
def test_farbe_lesen(eingabe, css, hex_wert):
    assert farbe_lesen(eingabe) == (css, hex_wert)


def test_unbekannte_farbe():
    with pytest.raises(WerkzeugFehler, match="kenne ich nicht"):
        farbe_lesen("regenbogen")


def test_farbe_name():
    assert farbe_name("red") == "rot"
    assert farbe_name("#FF0000") == "rot"
    assert farbe_name("#123456") == "#123456"


@pytest.mark.parametrize(
    ("eingabe", "minuten"),
    [(15, [15]), ("15, 60", [15, 60]), ("1 Tag", [1440]), ("2 Stunden", [120]), ([30], [30])],
)
def test_erinnerungen_lesen(eingabe, minuten):
    assert erinnerungen_lesen(eingabe) == minuten


# ---------------------------------------------------------------------- Wiederholungen


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("täglich", {"FREQ": "DAILY"}),
        ("werktags", {"FREQ": "WEEKLY", "BYDAY": ["MO", "TU", "WE", "TH", "FR"]}),
        ("jeden Montag und Donnerstag", {"FREQ": "WEEKLY", "BYDAY": ["MO", "TH"]}),
        ("alle 2 Wochen", {"FREQ": "WEEKLY", "INTERVAL": 2}),
        ("monatlich, 10 mal", {"FREQ": "MONTHLY", "COUNT": 10}),
        ("jährlich", {"FREQ": "YEARLY"}),
    ],
)
def test_rrule_lesen(eingabe, erwartet):
    assert rrule_lesen(eingabe, datetime(2026, 11, 2, 18, tzinfo=ZONE), ZONE) == erwartet


def test_rrule_mit_ende_und_roh():
    regel = rrule_lesen("wöchentlich bis 31.12.2026", datetime(2026, 11, 2, 18, tzinfo=ZONE), ZONE)
    assert regel["FREQ"] == "WEEKLY"
    assert regel["UNTIL"] == datetime(2026, 12, 31, 22, 59, 59, tzinfo=ZoneInfo("UTC"))
    assert rrule_lesen("FREQ=WEEKLY;BYDAY=MO", date(2026, 11, 2), ZONE)["BYDAY"] == ["MO"]
    with pytest.raises(WerkzeugFehler):
        rrule_lesen("ab und zu", date(2026, 11, 2), ZONE)


def test_rrule_text():
    assert rrule_text({"FREQ": ["WEEKLY"], "BYDAY": ["MO"]}) == "wöchentlich am Montag"
    assert rrule_text({"FREQ": ["WEEKLY"], "BYDAY": ["MO", "TU", "WE", "TH", "FR"]}) == "werktags"
    assert rrule_text({"FREQ": ["WEEKLY"], "INTERVAL": [2]}) == "alle 2 Wochen"


@pytest.fixture
def training(kalender, sammlung) -> str:
    kalender.termin_eintragen(
        "Training", "2026-11-02T18:00", "2026-11-02T19:30", wiederholung="jeden Montag"
    )
    return kurz_id(str(komponente(sammlung)["uid"]))


def test_serie_anlegen_und_einzeln_anzeigen(kalender, sammlung, training):
    assert dict(komponente(sammlung)["rrule"]) == {"FREQ": ["WEEKLY"], "BYDAY": ["MO"]}
    text = kalender.termine_anzeigen("2026-11-09", "2026-11-22")
    assert text.count("Training") == 2
    assert "wiederkehrend" in text


def test_nur_ein_vorkommen_verschieben(kalender, sammlung, training):
    kalender.termin_aendern(training, nur_am="2026-11-16", start="2026-11-16T19:00")
    objekt = sammlung.objekte[0]
    termine = list(objekt.icalendar_instance.walk("VEVENT"))
    assert len(termine) == 2  # Serie + Ausnahme in einem Objekt
    ausnahme = next(t for t in termine if t.get("recurrence-id") is not None)
    assert ausnahme["recurrence-id"].dt == datetime(2026, 11, 16, 18, 0, tzinfo=ZONE)
    assert ausnahme["dtstart"].dt == datetime(2026, 11, 16, 19, 0, tzinfo=ZONE)
    assert ausnahme["dtend"].dt == datetime(2026, 11, 16, 20, 30, tzinfo=ZONE)

    text = kalender.termine_anzeigen("2026-11-16", "2026-11-23")
    assert "Mo 16.11.2026 19:00–20:30: Training" in text
    assert "Mo 23.11.2026 18:00–19:30: Training" in text


def test_nur_ein_vorkommen_loeschen(kalender, sammlung, training):
    text = kalender.termin_loeschen(training, nur_am="2026-11-23")
    assert "nur am Mo 23.11.2026 gelöscht" in text
    assert "exdate" in komponente(sammlung)
    anzeige = kalender.termine_anzeigen("2026-11-16", "2026-11-30")
    assert "23.11.2026" not in anzeige
    assert "16.11.2026" in anzeige and "30.11.2026" in anzeige


def test_nur_am_an_einem_tag_ohne_vorkommen(kalender, training):
    with pytest.raises(WerkzeugFehler, match="findet am"):
        kalender.termin_aendern(training, nur_am="2026-11-17", farbe="rot")


def test_serie_beenden(kalender, sammlung, training):
    kalender.termin_aendern(training, nur_am="2026-11-16", farbe="rot")
    kalender.termin_aendern(training, wiederholung="keine")
    termine = list(sammlung.objekte[0].icalendar_instance.walk("VEVENT"))
    assert len(termine) == 1
    assert "rrule" not in termine[0]


# ---------------------------------------------------------------------- eigene Kalender


def test_kalender_anlegen_und_einfaerben(kalender, dav):
    text = kalender.kalender_anlegen("Lehre", "grün")
    assert text.startswith("Kalender „Lehre“ mit der Farbe grün angelegt.")
    neu = dav.sammlungen[-1]
    assert (neu.name, neu.farbe) == ("Lehre", "#008000")
    uebersicht = kalender.kalender_anzeigen()
    assert "- Lehre (Farbe: grün)" in uebersicht
    assert "- Persönlich (Farbe: #0082C9) [Standard]" in uebersicht
    assert "Aufgaben" not in uebersicht  # Aufgabenlisten sind keine Terminkalender
    assert "Lehre = grün" in uebersicht


def test_kalender_bekommt_farbe_aus_farbsystem(kalender, dav):
    kalender.kalender_anlegen("Pausen")
    assert dav.sammlungen[-1].farbe == "#40E0D0"


def test_doppelter_kalender(kalender):
    with pytest.raises(WerkzeugFehler, match="gibt es schon"):
        kalender.kalender_anlegen("arbeit")


def test_kalender_umbenennen_und_einfaerben(kalender, dav):
    text = kalender.kalender_aendern("Arbeit", neuer_name="Job", farbe="orange")
    assert "umbenannt in „Job“ und neue Farbe orange" in text
    assert (dav.sammlungen[1].name, dav.sammlungen[1].farbe) == ("Job", "#FFA500")


def test_standardkalender_wird_nicht_umbenannt(kalender):
    with pytest.raises(WerkzeugFehler, match="Standardkalender"):
        kalender.kalender_aendern("Persönlich", neuer_name="Privat")
    assert "neue Farbe rot" in kalender.kalender_aendern("Persönlich", farbe="rot")


def test_termin_in_neuem_kalender(kalender, dav):
    kalender.kalender_anlegen("Server")
    text = kalender.termin_eintragen("Update ai01", "2026-11-12T21:00", kalender="server")
    assert "Kalender „Server“" in text
    assert len(dav.sammlungen[-1].objekte) == 1


def test_alle_kalender_anzeigen(kalender, sammlung, dav, meeting):
    dav.sammlungen[1].hinzufuegen(
        ereignis("uid-a", "Sprint", datetime(2026, 11, 11, 9, 0, tzinfo=ZONE))
    )
    text = kalender.termine_anzeigen("2026-11-11", kalender="alle")
    assert text.startswith("Termine in allen Kalendern")
    assert "Sprint | Kalender: Arbeit" in text
    assert "Meeting | Kalender: Persönlich" in text


# ---------------------------------------------------------------------- Rückgängig


def test_loeschen_rueckgaengig(kalender, sammlung, meeting):
    kalender.termin_loeschen(meeting)
    assert sammlung.objekte == []
    text = kalender.rueckgaengig()
    assert text.startswith("Rückgängig gemacht:")
    [objekt] = sammlung.objekte
    assert str(objekt.icalendar_component["uid"]) == "uid-meeting"
    assert str(objekt.icalendar_component["summary"]) == "Meeting"


def test_aenderung_rueckgaengig(kalender, sammlung, meeting):
    kalender.termin_aendern(meeting, start="2026-11-11T15:00", farbe="rot")
    kalender.rueckgaengig()
    termin = komponente(sammlung)
    assert termin["dtstart"].dt == datetime(2026, 11, 11, 10, 0, tzinfo=ZONE)
    assert "color" not in termin


def test_eintragen_rueckgaengig(kalender, sammlung):
    kalender.termin_eintragen("Kino", "2026-11-14T20:00")
    kalender.rueckgaengig()
    assert sammlung.objekte == []


def test_rueckgaengig_nach_nummer_und_nicht_doppelt(kalender, sammlung, meeting):
    kalender.termin_aendern(meeting, farbe="rot")  # #1
    kalender.termin_eintragen("Kino", "2026-11-14T20:00")  # #2
    kalender.rueckgaengig(1)
    assert "color" not in komponente(sammlung)
    assert len(sammlung.objekte) == 2
    with pytest.raises(WerkzeugFehler, match="schon rückgängig"):
        kalender.rueckgaengig("#1")
    kalender.rueckgaengig()  # die letzte offene: #2
    assert len(sammlung.objekte) == 1


def test_kalender_anlegen_rueckgaengig(kalender, dav):
    kalender.kalender_anlegen("Lehre", "grün")
    kalender.rueckgaengig()
    assert [s.name for s in dav.sammlungen] == ["Persönlich", "Arbeit", "Aufgaben"]


def test_kalender_mit_terminen_wird_nicht_geloescht(kalender, dav):
    kalender.kalender_anlegen("Lehre")
    kalender.termin_eintragen("Vorlesung", "2026-11-12T08:00", kalender="Lehre")
    with pytest.raises(WerkzeugFehler, match="inzwischen Termine"):
        kalender.rueckgaengig(1)


def test_kalender_aendern_rueckgaengig(kalender, dav):
    kalender.kalender_aendern("Arbeit", neuer_name="Job", farbe="orange")
    kalender.rueckgaengig()
    assert (dav.sammlungen[1].name, dav.sammlungen[1].farbe) == ("Arbeit", "#4169E1")


def test_nichts_rueckgaengig_zu_machen(kalender):
    with pytest.raises(WerkzeugFehler, match="keine Kalenderänderung"):
        kalender.rueckgaengig()


def test_protokoll_vorher_nachher(kalender, meeting, tmp_path):
    kalender.termin_aendern(meeting, farbe="rot")
    kalender.termin_loeschen(meeting)
    [loeschen, aendern] = KalenderProtokoll(tmp_path / "missa.db", ZONE).liste()
    assert loeschen.aktion == "termin_geloescht" and loeschen.nachher is None
    assert "COLOR:red" in (loeschen.vorher or "")
    assert aendern.aktion == "termin_geaendert"
    assert "COLOR" not in (aendern.vorher or "")
    assert "COLOR:red" in (aendern.nachher or "")
    icalendar.Calendar.from_ical(aendern.nachher)  # gültiges iCalendar
    text = kalender.protokoll_anzeigen()
    assert "Termin gelöscht" in text and "Termin geändert" in text


# ---------------------------------------------------------------------- Ablauf über die API


def test_chat_verschieben_und_rueckgaengig(einstellungen, llama, sammlung, dav, meeting, tmp_path):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.kalender import kalender_werkzeuge
    from tests.conftest import API_KEY, text, werkzeug_aufruf

    verbindung = NextcloudVerbindung("https://cloud.example.ch", "louis", "pw", client_fabrik=dav)
    werkzeuge = Werkzeugkasten(
        kalender_werkzeuge(
            verbindung,
            "Persönlich",
            ZONE,
            Bestaetigungen(),
            KalenderProtokoll(einstellungen.datenbank, ZONE),
            Farbsystem(einstellungen.datenbank),
        )
    )
    app = create_app(einstellungen, werkzeuge=werkzeuge, llama_transport=llama.transport)
    kopf = {"Authorization": f"Bearer {API_KEY}", "X-Missa-Session": "kalender"}
    llama.antworten = [
        werkzeug_aufruf("termine_anzeigen", {"von": "2026-11-11"}),
        werkzeug_aufruf(
            "termin_aendern",
            {"termin_id": meeting, "start": "2026-11-11T15:00", "farbe": "rot"},
            "c2",
        ),
        text("Erledigt: Das Meeting ist jetzt morgen um 15 Uhr und rot. 🔴"),
        werkzeug_aufruf("kalender_rueckgaengig", {}, "c3"),
        text("Zurückgesetzt – das Meeting ist wieder um 10 Uhr."),
    ]
    with TestClient(app) as client:
        frage = "Verschieb das Meeting morgen auf 15 Uhr und mach es rot"
        client.post(
            "/v1/chat/completions",
            headers=kopf,
            json={"messages": [{"role": "user", "content": frage}]},
        )
        assert komponente(sammlung)["dtstart"].dt.hour == 15
        assert str(komponente(sammlung)["color"]) == "red"
        protokoll = client.get("/api/kalender/protokoll", headers=kopf).json()
        assert protokoll["anzahl"] == 1
        assert protokoll["aenderungen"][0]["aktion"] == "termin_geaendert"

        client.post(
            "/v1/chat/completions",
            headers=kopf,
            json={"messages": [{"role": "user", "content": "Mach das rückgängig"}]},
        )
    assert komponente(sammlung)["dtstart"].dt.hour == 10
    assert "color" not in komponente(sammlung)
