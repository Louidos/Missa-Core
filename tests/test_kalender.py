"""Kalender-Werkzeuge mit gemocktem CalDAV-Client."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from caldav.lib import error as caldav_fehler

from missa.core.werkzeuge import WerkzeugFehler, aktuelle_anfrage
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.kalender import Kalender, kalender_werkzeuge
from missa.tools.nextcloud import NextcloudFehler, NextcloudVerbindung, kurz_id
from tests.caldav_fake import FakeDAV, FakeSammlung, ereignis

ZONE = ZoneInfo("Europe/Zurich")


@pytest.fixture
def sammlung() -> FakeSammlung:
    return FakeSammlung("Persönlich")


@pytest.fixture
def dav(sammlung: FakeSammlung) -> FakeDAV:
    return FakeDAV(FakeSammlung("Arbeit"), sammlung, FakeSammlung("Aufgaben"))


@pytest.fixture
def verbindung(dav: FakeDAV) -> NextcloudVerbindung:
    return NextcloudVerbindung("https://cloud.example.ch/", "louis", "app-pw", client_fabrik=dav)


@pytest.fixture
def kalender(verbindung: NextcloudVerbindung) -> Kalender:
    return Kalender(verbindung, "persönlich", ZONE, Bestaetigungen())


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)


def neue_anfrage(kennung: str) -> None:
    aktuelle_anfrage.set(kennung)


# ---------------------------------------------------------------------- Verbindung


def test_verbindung_nutzt_remote_php_dav_und_anmeldedaten(kalender, dav):
    kalender.termine_anzeigen("2026-11-10")
    assert dav.aufrufe == [
        {
            "url": "https://cloud.example.ch/remote.php/dav",
            "username": "louis",
            "password": "app-pw",
        }
    ]


def test_kalender_wird_per_anzeigename_gesucht_und_gemerkt(kalender, verbindung, sammlung, dav):
    assert verbindung.sammlung("PERSÖNLICH ") is sammlung
    kalender.termine_anzeigen("2026-11-10")
    kalender.termine_anzeigen("2026-11-11")
    assert len(dav.aufrufe) == 1


def test_unbekannter_kalender_nennt_vorhandene(verbindung):
    with pytest.raises(NextcloudFehler, match="Vorhanden: Arbeit, Persönlich, Aufgaben"):
        Kalender(verbindung, "Ferien", ZONE, Bestaetigungen()).termine_anzeigen("2026-11-10")


def test_falsches_passwort_gibt_verstaendlichen_fehler(kalender, dav):
    dav.anmeldung_fehler = caldav_fehler.AuthorizationError("401")
    with pytest.raises(NextcloudFehler, match="App-Passwort"):
        kalender.termine_anzeigen("2026-11-10")


def test_netzwerkfehler_setzt_verbindung_zurueck(kalender, sammlung, dav):
    kalender.termine_anzeigen("2026-11-10")
    sammlung.fehler = ConnectionError("Netz weg")
    with pytest.raises(NextcloudFehler, match="nicht erreichbar"):
        kalender.termine_anzeigen("2026-11-10")
    sammlung.fehler = None
    kalender.termine_anzeigen("2026-11-10")
    assert len(dav.aufrufe) == 2


def test_http_ohne_tls_ist_erlaubt():
    dav = FakeDAV(FakeSammlung("K"))
    verbindung = NextcloudVerbindung("http://nextcloud.intern", "u", "p", client_fabrik=dav)
    verbindung.sammlung("K")
    assert dav.aufrufe[0]["require_tls"] is False


# ---------------------------------------------------------------------- Termine anzeigen


def test_termine_eines_tages(kalender, sammlung):
    sammlung.hinzufuegen(
        ereignis(
            "zahnarzt",
            "Zahnarzt",
            datetime(2026, 11, 10, 14, 0, tzinfo=ZONE),
            datetime(2026, 11, 10, 15, 0, tzinfo=ZONE),
            location="Bern",
        )
    )
    sammlung.hinzufuegen(
        ereignis(
            "frueh", "Frühstück", datetime(2026, 11, 10, 7, 30, tzinfo=ZONE), description="Gipfeli"
        )
    )
    sammlung.hinzufuegen(
        ereignis("anderer-tag", "Kino", datetime(2026, 11, 11, 20, 0, tzinfo=ZONE))
    )

    text = kalender.termine_anzeigen("2026-11-10")
    assert text.splitlines() == [
        "Termine im Kalender „persönlich“ für Dienstag, 10. November 2026:",
        f"- Di 10.11.2026 07:30: Frühstück | Notiz: Gipfeli [ID {kurz_id('frueh')}]",
        f"- Di 10.11.2026 14:00–15:00: Zahnarzt | Ort: Bern [ID {kurz_id('zahnarzt')}]",
    ]
    suche = sammlung.suchen[-1]
    assert suche["start"] == datetime(2026, 11, 10, tzinfo=ZONE)
    assert suche["end"] == datetime(2026, 11, 11, tzinfo=ZONE)
    assert suche["event"] is True
    assert suche["expand"] is True


def test_bis_ist_einschliesslich(kalender, sammlung):
    kalender.termine_anzeigen("2026-11-10", "2026-11-12")
    assert sammlung.suchen[-1]["end"] == datetime(2026, 11, 13, tzinfo=ZONE)


def test_keine_termine(kalender):
    text = kalender.termine_anzeigen("2026-11-10", "2026-11-12")
    assert text == (
        "Keine Termine im Kalender „persönlich“ für Dienstag, 10. November 2026 "
        "bis Donnerstag, 12. November 2026."
    )


def test_wiederkehrende_termine_werden_einzeln_gezeigt(kalender, sammlung):
    # So liefert caldav eine aufgelöste Serie: je Wiederholung ein Objekt mit RECURRENCE-ID.
    instanzen = []
    for tag in (9, 16):
        start = datetime(2026, 11, tag, 18, 0, tzinfo=ZONE)
        instanzen.append(
            sammlung.hinzufuegen(
                ereignis(
                    "training",
                    "Training",
                    start,
                    datetime(2026, 11, tag, 19, 30, tzinfo=ZONE),
                    recurrence_id=start,
                )
            )
        )
    sammlung.suchergebnis = instanzen
    text = kalender.termine_anzeigen("2026-11-09", "2026-11-22")
    assert "- Mo 09.11.2026 18:00–19:30: Training | wiederkehrend" in text
    assert "- Mo 16.11.2026 18:00–19:30: Training | wiederkehrend" in text


def test_ganztaegige_und_mehrtaegige_termine(kalender, sammlung):
    sammlung.hinzufuegen(ereignis("geb", "Geburtstag Anna", date(2026, 11, 10), date(2026, 11, 11)))
    sammlung.hinzufuegen(ereignis("ferien", "Ferien", date(2026, 11, 12), date(2026, 11, 15)))
    text = kalender.termine_anzeigen("2026-11-10", "2026-11-14")
    assert "- Di 10.11.2026 (ganztägig): Geburtstag Anna" in text
    assert "- Do 12.11.2026 – Sa 14.11.2026 (ganztägig): Ferien" in text


def test_utc_termine_werden_in_zuerich_zeit_gezeigt(kalender, sammlung):
    sammlung.suchergebnis = [
        sammlung.hinzufuegen(
            ereignis("utc", "Call", datetime(2026, 11, 10, 13, 0, tzinfo=ZoneInfo("UTC")))
        )
    ]
    assert "Di 10.11.2026 14:00: Call" in kalender.termine_anzeigen("2026-11-10")


@pytest.mark.parametrize(
    ("von", "bis", "meldung"),
    [
        ("morgen", None, "kein gültiges Datum"),
        ("2026-11-10", "2026-11-09", "nach „von“"),
        ("2026-01-01", "2027-06-01", "höchstens"),
    ],
)
def test_ungueltige_zeitraeume(kalender, von, bis, meldung):
    with pytest.raises(WerkzeugFehler, match=meldung):
        kalender.termine_anzeigen(von, bis)


# ---------------------------------------------------------------------- Termin eintragen


def test_termin_eintragen(kalender, sammlung):
    text = kalender.termin_eintragen("Zahnarzt", "2026-11-13T14:00", ort="Bern", notiz="Kontrolle")
    assert len(sammlung.objekte) == 1
    komponente = sammlung.objekte[0].icalendar_component
    assert str(komponente["summary"]) == "Zahnarzt"
    assert komponente["dtstart"].dt == datetime(2026, 11, 13, 14, 0, tzinfo=ZONE)
    assert komponente["dtend"].dt == datetime(2026, 11, 13, 15, 0, tzinfo=ZONE)
    assert komponente["dtstart"].params["TZID"] == "Europe/Zurich"
    assert str(komponente["location"]) == "Bern"
    assert str(komponente["description"]) == "Kontrolle"
    assert sammlung.objekte[0].icalendar_instance.walk("VTIMEZONE")
    assert text.startswith("Eingetragen im Kalender „persönlich“:\n- Fr 13.11.2026 14:00–15:00:")


def test_termin_mit_ende_und_zeitzone(kalender, sammlung):
    kalender.termin_eintragen("Meeting", "2026-11-13T09:00:00Z", "2026-11-13T10:30:00+00:00")
    komponente = sammlung.objekte[0].icalendar_component
    assert komponente["dtstart"].dt == datetime(2026, 11, 13, 10, 0, tzinfo=ZONE)
    assert komponente["dtend"].dt == datetime(2026, 11, 13, 11, 30, tzinfo=ZONE)


def test_ganztaegiger_termin(kalender, sammlung):
    kalender.termin_eintragen("Ferien", "2026-12-24", "2026-12-26")
    komponente = sammlung.objekte[0].icalendar_component
    assert komponente["dtstart"].dt == date(2026, 12, 24)
    assert komponente["dtend"].dt == date(2026, 12, 26)
    kalender.termin_eintragen("Feiertag", "2026-12-31")
    assert sammlung.objekte[1].icalendar_component["dtend"].dt == date(2027, 1, 1)


@pytest.mark.parametrize(
    ("titel", "start", "ende", "meldung"),
    [
        ("", "2026-11-13T14:00", None, "Titel"),
        ("X", "Freitag 14 Uhr", None, "kein gültiges Datum"),
        ("X", "2026-11-13T14:00", "2026-11-13T13:00", "vor dem Beginn"),
        ("X", "2026-11-13", "2026-11-13T13:00", "beide"),
    ],
)
def test_termin_eintragen_fehler(kalender, sammlung, titel, start, ende, meldung):
    with pytest.raises(WerkzeugFehler, match=meldung):
        kalender.termin_eintragen(titel, start, ende)
    assert sammlung.objekte == []


# ---------------------------------------------------------------------- Termin löschen


@pytest.fixture
def zahnarzt(sammlung) -> str:
    sammlung.hinzufuegen(
        ereignis("uid-zahnarzt", "Zahnarzt", datetime(2026, 11, 13, 14, 0, tzinfo=ZONE))
    )
    return kurz_id("uid-zahnarzt")


def test_loeschen_ohne_bestaetigung_loescht_nicht(kalender, sammlung, zahnarzt):
    text = kalender.termin_loeschen(zahnarzt)
    assert text.startswith("NOCH NICHT GELÖSCHT. Frag Louis zuerst")
    assert "Zahnarzt" in text
    assert f"termin_id={zahnarzt} und bestaetigt=true" in text
    assert len(sammlung.objekte) == 1


def test_bestaetigung_in_derselben_anfrage_reicht_nicht(kalender, sammlung, zahnarzt):
    kalender.termin_loeschen(zahnarzt)
    text = kalender.termin_loeschen(zahnarzt, bestaetigt=True)
    assert text.startswith("NICHT GELÖSCHT, Louis hat noch nicht zugestimmt.")
    assert len(sammlung.objekte) == 1


def test_bestaetigt_ohne_vorherige_rueckfrage_loescht_nicht(kalender, sammlung, zahnarzt):
    text = kalender.termin_loeschen(zahnarzt, bestaetigt=True)
    assert text.startswith("NICHT GELÖSCHT")
    assert len(sammlung.objekte) == 1


def test_loeschen_nach_ja_von_louis(kalender, sammlung, zahnarzt):
    kalender.termin_loeschen(zahnarzt)
    neue_anfrage("anfrage-2")  # Louis hat geantwortet: "Ja"
    text = kalender.termin_loeschen(zahnarzt, bestaetigt=True)
    assert text.startswith("Gelöscht aus dem Kalender „persönlich“:")
    assert sammlung.objekte == []


def test_vorschau_in_der_ja_anfrage_bricht_bestaetigung_nicht(kalender, sammlung, zahnarzt):
    kalender.termin_loeschen(zahnarzt)
    neue_anfrage("anfrage-2")
    kalender.termin_loeschen(zahnarzt)  # Modell fragt sicherheitshalber nochmal nach
    kalender.termin_loeschen(zahnarzt, bestaetigt=True)
    assert sammlung.objekte == []


def test_rueckfrage_laeuft_ab(verbindung, sammlung, zahnarzt):
    uhr = [0.0]
    kalender = Kalender(verbindung, "Persönlich", ZONE, Bestaetigungen(60, lambda: uhr[0]))
    kalender.termin_loeschen(zahnarzt)
    uhr[0] = 61
    neue_anfrage("anfrage-2")
    assert kalender.termin_loeschen(zahnarzt, bestaetigt=True).startswith("NICHT GELÖSCHT")
    assert len(sammlung.objekte) == 1


def test_loeschen_ueber_volle_uid(kalender, sammlung, zahnarzt):
    kalender.termin_loeschen("uid-zahnarzt")
    neue_anfrage("anfrage-2")
    kalender.termin_loeschen("uid-zahnarzt", bestaetigt=True)
    assert sammlung.objekte == []


def test_wiederkehrender_termin_warnt_vor_serie(kalender, sammlung):
    sammlung.hinzufuegen(
        ereignis(
            "serie",
            "Training",
            datetime(2026, 11, 2, 18, 0, tzinfo=ZONE),
            rrule={"freq": "weekly"},
        )
    )
    assert "alle Wiederholungen" in kalender.termin_loeschen(kurz_id("serie"))


def test_unbekannte_id(kalender):
    with pytest.raises(WerkzeugFehler, match="Keinen Termin mit der ID abc12345"):
        kalender.termin_loeschen("abc12345")


# ---------------------------------------------------------------------- Werkzeug-Schemas


def test_werkzeuge_und_schemas(verbindung):
    werkzeuge = kalender_werkzeuge(verbindung, "Persönlich", ZONE, Bestaetigungen())
    assert [w.name for w in werkzeuge] == [
        "termine_anzeigen",
        "termin_eintragen",
        "termin_loeschen",
    ]
    for werkzeug in werkzeuge:
        schema = werkzeug.schema()
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------- über die API


def test_loeschen_ueber_die_api_erst_nach_ja(einstellungen, llama, verbindung, sammlung, zahnarzt):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from missa.core.werkzeuge import Werkzeugkasten
    from tests.conftest import API_KEY, text, werkzeug_aufruf

    werkzeuge = Werkzeugkasten(kalender_werkzeuge(verbindung, "Persönlich", ZONE, Bestaetigungen()))
    app = create_app(einstellungen, werkzeuge=werkzeuge, llama_transport=llama.transport)
    kopf = {"Authorization": f"Bearer {API_KEY}", "X-Missa-Session": "loeschen"}
    llama.antworten = [
        # Anfrage 1: Das Modell versucht, sofort zu löschen.
        werkzeug_aufruf("termin_loeschen", {"termin_id": zahnarzt}),
        werkzeug_aufruf("termin_loeschen", {"termin_id": zahnarzt, "bestaetigt": True}, "c2"),
        text("Soll ich den Zahnarzt-Termin wirklich löschen?"),
        # Anfrage 2: Louis hat Ja gesagt.
        werkzeug_aufruf("termin_loeschen", {"termin_id": zahnarzt, "bestaetigt": True}, "c3"),
        text("Erledigt, der Termin ist gelöscht."),
    ]
    with TestClient(app) as client:
        frage = {"messages": [{"role": "user", "content": "Lösch den Zahnarzt-Termin"}]}
        client.post("/v1/chat/completions", headers=kopf, json=frage)
        assert len(sammlung.objekte) == 1
        assert llama.anfragen[2]["messages"][-1]["content"].startswith("NICHT GELÖSCHT")

        ja = {"messages": [{"role": "user", "content": "Ja"}]}
        antwort = client.post("/v1/chat/completions", headers=kopf, json=ja)
    assert sammlung.objekte == []
    assert (
        antwort.json()["choices"][0]["message"]["content"] == "Erledigt, der Termin ist gelöscht."
    )
