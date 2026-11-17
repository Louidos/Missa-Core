"""Endpunkte, Anmeldung und Fehlerformat."""

from __future__ import annotations

import pytest

from tests.conftest import FakeLlama


def test_health_ohne_anmeldung(client):
    client.headers.pop("Authorization")
    antwort = client.get("/health")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["status"] == "ok"
    assert daten["llama"] == "ok"
    assert daten["charakter"] == "ok"


def test_health_meldet_ausgefallenen_llama_server(client, llama: FakeLlama):
    llama.erreichbar = False
    antwort = client.get("/health")
    assert antwort.status_code == 503
    assert antwort.json()["llama"] == "nicht erreichbar"


def test_health_meldet_fehlende_charakterdatei(client, charakter_datei):
    charakter_datei.unlink()
    assert client.get("/health").json()["charakter"] == "fehlt"


@pytest.mark.parametrize(
    ("methode", "pfad"),
    [("get", "/v1/models"), ("post", "/v1/chat/completions"), ("get", "/v1/models/missa")],
)
@pytest.mark.parametrize("kopf", [None, "Bearer falsch", "Basic dGVzdDp0ZXN0", "Bearer "])
def test_ohne_gueltigen_token_kommt_401(client, methode, pfad, kopf):
    client.headers.pop("Authorization")
    headers = {"Authorization": kopf} if kopf else {}
    json = {"messages": [{"role": "user", "content": "Hallo"}]}
    antwort = client.request(methode, pfad, headers=headers, json=json)
    assert antwort.status_code == 401
    assert antwort.headers["WWW-Authenticate"] == "Bearer"
    assert antwort.json()["error"]["type"] == "authentication_error"


def test_models_liefert_missa(client):
    antwort = client.get("/v1/models")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["object"] == "list"
    assert [m["id"] for m in daten["data"]] == ["missa"]


def test_einzelnes_modell(client):
    assert client.get("/v1/models/missa").json()["id"] == "missa"
    assert client.get("/v1/models/gpt-4").status_code == 404


def test_leere_nachrichtenliste_ist_400(client):
    antwort = client.post("/v1/chat/completions", json={"messages": []})
    assert antwort.status_code == 400
    assert antwort.json()["error"]["type"] == "invalid_request_error"


def test_nur_system_nachricht_ist_400(client):
    antwort = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "system", "content": "x"}]}
    )
    assert antwort.status_code == 400


def test_zu_lange_sitzung_ist_400(client):
    antwort = client.post(
        "/v1/chat/completions",
        headers={"X-Missa-Session": "x" * 201},
        json={"messages": [{"role": "user", "content": "Hallo"}]},
    )
    assert antwort.status_code == 400


def test_llama_nicht_erreichbar_gibt_502(client, llama: FakeLlama):
    llama.erreichbar = False
    antwort = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hallo"}]}
    )
    assert antwort.status_code == 502
    assert antwort.json()["error"]["type"] == "llama_fehler"


def test_llama_api_key_wird_mitgeschickt(client, llama: FakeLlama, einstellungen):
    einstellungen.llama_api_key_file.write_text("llama-geheim\n", encoding="utf-8")
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert llama.kopfzeilen[-1]["Authorization"] == "Bearer llama-geheim"


def test_ohne_llama_key_datei_kein_authorization_kopf(client, llama: FakeLlama):
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert "Authorization" not in llama.kopfzeilen[-1]


@pytest.mark.parametrize("pfad", ["/api/initiative/journal", "/api/initiative"])
def test_initiative_journal_ohne_api_key_gesperrt_und_nur_lesend(client, einstellungen, pfad):
    import sqlite3

    def inhalt() -> list[str]:
        db = sqlite3.connect(einstellungen.datenbank)
        try:
            return list(db.iterdump())
        finally:
            db.close()

    vorher = inhalt()
    assert client.get(pfad).status_code == 200
    client.headers.pop("Authorization")
    assert client.get(pfad).status_code == 401
    assert inhalt() == vorher


def test_im_journal_stehen_keine_passwoerter(client, einstellungen, llama: FakeLlama):
    import asyncio
    import json

    from tests.conftest import text

    initiative = client.app.state.initiative
    initiative.vorgaben.setzen("initiative.aktiv", "ja")
    initiative.lagebild.registrieren(
        "Notiz", lambda: f"api_key={einstellungen.api_key} passwort=hunter22"
    )
    llama.antworten = [
        text(
            json.dumps(
                {
                    "aktion": "notieren",
                    "schluessel": "zugang",
                    "titel": f"Token: {einstellungen.api_key}",
                    "text": "Bearer abcdefghijkl123",
                    "grund": "passwort=hunter22",
                }
            )
        )
    ]

    async def laufen():
        return await initiative.lauf()

    asyncio.run(laufen())
    roh = client.get("/api/initiative/journal").text
    for geheim in (einstellungen.api_key, "hunter22", "abcdefghijkl123"):
        assert geheim not in roh
    assert "***" in roh
