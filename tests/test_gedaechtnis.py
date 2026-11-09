"""Gedächtnis: Fakten, Volltextsuche, Kontext im Systemprompt, API."""

from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

import pytest

from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten, aktuelle_anfrage
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.gedaechtnis import Gedaechtnis, gedaechtnis_werkzeuge, suchbegriffe
from tests.conftest import FakeLlama, text, werkzeug_aufruf

ZONE = ZoneInfo("Europe/Zurich")


@pytest.fixture
def gedaechtnis(tmp_path) -> Gedaechtnis:
    return Gedaechtnis(tmp_path / "missa.db", ZONE)


@pytest.fixture
def werkzeuge_kasten(gedaechtnis) -> Werkzeugkasten:
    return Werkzeugkasten(gedaechtnis_werkzeuge(gedaechtnis, Bestaetigungen()))


@pytest.fixture
def werkzeuge(einstellungen):
    """Für den API-Client: die echten Gedächtnis-Werkzeuge auf der Test-Datenbank."""
    gedaechtnis = Gedaechtnis(einstellungen.datenbank, ZONE)
    return Werkzeugkasten(gedaechtnis_werkzeuge(gedaechtnis, Bestaetigungen()))


def ausfuehren(kasten: Werkzeugkasten, name: str, anfrage: str = "a1", **argumente) -> str:
    import asyncio

    return asyncio.run(kasten.ausfuehren(name, argumente, anfrage))


# ---------------------------------------------------------------------- Speicher und Suche


def test_suchbegriffe():
    assert suchbegriffe("Wie trinke ich meinen Kaffee?") == ["trink", "kaffe"]
    assert suchbegriffe("Was weißt du über Louis' Schwester?") == ["schwest"]
    assert suchbegriffe("ich du er") == []


def test_tabelle_fakten_mit_fts5(gedaechtnis):
    with sqlite3.connect(gedaechtnis.datei) as db:
        spalten = [z[1] for z in db.execute("PRAGMA table_info(fakten)")]
        tabellen = {z[0] for z in db.execute("SELECT name FROM sqlite_master")}
    assert spalten == ["id", "text", "kategorie", "erstellt"]
    assert "fakten_fts" in tabellen


def test_hinzufuegen_und_suchen(gedaechtnis):
    kaffee, neu = gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    assert neu and kaffee.id == 1 and kaffee.kategorie == "vorlieben"
    gedaechtnis.hinzufuegen("Louis' Schwester heißt Anna", "sonstiges")
    gedaechtnis.hinzufuegen("Missa-Core läuft auf ai01", "projekte")
    gedaechtnis.hinzufuegen("Der Zahnarzt ist Dr. Müller", "termine")

    assert [f.text for f in gedaechtnis.suchen("Wie trinke ich meinen Kaffee?")] == [
        "Louis trinkt Kaffee ohne Zucker"
    ]
    assert [f.text for f in gedaechtnis.suchen("Wer ist mein Zahnarzt? Mueller?")] == [
        "Der Zahnarzt ist Dr. Müller"
    ]
    assert gedaechtnis.suchen("Wetter morgen") == []
    assert gedaechtnis.suchen("") == []


def test_suche_hoechstens_fuenf(gedaechtnis):
    for nummer in range(8):
        gedaechtnis.hinzufuegen(f"Projekt Nummer {nummer} läuft gut", "projekte")
    assert len(gedaechtnis.suchen("Wie läuft das Projekt?")) == 5


def test_doppelte_fakten_werden_nicht_neu_angelegt(gedaechtnis):
    gedaechtnis.hinzufuegen("Louis mag Pilze", "vorlieben")
    fakt, neu = gedaechtnis.hinzufuegen("  louis  mag pilze ", "vorlieben")
    assert not neu and fakt.id == 1
    assert len(gedaechtnis.alle()) == 1


def test_ungueltige_kategorie(gedaechtnis):
    with pytest.raises(WerkzeugFehler, match="Erlaubt: termine, vorlieben, projekte, sonstiges"):
        gedaechtnis.hinzufuegen("X", "geheim")


def test_entfernen_aktualisiert_volltextindex(gedaechtnis):
    fakt, _ = gedaechtnis.hinzufuegen("Louis spielt Gitarre", "vorlieben")
    gedaechtnis.entfernen(fakt.id)
    assert gedaechtnis.suchen("Gitarre") == []
    assert gedaechtnis.holen(fakt.id) is None


def test_kontext_block(gedaechtnis):
    assert gedaechtnis.kontext("Kaffee?") is None
    gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    block = gedaechtnis.kontext("Wie trinke ich meinen Kaffee?")
    assert block.startswith("## Was du über Louis weißt")
    assert "- Louis trinkt Kaffee ohne Zucker [ID 1, vorlieben, gemerkt am " in block


# ---------------------------------------------------------------------- Werkzeuge


def test_merken_und_erinnern(werkzeuge_kasten, gedaechtnis):
    ergebnis = ausfuehren(
        werkzeuge_kasten, "merken", fakt="Louis trinkt Kaffee ohne Zucker", kategorie="vorlieben"
    )
    assert ergebnis.startswith("Gemerkt:\n- Louis trinkt Kaffee ohne Zucker [ID 1, vorlieben")
    assert ausfuehren(
        werkzeuge_kasten, "merken", fakt="Louis trinkt Kaffee ohne Zucker", kategorie="vorlieben"
    ).startswith("Das weißt du schon")
    assert "Kaffee ohne Zucker" in ausfuehren(werkzeuge_kasten, "erinnern", suche="Kaffee")
    assert ausfuehren(werkzeuge_kasten, "erinnern", suche="Hund") == (
        "Zu „Hund“ ist nichts gespeichert."
    )


def test_vergessen_erst_nach_ja(werkzeuge_kasten, gedaechtnis):
    gedaechtnis.hinzufuegen("Louis mag keine Pilze", "vorlieben")
    vorschau = ausfuehren(werkzeuge_kasten, "vergessen", "a1", fakt_id=1)
    assert vorschau.startswith("NOCH NICHT GELÖSCHT")
    assert "fakt_id=1 und bestaetigt=true" in vorschau
    sofort = ausfuehren(werkzeuge_kasten, "vergessen", "a1", fakt_id=1, bestaetigt=True)
    assert sofort.startswith("NICHT GELÖSCHT")
    assert gedaechtnis.holen(1) is not None

    nach_ja = ausfuehren(werkzeuge_kasten, "vergessen", "a2", fakt_id="1", bestaetigt=True)
    assert nach_ja.startswith("Vergessen:\n- Louis mag keine Pilze")
    assert gedaechtnis.holen(1) is None


def test_vergessen_unbekannte_id(werkzeuge_kasten):
    assert ausfuehren(werkzeuge_kasten, "vergessen", fakt_id=99).startswith(
        "Fehler: Keinen Fakt mit der ID 99"
    )
    assert "keine Zahl" in ausfuehren(werkzeuge_kasten, "vergessen", fakt_id="Pilze")


def test_kontextvariable_bleibt_sauber(werkzeuge_kasten):
    ausfuehren(werkzeuge_kasten, "erinnern", "x", suche="Test")
    assert aktuelle_anfrage.get() == ""


# ---------------------------------------------------------------------- über die API


def test_kaffee_ueber_zwei_sitzungen(client, llama: FakeLlama):
    """Fertig-Kriterien: Merken, dann in einer neuen Sitzung richtig antworten."""
    llama.antworten = [
        werkzeug_aufruf(
            "merken", {"fakt": "Louis trinkt Kaffee ohne Zucker", "kategorie": "vorlieben"}
        ),
        text("Alles klar, ohne Zucker!"),
        text("Ohne Zucker."),
    ]
    client.post(
        "/v1/chat/completions",
        headers={"X-Missa-Session": "handy"},
        json={
            "messages": [
                {"role": "user", "content": "Merk dir, dass ich Kaffee ohne Zucker trinke"}
            ]
        },
    )
    assert llama.anfragen[1]["messages"][-1]["content"].startswith("Gemerkt:")

    client.post(
        "/v1/chat/completions",
        headers={"X-Missa-Session": "laptop"},
        json={"messages": [{"role": "user", "content": "Wie trinke ich meinen Kaffee?"}]},
    )
    neue_sitzung = llama.anfragen[2]
    assert len(neue_sitzung["messages"]) == 2  # kein Verlauf aus der anderen Sitzung
    assert "## Was du über Louis weißt" in neue_sitzung["messages"][0]["content"]
    assert "Louis trinkt Kaffee ohne Zucker" in neue_sitzung["messages"][0]["content"]


def test_kein_gedaechtnis_block_ohne_treffer(client, llama: FakeLlama):
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hallo"}]})
    assert "Was du über Louis weißt" not in llama.system()


def test_api_gedaechtnis_listet_alle_fakten(client):
    gedaechtnis = client.app.state.gedaechtnis
    gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    gedaechtnis.hinzufuegen("Missa läuft auf ai01", "projekte")

    antwort = client.get("/api/gedaechtnis")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["anzahl"] == 2
    assert [f["text"] for f in daten["fakten"]] == [
        "Louis trinkt Kaffee ohne Zucker",
        "Missa läuft auf ai01",
    ]
    assert set(daten["fakten"][0]) == {"id", "text", "kategorie", "erstellt"}

    nur = client.get("/api/gedaechtnis", params={"kategorie": "projekte"}).json()
    assert [f["text"] for f in nur["fakten"]] == ["Missa läuft auf ai01"]
    assert client.get("/api/gedaechtnis", params={"kategorie": "x"}).status_code == 400


def test_api_gedaechtnis_braucht_token(client):
    client.headers.pop("Authorization")
    assert client.get("/api/gedaechtnis").status_code == 401


def test_scharfes_s_wird_gefunden(gedaechtnis):
    gedaechtnis.hinzufuegen("Die Katze heißt Mimi", "sonstiges")
    assert [f.text for f in gedaechtnis.suchen("Wie heißt die Katze?")] == ["Die Katze heißt Mimi"]
