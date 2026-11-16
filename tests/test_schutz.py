"""Anmeldung vor dem Lesen, Grössengrenze und Sperre nach Fehlversuchen."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from missa.core.schutz import MAX_FEHLVERSUCHE, SPERRE_SEKUNDEN, Schutz
from tests.conftest import API_KEY

GESCHUETZT = [
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/api/gedaechtnis"),
    ("GET", "/api/aktionen"),
    ("GET", "/api/anmeldungen"),
    ("POST", "/api/kontakte/kontakt"),
    ("GET", "/gibt-es-nicht"),
]


@pytest.mark.parametrize(("methode", "pfad"), GESCHUETZT)
def test_ohne_token_wird_der_inhalt_nicht_gelesen(client, methode, pfad):
    client.headers.pop("Authorization")
    # Kaputtes JSON: ohne Anmeldung muss trotzdem 401 kommen, nicht 400 (= Body gelesen).
    antwort = client.request(methode, pfad, content=b"{kaputt")
    assert antwort.status_code == 401
    assert antwort.json()["error"]["type"] == "authentication_error"
    antwort = client.request(methode, pfad, headers={"Authorization": "Bearer falsch"})
    assert antwort.status_code == 401


@pytest.mark.parametrize("pfad", ["/docs", "/redoc", "/openapi.json"])
def test_keine_oeffentliche_api_beschreibung(client, pfad):
    assert client.get(pfad).status_code == 404
    client.headers.pop("Authorization")
    assert client.get(pfad).status_code == 401


def test_health_bleibt_offen(client):
    client.headers.pop("Authorization")
    assert client.get("/health").status_code == 200


def test_zu_grosse_anfrage_wird_abgelehnt(client):
    antwort = client.post(
        "/v1/chat/completions",
        content=b"x" * 21_000_000,
        headers={"Content-Type": "application/json"},
    )
    assert antwort.status_code == 413


def _mini_app(uhr) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        return {"laenge": len(await request.body())}

    app.add_middleware(Schutz, api_key=API_KEY, max_bytes=1000, uhr=uhr)
    return app


def test_grenze_gilt_auch_ohne_content_length():
    client = TestClient(_mini_app(lambda: 0.0))
    kopf = {"Authorization": f"Bearer {API_KEY}"}

    def stuecke(anzahl: int):
        for _ in range(anzahl):
            yield b"y" * 100

    assert client.post("/echo", content=stuecke(5), headers=kopf).json() == {"laenge": 500}
    assert client.post("/echo", content=stuecke(20), headers=kopf).status_code == 413


def test_sperre_nach_zu_vielen_fehlversuchen():
    zeit = [0.0]
    client = TestClient(_mini_app(lambda: zeit[0]))
    for _ in range(MAX_FEHLVERSUCHE):
        assert client.post("/echo", headers={"Authorization": "Bearer falsch"}).status_code == 401
    # Jetzt gesperrt – auch mit richtigem Schlüssel.
    gesperrt = client.post("/echo", headers={"Authorization": f"Bearer {API_KEY}"})
    assert gesperrt.status_code == 429
    zeit[0] += SPERRE_SEKUNDEN + 1
    assert client.post("/echo", headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 200
