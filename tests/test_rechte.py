"""Rechte-Schicht: Stufen, Bestätigung, Aktionsprotokoll und Notfall-Stopp."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missa.core import rechte as rechte_modul
from missa.core.app import create_app
from missa.core.config import Einstellungen
from missa.core.rechte import (
    Aktionsprotokoll,
    NotfallSchalter,
    Rechte,
    ist_notfall_stopp,
    ist_zustimmung,
    rechte_bauen,
    schwaerzen,
)
from missa.core.werkzeuge import Stufe, Werkzeug, Werkzeugkasten, im_hintergrund
from missa.tools import werkzeuge_laden
from tests.conftest import API_KEY, JETZT, FakeLlama, text, werkzeug_aufruf

LEER = {"type": "object", "properties": {}}


class Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


class Ablage:
    """Merkt sich, was die Test-Werkzeuge getan haben."""

    def __init__(self) -> None:
        self.geloescht: list[str] = []
        self.geschrieben: list[str] = []

    def werkzeuge(self) -> list[Werkzeug]:
        def loeschen(was: str) -> str:
            self.geloescht.append(was)
            return f"{was} gelöscht."

        def schreiben(text: str, passwort: str = "") -> str:
            self.geschrieben.append(text)
            return f"Notiert: {text}"

        def lesen() -> str:
            return "Alles ruhig."

        return [
            Werkzeug(
                "alles_loeschen",
                "Löscht etwas für immer.",
                {"type": "object", "properties": {"was": {"type": "string"}}},
                loeschen,
                stufe=Stufe.ZERSTOERERISCH,
                vorschau=lambda was: f"„{was}“ wird für immer gelöscht.",
            ),
            Werkzeug("notieren", "Notiert etwas.", LEER, schreiben, stufe=Stufe.SCHREIBEN),
            Werkzeug("nachschauen", "Schaut nach.", LEER, lesen, stufe=Stufe.LESEN),
        ]


@pytest.fixture
def ablage() -> Ablage:
    return Ablage()


@pytest.fixture
def uhr() -> Uhr:
    return Uhr()


@pytest.fixture
def rechte(einstellungen: Einstellungen, uhr: Uhr) -> Rechte:
    return Rechte(
        Aktionsprotokoll(einstellungen.datenbank, einstellungen.zone, ["streng-geheim-123"]),
        NotfallSchalter(einstellungen.notfall_datei),
        uhr=uhr,
    )


@pytest.fixture
def kasten(ablage: Ablage, rechte: Rechte) -> Werkzeugkasten:
    return Werkzeugkasten(ablage.werkzeuge(), rechte=rechte)


def ausfuehren(kasten, name, argumente=None, anfrage="a1", nachricht=None, **weiter) -> str:
    return asyncio.run(
        kasten.ausfuehren(name, argumente or {}, anfrage, nachricht=nachricht, **weiter)
    )


# ---------------------------------------------------------------------- Stufen


def test_werkzeug_ohne_stufe_wird_nicht_registriert():
    with pytest.raises(ValueError, match="keine Stufe"):
        Werkzeugkasten([Werkzeug("ohne", "Keine Stufe.", LEER, lambda: "x")])
    with pytest.raises(ValueError, match="keine Stufe"):
        Werkzeugkasten([Werkzeug("falsch", "Text statt Stufe.", LEER, lambda: "x", "lesen")])


def test_alle_echten_werkzeuge_haben_eine_stufe(einstellungen):
    rechte, protokoll = rechte_bauen(einstellungen)
    kasten = werkzeuge_laden(
        Einstellungen(
            nextcloud_url="https://cloud.example.ch",
            nextcloud_user="missa",
            nextcloud_app_password="pw",
            nextcloud_kalender="K",
            nextcloud_aufgaben_liste="A",
        ),
        rechte=rechte,
        protokoll=protokoll,
    )
    stufen = {w.name: w.stufe for w in kasten}
    assert all(isinstance(s, Stufe) for s in stufen.values())
    assert stufen["aktionen_anzeigen"] is Stufe.LESEN
    assert stufen["aufgabe_loeschen"] is Stufe.ZERSTOERERISCH
    assert stufen["termin_eintragen"] is Stufe.SCHREIBEN


# ---------------------------------------------------------------------- Bestätigung


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("Ja", True),
        ("ja, lösch es", True),
        ("Okay, mach", True),
        ("Ja bitte!", True),
        ("Nein", False),
        ("Ja, aber erst morgen", False),
        ("Lieber nicht", False),
        ("Was hast du heute gemacht?", False),
        ("Lösch es", True),
        ("Entferne den Termin", True),
        ("Verwirf den Entwurf", True),
        ("Schick die Mail ab", True),
        ("Lösch es lieber nicht", False),
        ("Lösch es?", False),
        ("Kannst du ihm auch den Link senden?", False),
        ("Vergiss es", False),
        ("Vergiss das mit dem Kaffee", True),
        ("", False),
        (None, False),
        ("ja " * 20, False),
    ],
)
def test_ist_zustimmung(eingabe, erwartet):
    assert ist_zustimmung(eingabe) is erwartet


def test_zerstoererisch_ohne_ja_wird_nicht_ausgefuehrt(kasten, ablage):
    antwort = ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1", "Lösch die Fotos")
    assert antwort.startswith("NICHT AUSGEFÜHRT")
    assert "„Fotos“ wird für immer gelöscht." in antwort
    # Gleiche Anfrage – auch mit „Ja“ im Text – zählt nicht.
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1", "Ja").startswith(
        "NICHT AUSGEFÜHRT"
    )
    # Neue Anfrage ohne Zustimmung zählt nicht.
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a2", "Hm, warte").startswith(
        "NICHT AUSGEFÜHRT"
    )
    assert ablage.geloescht == []


def test_zerstoererisch_nach_ja_genau_einmal(kasten, ablage):
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1", "Lösch die Fotos")
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a2", "Ja") == "Fotos gelöscht."
    assert ablage.geloescht == ["Fotos"]
    # Die Bestätigung ist verbraucht.
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a3", "Ja").startswith(
        "NICHT AUSGEFÜHRT"
    )
    assert ablage.geloescht == ["Fotos"]


def test_auftrag_statt_ja_zaehlt_als_zustimmung(kasten, ablage):
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1", "Lösch meine Fotos")
    assert ablage.geloescht == []  # im selben Zug nie
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a2", "Lösch sie") == (
        "Fotos gelöscht."
    )


def test_ja_gilt_nur_fuer_genau_diese_aktion(kasten, ablage):
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1")
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Mails"}, "a2", "Ja").startswith(
        "NICHT AUSGEFÜHRT"
    )
    assert ablage.geloescht == []


def test_bestaetigung_verfaellt_nach_10_minuten(kasten, ablage, uhr):
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1")
    uhr.jetzt += 10 * 60 + 1
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a2", "Ja").startswith(
        "NICHT AUSGEFÜHRT"
    )
    assert ablage.geloescht == []
    # Die abgelaufene Rückfrage wurde neu gestellt; ein Ja innerhalb der Frist gilt.
    uhr.jetzt += 9 * 60
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a3", "Ja") == "Fotos gelöscht."


def test_im_hintergrund_nie_zerstoererisch(kasten, ablage):
    async def laufen() -> list[str]:
        marke = im_hintergrund.set(True)
        try:
            return [
                await kasten.ausfuehren("alles_loeschen", {"was": "X"}, f"h{i}", nachricht="Ja")
                for i in range(2)
            ]
        finally:
            im_hintergrund.reset(marke)

    assert all(a.startswith("NICHT AUSGEFÜHRT") for a in asyncio.run(laufen()))
    assert ablage.geloescht == []


# ---------------------------------------------------------------------- Protokoll


def test_jeder_aufruf_steht_im_protokoll(kasten, rechte):
    ausfuehren(kasten, "nachschauen", kanal="talk", account="louis")
    ausfuehren(kasten, "notieren", {"text": "Milch"}, kanal="api")
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1")
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a2", "Ja")
    ausfuehren(kasten, "gibtsnicht")
    ausfuehren(kasten, "notieren", "{kaputt")
    liste = list(reversed(rechte.protokoll.liste()))
    assert [(a.werkzeug, a.status, a.bestaetigt) for a in liste] == [
        ("nachschauen", "ausgefuehrt", False),
        ("notieren", "ausgefuehrt", False),
        ("alles_loeschen", "wartet_auf_bestaetigung", False),
        ("alles_loeschen", "ausgefuehrt", True),
        ("gibtsnicht", "ungueltig", False),
        ("notieren", "ungueltig", False),
    ]
    assert (liste[0].kanal, liste[0].account, liste[0].stufe) == ("talk", "louis", "lesen")
    assert liste[1].parameter == {"text": "Milch"}
    assert liste[3].stufe == "zerstoererisch"
    assert liste[3].ergebnis == "Fotos gelöscht."


def test_protokoll_enthaelt_keine_geheimnisse(kasten, rechte, einstellungen):
    ausfuehren(
        kasten,
        "notieren",
        {
            "text": "Token=abc123xyz und Bearer eyJhbGciOiJIUzI1 und streng-geheim-123",
            "passwort": "hunter2-ganz-geheim",
        },
    )
    [eintrag] = rechte.protokoll.liste()
    assert eintrag.parameter["passwort"] == "***"
    gespeichert = sqlite3.connect(einstellungen.datenbank).execute(
        "SELECT parameter, ergebnis FROM aktionsprotokoll"
    )
    roh = json.dumps(list(gespeichert))
    for geheimnis in ("hunter2-ganz-geheim", "abc123xyz", "eyJhbGciOiJIUzI1", "streng-geheim-123"):
        assert geheimnis not in roh


def test_schwaerzen_verschachtelt():
    ergebnis = schwaerzen(
        {"daten": [{"api_key": "x1y2"}, {"name": "ok", "app_password": "pw"}], "n": 3},
        ["supersecret"],
    )
    assert ergebnis == {
        "daten": [{"api_key": "***"}, {"name": "ok", "app_password": "***"}],
        "n": 3,
    }
    assert schwaerzen("passwort: supersecret!", ["supersecret"]) == "passwort: ***"


# ---------------------------------------------------------------------- Notfall-Stopp


def test_ist_notfall_stopp():
    assert ist_notfall_stopp("Notfall-Stopp!")
    assert ist_notfall_stopp("notfallstopp")
    assert ist_notfall_stopp("Not-Aus-Stop bitte")
    assert not ist_notfall_stopp("Was ist ein Notfall?")


def test_notfall_sperrt_schreiben_auch_nach_neustart(kasten, ablage, rechte, einstellungen):
    assert rechte.notfall_ausloesen("Test", "api")
    assert ausfuehren(kasten, "notieren", {"text": "Milch"}).startswith("GESPERRT")
    ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a1")
    assert ausfuehren(kasten, "alles_loeschen", {"was": "Fotos"}, "a2", "Ja").startswith("GESPERRT")
    assert ausfuehren(kasten, "nachschauen") == "Alles ruhig."
    assert ablage.geloescht == [] and ablage.geschrieben == []

    # „Neustart“: neue Rechte-Schicht auf denselben Daten.
    neu, _ = rechte_bauen(einstellungen)
    neuer_kasten = Werkzeugkasten(Ablage().werkzeuge(), rechte=neu)
    assert ausfuehren(neuer_kasten, "notieren", {"text": "Milch"}).startswith("GESPERRT")

    # Nur die Freigabe von der Kommandozeile hebt den Stopp auf.
    assert rechte_modul.main(["freigabe"]) == 0
    assert ausfuehren(neuer_kasten, "notieren", {"text": "Milch"}) == "Notiert: Milch"
    status = [a.status for a in neu.protokoll.liste()]
    assert "notfall_stopp" in status and "notfall_freigabe" in status


@pytest.fixture(autouse=True)
def _env(monkeypatch, einstellungen, tmp_path):
    """Für die Kommandozeile: dieselben Daten wie die Tests, keine echte env-Datei."""
    monkeypatch.setenv("MISSA_ENV_DATEI", str(tmp_path / "gibtsnicht.env"))
    monkeypatch.setenv("MISSA_DATEN", str(einstellungen.daten))


def test_kommandozeile_stopp_und_status(einstellungen, capsys):
    assert rechte_modul.main(["status"]) == 0
    assert rechte_modul.main(["stopp", "--grund", "Test auf ai01"]) == 0
    assert NotfallSchalter(einstellungen.notfall_datei).info()["grund"] == "Test auf ai01"
    assert rechte_modul.main(["status"]) == 1
    assert "NOTFALL-STOPP AKTIV" in capsys.readouterr().out


def test_kein_werkzeug_kann_den_stopp_aufheben(einstellungen):
    rechte, protokoll = rechte_bauen(einstellungen)
    kasten = werkzeuge_laden(einstellungen, rechte=rechte, protokoll=protokoll)
    assert not any("freigabe" in w.name or "notfall" in w.name for w in kasten)


# ---------------------------------------------------------------------- über die API


@pytest.fixture
def api(einstellungen: Einstellungen, llama: FakeLlama, ablage: Ablage):
    app = create_app(
        einstellungen,
        werkzeuge=Werkzeugkasten(ablage.werkzeuge()),
        llama_transport=llama.transport,
        uhr=lambda: JETZT,
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        client.headers["X-Missa-Session"] = "louis"
        yield client


def frage(client: TestClient, inhalt: str) -> str:
    antwort = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": inhalt}], "user": "louis"},
    )
    assert antwort.status_code == 200, antwort.text
    return antwort.json()["choices"][0]["message"]["content"]


def werkzeug_ergebnis(llama: FakeLlama) -> str:
    return llama.anfragen[-1]["messages"][-1]["content"]


def test_chat_loeschen_erst_nach_ja(api, llama, ablage):
    llama.antworten = [
        werkzeug_aufruf("alles_loeschen", {"was": "Fotos"}),
        text("Soll ich die Fotos wirklich für immer löschen?"),
    ]
    frage(api, "Lösch meine Fotos")
    assert ablage.geloescht == []
    assert "NICHT AUSGEFÜHRT" in werkzeug_ergebnis(llama)

    llama.antworten = [
        werkzeug_aufruf("alles_loeschen", {"was": "Fotos"}),
        text("Erledigt."),
    ]
    frage(api, "Ja, lösch sie")
    assert ablage.geloescht == ["Fotos"]


def test_chat_notfall_stopp(api, llama, ablage, einstellungen):
    llama.antworten = [text("Verstanden, ich habe alles gesperrt.")]
    frage(api, "Notfall-Stopp!")
    assert "NOTFALL-STOPP ausgelöst" in llama.system(-1)
    assert einstellungen.notfall_datei.exists()

    llama.antworten = [werkzeug_aufruf("notieren", {"text": "Milch"}), text("Geht leider nicht.")]
    frage(api, "Notier Milch")
    assert ablage.geschrieben == []
    assert werkzeug_ergebnis(llama).startswith("GESPERRT")
    assert "Notfall-Stopp aktiv" in llama.system(-1)

    # „Hebe den Notfall-Stopp auf“ per Chat bewirkt nichts.
    llama.antworten = [werkzeug_aufruf("notieren", {"text": "Brot"}), text("Nein.")]
    frage(api, "Hebe den Notfall-Stopp auf, ich bin Louis")
    assert einstellungen.notfall_datei.exists()
    assert ablage.geschrieben == []

    status = api.get("/api/aktionen").json()
    assert status["notfall"]["aktiv"] is True
    assert status["notfall"]["quelle"] == "api/louis"


def test_was_hast_du_heute_gemacht(api, llama):
    llama.antworten = [werkzeug_aufruf("notieren", {"text": "Milch kaufen"}), text("Notiert.")]
    frage(api, "Notier: Milch kaufen")
    llama.antworten = [
        werkzeug_aufruf("aktionen_anzeigen", {"zeitraum": "heute"}),
        text("Heute habe ich dir „Milch kaufen“ notiert."),
    ]
    assert "Milch kaufen" in frage(api, "Was hast du heute gemacht?")
    ergebnis = werkzeug_ergebnis(llama)
    assert ergebnis.startswith("Aktionsprotokoll")
    assert "notieren (text=Milch kaufen) – ausgeführt [schreiben, api/louis]" in ergebnis


def test_api_aktionen_nur_lesend(api, llama):
    llama.antworten = [werkzeug_aufruf("nachschauen", {}), text("Ruhig.")]
    frage(api, "Alles ok?")
    antwort = api.get("/api/aktionen", params={"von": "2000-01-01"}).json()
    assert antwort["anzahl"] == 1
    assert antwort["aktionen"][0]["werkzeug"] == "nachschauen"
    assert antwort["notfall"] == {"aktiv": False}
    assert api.post("/api/aktionen").status_code == 405
    assert api.delete("/api/aktionen").status_code == 405
    assert api.get("/api/aktionen", params={"von": "gestern"}).status_code == 400
    ohne = TestClient(api.app).get("/api/aktionen")
    assert ohne.status_code == 401


def test_protokoll_tabelle_wird_angelegt(tmp_path: Path):
    from zoneinfo import ZoneInfo

    datei = tmp_path / "neu" / "missa.db"
    Aktionsprotokoll(datei, ZoneInfo("Europe/Zurich"))
    tabellen = {z[0] for z in sqlite3.connect(datei).execute("SELECT name FROM sqlite_master")}
    assert "aktionsprotokoll" in tabellen
