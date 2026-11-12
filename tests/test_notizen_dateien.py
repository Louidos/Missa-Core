"""Gruppe 3: To-dos aus Gesprächen, Notizen (Nextcloud Notes) und Dateisuche (WebDAV)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.werkzeuge import Werkzeugkasten, aktuelle_anfrage, im_hintergrund
from missa.tools.aufgaben import Aufgaben, aufgaben_werkzeuge
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.dateien import Dateien, dateien_werkzeuge
from missa.tools.nextcloud import NextcloudVerbindung
from missa.tools.notizen import Notizen, notizen_werkzeuge
from missa.tools.todo_erkennung import aufgaben_hinweis, todo_finden
from tests.caldav_fake import FakeDAV, FakeSammlung
from tests.conftest import API_KEY, text, werkzeug_aufruf
from tests.nextcloud_http_fake import FakeNextcloudHttp

ZONE = ZoneInfo("Europe/Zurich")


@pytest.fixture
def nc() -> FakeNextcloudHttp:
    return FakeNextcloudHttp()


@pytest.fixture
def dateien(nc) -> Dateien:
    return Dateien("https://cloud.example.ch", "louis", "pw", ZONE, transport=nc.transport)


@pytest.fixture
def notizen(nc) -> Notizen:
    return Notizen("https://cloud.example.ch", "louis", "pw", ZONE, transport=nc.transport)


# ---------------------------------------------------------------------- To-dos erkennen


@pytest.mark.parametrize(
    ("satz", "aufgabe"),
    [
        ("Ich muss noch Rail-Kit bestellen.", "Rail-Kit bestellen"),
        ("muss noch das Rail-Kit bestellen, sonst wird das nix", "Das Rail-Kit bestellen"),
        ("Ich sollte morgen unbedingt Oma anrufen", "Oma anrufen"),
        ("Nicht vergessen: Steuererklärung abgeben", "Steuererklärung abgeben"),
        ("Ich muss Milch kaufen!", "Milch kaufen"),
        ("Todo: Velo putzen", "Velo putzen"),
    ],
)
def test_todo_wird_erkannt(satz, aufgabe):
    assert todo_finden(satz) == aufgabe


@pytest.mark.parametrize(
    "satz",
    [
        "Muss ich noch was kaufen?",
        "Ich muss los",
        "Das muss nicht sein",
        "Wie spät ist es?",
        "Du musst noch was machen",
        "Was muss ich morgen noch erledigen?",
    ],
)
def test_kein_todo(satz):
    assert todo_finden(satz) is None


def test_hinweis_nur_im_gespraech():
    assert "Rail-Kit bestellen" in (aufgaben_hinweis("Ich muss noch Rail-Kit bestellen") or "")
    marke = im_hintergrund.set(True)
    try:
        assert aufgaben_hinweis("Ich muss noch Rail-Kit bestellen") is None
    finally:
        im_hintergrund.reset(marke)


def test_satz_im_gespraech_legt_aufgabe_in_nextcloud_an(einstellungen, llama):
    liste = FakeSammlung("Aufgaben", art="VTODO")
    verbindung = NextcloudVerbindung(
        "https://cloud.example.ch", "louis", "pw", client_fabrik=FakeDAV(liste)
    )
    kasten = Werkzeugkasten(aufgaben_werkzeuge(verbindung, "Aufgaben", ZONE, Bestaetigungen()))
    app = create_app(einstellungen, werkzeuge=kasten, llama_transport=llama.transport)
    llama.antworten = [
        werkzeug_aufruf("aufgabe_erstellen", {"titel": "Rail-Kit bestellen"}),
        text("Klar! Ich hab dir „Rail-Kit bestellen“ als Aufgabe angelegt. 📝"),
    ]
    with TestClient(app) as client:
        antwort = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"messages": [{"role": "user", "content": "Ich muss noch Rail-Kit bestellen"}]},
        ).json()
    system = llama.system(0)
    assert "## Mögliche Aufgabe" in system and "„Rail-Kit bestellen“" in system
    [objekt] = liste.objekte
    assert str(objekt.icalendar_component["summary"]) == "Rail-Kit bestellen"
    assert "angelegt" in antwort["choices"][0]["message"]["content"]


def test_doppelte_aufgabe_wird_nicht_angelegt():
    liste = FakeSammlung("Aufgaben", art="VTODO")
    verbindung = NextcloudVerbindung(
        "https://cloud.example.ch", "louis", "pw", client_fabrik=FakeDAV(liste)
    )
    aufgaben = Aufgaben(verbindung, "Aufgaben", ZONE, Bestaetigungen())
    aufgaben.aufgabe_erstellen("Rail-Kit bestellen")
    assert "gibt es schon" in aufgaben.aufgabe_erstellen("rail-kit  bestellen")
    assert len(liste.objekte) == 1


# ---------------------------------------------------------------------- Notizen


def test_schreib_dir_auf_legt_notiz_an(notizen, nc):
    text_ = notizen.anlegen("WLAN Gäste", "Passwort hängt am Kühlschrank")
    assert "Notiz in Nextcloud Notes angelegt" in text_
    [notiz] = nc.notizen.values()
    assert notiz["title"] == "WLAN Gäste"
    assert notiz["category"] == "Missa"
    assert "Passwort hängt am Kühlschrank" in notiz["content"]


def test_notiz_ergaenzen_suchen_lesen(notizen, nc):
    nummer = nc.notiz("Homelab", "# Homelab\nrp01 ist der Proxy", "Technik")
    nc.notiz("Einkauf", "Milch, Brot")
    assert "ergänzt" in notizen.ergaenzen("Homelab", "ai01 hat 64 GB RAM")
    assert "ai01 hat 64 GB RAM" in nc.notizen[nummer]["content"]
    assert "rp01 ist der Proxy" in nc.notizen[nummer]["content"]  # nichts verloren

    treffer = notizen.suchen("proxy")
    assert "#1 „Homelab“" in treffer and "Einkauf" not in treffer
    assert "Keine Notiz" in notizen.suchen("Ferien")
    assert "64 GB RAM" in notizen.lesen("#1")


def test_notizen_haben_kein_loeschen(notizen):
    namen = [w.name for w in notizen_werkzeuge(notizen)]
    assert namen == ["notiz_anlegen", "notiz_ergaenzen", "notizen_suchen", "notiz_lesen"]


# ---------------------------------------------------------------------- Dateien


@pytest.fixture
def homelab(nc) -> FakeNextcloudHttp:
    nc.datei("Doku/Homelab/rp01.md", "# rp01\nIP: 10.0.30.5\nCaddy als Reverse Proxy")
    nc.datei("Doku/Homelab/ai01.md", "# ai01\nIP: 10.0.30.20")
    nc.datei("Fotos/rp01-rack.jpg", "binär")
    nc.datei("Rechnungen/2026.pdf", "%PDF")
    return nc


def test_wo_ist_die_doku_zu_rp01(dateien, homelab):
    text_ = dateien.suchen("rp01")
    zeilen = text_.splitlines()
    assert zeilen[1].startswith("- Doku/Homelab/rp01.md")  # Textdatei zuerst
    assert "Fotos/rp01-rack.jpg" in text_
    assert "Rechnungen" not in text_


def test_suche_in_einem_ordner_und_ohne_treffer(dateien, homelab):
    assert "Fotos" not in dateien.suchen("rp01", ordner="Doku")
    assert "Keine Datei" in dateien.suchen("minecraft")


def test_textdatei_lesen(dateien, homelab):
    text_ = dateien.lesen("Doku/Homelab/rp01.md")
    assert "IP: 10.0.30.5" in text_
    from missa.core.werkzeuge import WerkzeugFehler

    with pytest.raises(WerkzeugFehler, match="keine Textdatei"):
        dateien.lesen("Rechnungen/2026.pdf")
    with pytest.raises(WerkzeugFehler, match="gibt es nicht"):
        dateien.lesen("Doku/fehlt.md")


def test_ordner_anzeigen(dateien, homelab):
    text_ = dateien.auflisten("Doku/Homelab")
    assert "- Doku/Homelab/ai01.md" in text_ and "- Doku/Homelab/rp01.md" in text_


def test_schreiben_im_eigenen_ordner(dateien, nc):
    assert "angelegt" in dateien.schreiben("zusammenfassung.md", "Hallo")
    assert nc.dateien["Missa/zusammenfassung.md"] == b"Hallo"
    assert "Missa" in nc.ordner
    assert "ergänzt" in dateien.schreiben("Missa/zusammenfassung.md", "Mehr", anhaengen=True)
    assert nc.dateien["Missa/zusammenfassung.md"] == b"Hallo\n\nMehr"
    dateien.schreiben("Berichte/woche.md", "x")
    assert "Missa/Berichte/woche.md" in nc.dateien


def test_bestehende_datei_wird_nicht_ueberschrieben(dateien, nc):
    from missa.core.werkzeuge import WerkzeugFehler

    dateien.schreiben("a.md", "erste Fassung")
    with pytest.raises(WerkzeugFehler, match="überschreiben tue ich nichts"):
        dateien.schreiben("a.md", "weg damit")
    assert nc.dateien["Missa/a.md"] == b"erste Fassung"


@pytest.mark.parametrize(
    "pfad",
    [
        "../Doku/Homelab/rp01.md",
        "/Doku/Homelab/rp01.md",
        "Missa/../Doku/x.md",
        "Missa/../../etc/passwd.txt",
        "./../x.md",
        "/Missa2/x.md",
        "..\\Doku\\x.md",
    ],
)
def test_schreiben_ausserhalb_von_missa_ist_unmoeglich(dateien, homelab, pfad):
    from missa.core.werkzeuge import WerkzeugFehler

    vorher = dict(homelab.dateien)
    with pytest.raises(WerkzeugFehler, match="nicht in meinem Ordner"):
        dateien.schreiben(pfad, "überschrieben!")
    assert homelab.dateien == vorher
    assert homelab.schreibende() == []


def test_jeder_schreibende_zugriff_bleibt_in_missa(dateien, homelab):
    for pfad in ("a.md", "Missa/b.md", "Unterordner/c.md", "Missa/x/y/z.md"):
        dateien.schreiben(pfad, "x")
    for methode, pfad in homelab.schreibende():
        assert pfad.startswith("/remote.php/dav/files/louis/Missa"), (methode, pfad)
        assert methode in ("PUT", "MKCOL")


def test_loeschen_gibt_es_nicht(dateien):
    namen = [w.name for w in dateien_werkzeuge(dateien)]
    assert namen == ["dateien_suchen", "ordner_anzeigen", "datei_lesen", "datei_schreiben"]
    assert not [n for n in dir(dateien) if "loesch" in n or "delete" in n.lower()]


def test_werkzeuge_mit_stufen(dateien, notizen):
    from missa.core.werkzeuge import Stufe

    stufen = {w.name: w.stufe for w in [*dateien_werkzeuge(dateien), *notizen_werkzeuge(notizen)]}
    assert stufen["datei_schreiben"] is Stufe.SCHREIBEN
    assert stufen["dateien_suchen"] is Stufe.LESEN
    assert stufen["notiz_anlegen"] is Stufe.SCHREIBEN


def test_app_laedt_notizen_und_dateien(einstellungen, llama):
    from dataclasses import replace

    nc_einstellungen = replace(
        einstellungen,
        nextcloud_url="https://cloud.example.ch",
        nextcloud_user="louis",
        nextcloud_app_password="pw",
    )
    app = create_app(nc_einstellungen, llama_transport=llama.transport)
    with TestClient(app) as client:
        werkzeuge = client.get("/health").json()["werkzeuge"]
    for name in ("notiz_anlegen", "notizen_suchen", "dateien_suchen", "datei_schreiben"):
        assert name in werkzeuge


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)
