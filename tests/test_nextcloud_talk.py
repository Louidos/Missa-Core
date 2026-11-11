"""Nextcloud-Talk-Kanal mit gemocktem Nextcloud und gemocktem llama-server."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.channels.nextcloud_talk import (
    MAX_LAENGE,
    WEBHOOK_PFAD,
    aufteilen,
    signatur,
    text_aus_nachricht,
    url_normalisieren,
)
from missa.core.app import create_app
from missa.core.werkzeuge import Werkzeugkasten
from tests.conftest import JETZT, FakeLlama, text

SECRET = "geheimes-bot-secret-" + "x" * 44
BASIS = "https://cloud.louis-degiorgi.ch:8443"
TOKEN = "missa123"


@dataclass
class FakeNextcloud:
    """Bot-API von Talk: prüft Signaturen und merkt sich alle Aufrufe."""

    anfragen: list[dict[str, Any]] = field(default_factory=list)
    fehler_bei_reaktion: bool = False

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self._bearbeiten)

    def _bearbeiten(self, request: httpx.Request) -> httpx.Response:
        daten = json.loads(request.content or b"{}")
        signiert = daten.get("message") if "/message" in request.url.path else daten.get("reaction")
        random = request.headers.get("X-Nextcloud-Talk-Bot-Random", "")
        gueltig = len(random) >= 32 and request.headers.get(
            "X-Nextcloud-Talk-Bot-Signature"
        ) == signatur(SECRET, random, signiert or "")
        self.anfragen.append(
            {
                "methode": request.method,
                "pfad": request.url.path,
                "daten": daten,
                "signatur_ok": gueltig,
                "ocs": request.headers.get("OCS-APIRequest"),
            }
        )
        if not gueltig:
            return httpx.Response(401)
        if "/reaction/" in request.url.path:
            if self.fehler_bei_reaktion:
                return httpx.Response(500)
            return httpx.Response(201 if request.method == "POST" else 200)
        return httpx.Response(201, json={"ocs": {"data": {}}})

    def nachrichten(self) -> list[dict[str, Any]]:
        return [a for a in self.anfragen if a["pfad"].endswith("/message")]

    def reaktionen(self) -> list[tuple[str, str]]:
        return [
            (a["methode"], a["daten"]["reaction"])
            for a in self.anfragen
            if "/reaction/" in a["pfad"]
        ]


def ereignis(
    inhalt: str = "Hallo Missa!",
    akteur: str = "users/louis",
    akteur_typ: str = "Person",
    name: str = "Louis",
    nachricht_id: str = "1567",
    typ: str = "Create",
) -> dict[str, Any]:
    return {
        "type": typ,
        "actor": {"type": akteur_typ, "id": akteur, "name": name},
        "object": {
            "type": "Note",
            "id": nachricht_id,
            "name": "message",
            "content": json.dumps({"message": inhalt, "parameters": {}}),
            "mediaType": "text/markdown",
        },
        "target": {"type": "Collection", "id": TOKEN, "name": "Missa"},
    }


def schicken(
    client: TestClient,
    daten: dict[str, Any],
    secret: str = SECRET,
    backend: str = BASIS + "/",
    random: str | None = None,
) -> httpx.Response:
    body = json.dumps(daten).encode()
    random = random or secrets.token_urlsafe(48)[:64]
    return client.post(
        WEBHOOK_PFAD,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Nextcloud-Talk-Random": random,
            "X-Nextcloud-Talk-Signature": signatur(secret, random, body),
            "X-Nextcloud-Talk-Backend": backend,
        },
    )


@pytest.fixture
def nextcloud() -> FakeNextcloud:
    return FakeNextcloud()


@pytest.fixture
def talk_einstellungen(einstellungen):
    return replace(
        einstellungen,
        nc_talk_enabled=True,
        nc_talk_base_url=BASIS,
        nc_talk_bot_secret=SECRET,
        nc_talk_allowed_users=("louis", "admin"),
    )


def talk_app(einstellungen, llama: FakeLlama, nextcloud: FakeNextcloud) -> TestClient:
    app = create_app(
        einstellungen,
        werkzeuge=Werkzeugkasten(),
        llama_transport=llama.transport,
        talk_transport=nextcloud.transport,
        uhr=lambda: JETZT,
    )
    return TestClient(app)


@pytest.fixture
def talk(talk_einstellungen, llama, nextcloud):
    with talk_app(talk_einstellungen, llama, nextcloud) as client:
        yield client


def gespeicherte_nachrichten(einstellungen) -> int:
    with sqlite3.connect(einstellungen.datenbank) as db:
        return db.execute("SELECT COUNT(*) FROM nachrichten").fetchone()[0]


# ---------------------------------------------------------------------- Signatur und Backend


def test_falsche_signatur_gibt_401(talk, llama, nextcloud):
    antwort = schicken(talk, ereignis(), secret="falsches-secret-" + "y" * 40)
    assert antwort.status_code == 401
    assert llama.anfragen == []
    assert nextcloud.anfragen == []


def test_veraenderter_body_gibt_401(talk, llama):
    body = json.dumps(ereignis()).encode()
    random = "r" * 64
    antwort = talk.post(
        WEBHOOK_PFAD,
        content=body.replace(b"Hallo", b"Hallx"),
        headers={
            "X-Nextcloud-Talk-Random": random,
            "X-Nextcloud-Talk-Signature": signatur(SECRET, random, body),
            "X-Nextcloud-Talk-Backend": BASIS + "/",
        },
    )
    assert antwort.status_code == 401
    assert llama.anfragen == []


@pytest.mark.parametrize("fehlt", ["X-Nextcloud-Talk-Random", "X-Nextcloud-Talk-Signature"])
def test_fehlende_kopfzeilen_geben_401(talk, fehlt):
    body = json.dumps(ereignis()).encode()
    kopf = {
        "X-Nextcloud-Talk-Random": "r" * 64,
        "X-Nextcloud-Talk-Signature": signatur(SECRET, "r" * 64, body),
        "X-Nextcloud-Talk-Backend": BASIS + "/",
    }
    del kopf[fehlt]
    assert talk.post(WEBHOOK_PFAD, content=body, headers=kopf).status_code == 401


def test_zu_kurzes_random_gibt_401(talk):
    assert schicken(talk, ereignis(), random="kurz").status_code == 401


def test_falsches_backend_gibt_401(talk, llama):
    antwort = schicken(talk, ereignis(), backend="https://boese.example.com/")
    assert antwort.status_code == 401
    assert llama.anfragen == []


@pytest.mark.parametrize(
    "backend", [BASIS, BASIS + "/", "HTTPS://Cloud.Louis-Degiorgi.ch:8443/", BASIS + "//"]
)
def test_backend_vergleich_toleriert_schreibweise(talk, backend):
    assert schicken(talk, ereignis(), backend=backend).status_code == 200


def test_url_normalisieren():
    assert url_normalisieren("https://Cloud.Example.ch:443/") == "https://cloud.example.ch"
    assert url_normalisieren("http://10.0.30.10:80") == "http://10.0.30.10"
    assert url_normalisieren("https://x.ch:8443/nc/") == "https://x.ch:8443/nc"


# ---------------------------------------------------------------------- erlaubte Absender


def test_louis_bekommt_antwort_mit_reaktion_und_reply(talk, llama, nextcloud, talk_einstellungen):
    llama.antworten = [text("Na klar! Hallo Louis 👋")]
    antwort = schicken(talk, ereignis("Hallo Missa!"))
    assert antwort.status_code == 200

    # Reihenfolge: 👀 setzen, antworten, 👀 entfernen – alles korrekt signiert.
    assert [(a["methode"], a["pfad"]) for a in nextcloud.anfragen] == [
        ("POST", f"/ocs/v2.php/apps/spreed/api/v1/bot/{TOKEN}/reaction/1567"),
        ("POST", f"/ocs/v2.php/apps/spreed/api/v1/bot/{TOKEN}/message"),
        ("DELETE", f"/ocs/v2.php/apps/spreed/api/v1/bot/{TOKEN}/reaction/1567"),
    ]
    assert all(a["signatur_ok"] and a["ocs"] == "true" for a in nextcloud.anfragen)
    assert nextcloud.reaktionen() == [("POST", "👀"), ("DELETE", "👀")]
    gesendet = nextcloud.nachrichten()[0]["daten"]
    assert gesendet["message"] == "Na klar! Hallo Louis 👋"
    assert gesendet["replyTo"] == 1567
    assert len(gesendet["referenceId"]) == 64

    # Dieselbe Pipeline: Charakter vorne, Hinweis auf Kanal und Absender angehängt.
    system = llama.system()
    assert system.startswith("Du bist Missa.")
    assert "Kanal: Nextcloud Talk" in system
    assert "Nextcloud-Benutzer „louis“" in system
    assert llama.anfragen[0]["messages"][-1] == {"role": "user", "content": "Hallo Missa!"}


def test_antwortet_sofort_und_arbeitet_im_hintergrund(talk, llama, nextcloud):
    """Talk wartet nur 5 Sekunden: Der Webhook plant die Arbeit nur ein und antwortet sofort."""
    from fastapi import BackgroundTasks, Request

    body = json.dumps(ereignis()).encode()
    random = "r" * 64
    scope = {
        "type": "http",
        "method": "POST",
        "path": WEBHOOK_PFAD,
        "headers": [
            (b"x-nextcloud-talk-random", random.encode()),
            (b"x-nextcloud-talk-signature", signatur(SECRET, random, body).encode()),
            (b"x-nextcloud-talk-backend", BASIS.encode()),
        ],
    }

    async def empfangen():
        return {"type": "http.request", "body": body, "more_body": False}

    aufgaben = BackgroundTasks()
    antwort = asyncio.run(talk.app.state.talk.webhook(Request(scope, empfangen), aufgaben))
    assert antwort.status_code == 200
    assert len(aufgaben.tasks) == 1  # Verarbeitung ist eingeplant …
    assert llama.anfragen == []  # … aber das Modell wurde noch nicht gefragt
    assert nextcloud.anfragen == []


def test_admin_ist_ebenfalls_erlaubt(talk, llama, nextcloud):
    schicken(talk, ereignis(akteur="users/admin", name="Administrator"))
    assert len(llama.anfragen) == 1
    assert len(nextcloud.nachrichten()) == 1


def test_gespraechsverlauf_pro_unterhaltung(talk, llama, talk_einstellungen):
    llama.antworten = [text("Freut mich!"), text("Bello.")]
    schicken(talk, ereignis("Mein Hund heißt Bello.", nachricht_id="1"))
    schicken(talk, ereignis("Wie heißt mein Hund?", nachricht_id="2"))
    inhalte = [n["content"] for n in llama.anfragen[1]["messages"][1:]]
    assert inhalte == ["Mein Hund heißt Bello.", "Freut mich!", "Wie heißt mein Hund?"]


def test_doppelte_zustellung_wird_nur_einmal_beantwortet(talk, llama):
    schicken(talk, ereignis(nachricht_id="77"))
    schicken(talk, ereignis(nachricht_id="77"))
    assert len(llama.anfragen) == 1


def test_erwaehnungen_werden_lesbar():
    inhalt = json.dumps(
        {
            "message": "Hey {mention-user1}, schau {file}",
            "parameters": {
                "mention-user1": {"type": "user", "id": "louis", "name": "Louis"},
                "file": {"type": "file", "name": "plan.pdf"},
            },
        }
    )
    assert text_aus_nachricht(inhalt) == "Hey @Louis, schau plan.pdf"


# ---------------------------------------------------------------------- ignorierte Absender


def test_anderer_account_wird_komplett_ignoriert(
    talk, llama, nextcloud, talk_einstellungen, caplog
):
    with caplog.at_level(logging.INFO, logger="missa.channels.nextcloud_talk"):
        antwort = schicken(talk, ereignis("Streng geheim: Merk dir das!", akteur="users/eve"))
    assert antwort.status_code == 200
    assert nextcloud.anfragen == []  # keine Antwort, keine Reaktion
    assert llama.anfragen == []  # kein Modellaufruf, keine Werkzeuge
    assert gespeicherte_nachrichten(talk_einstellungen) == 0  # kein Verlauf
    assert talk.app.state.gedaechtnis.alle() == []  # kein Gedächtnis
    eintraege = [r.getMessage() for r in caplog.records if "ignoriert" in r.getMessage()]
    assert len(eintraege) == 1
    assert "users/eve" in eintraege[0] and TOKEN in eintraege[0]
    assert "geheim" not in caplog.text  # Inhalt wird nie geloggt


@pytest.mark.parametrize(
    ("akteur", "akteur_typ", "name"),
    [
        ("guests/louis", "Person", "louis"),
        ("guests/4f3a9b", "Person", "Louis"),
        ("emails/louis@example.ch", "Person", "Louis"),
        ("federated_users/louis@cloud.anders.ch", "Person", "Louis"),
        ("federated_users/admin", "Person", "admin"),
        ("bots/bot-a78f46c5c203141b247554e180e1aa3553d282c6", "Application", "louis"),
        ("users/louis", "Application", "Louis"),
        ("users/mallory", "Person", "louis"),
        ("users/Louis", "Person", "Louis"),
        ("users/louis2", "Person", "Louis"),
        ("users/", "Person", "Louis"),
        ("louis", "Person", "Louis"),
    ],
)
def test_gaeste_foederierte_und_aehnliche_namen_werden_ignoriert(
    talk, llama, nextcloud, akteur, akteur_typ, name
):
    antwort = schicken(talk, ereignis(akteur=akteur, akteur_typ=akteur_typ, name=name))
    assert antwort.status_code == 200
    assert llama.anfragen == []
    assert nextcloud.anfragen == []


@pytest.mark.parametrize("liste", [(), ("",)])
def test_leere_erlaubnisliste_ignoriert_alles(talk_einstellungen, llama, nextcloud, caplog, liste):
    leer = replace(talk_einstellungen, nc_talk_allowed_users=tuple(x for x in liste if x))
    with caplog.at_level(logging.WARNING), talk_app(leer, llama, nextcloud) as client:
        assert schicken(client, ereignis()).status_code == 200
        assert schicken(client, ereignis(akteur="users/admin", nachricht_id="2")).status_code == 200
    assert llama.anfragen == []
    assert nextcloud.anfragen == []
    assert "NC_TALK_ALLOWED_USERS ist leer" in caplog.text


def test_fehlende_erlaubnisliste_aus_umgebung_ist_leer():
    from missa.core.config import Einstellungen

    assert Einstellungen.aus_umgebung({"NC_TALK_ENABLED": "true"}).nc_talk_allowed_users == ()
    assert Einstellungen.aus_umgebung(
        {"NC_TALK_ALLOWED_USERS": " louis , admin,,louis "}
    ).nc_talk_allowed_users == ("louis", "admin")


@pytest.mark.parametrize(
    "daten",
    [
        ereignis(typ="Like"),
        ereignis(typ="Activity"),
        {**ereignis(), "object": {**ereignis()["object"], "name": "call_started"}},
        {
            "type": "Join",
            "actor": {"type": "Application", "id": "bots/bot-123", "name": "Missa"},
            "object": {"type": "Collection", "id": TOKEN, "name": "Missa"},
        },
        {
            "type": "Leave",
            "actor": {"type": "Application", "id": "bots/bot-123", "name": "Missa"},
            "object": {"type": "Collection", "id": TOKEN, "name": "Missa"},
        },
        ereignis(inhalt="   "),
    ],
)
def test_nur_textnachrichten_werden_verarbeitet(talk, llama, nextcloud, daten):
    assert schicken(talk, daten).status_code == 200
    assert llama.anfragen == []
    assert nextcloud.anfragen == []


def test_kein_json_gibt_400(talk):
    random = "r" * 64
    antwort = talk.post(
        WEBHOOK_PFAD,
        content=b"kein json",
        headers={
            "X-Nextcloud-Talk-Random": random,
            "X-Nextcloud-Talk-Signature": signatur(SECRET, random, b"kein json"),
            "X-Nextcloud-Talk-Backend": BASIS,
        },
    )
    assert antwort.status_code == 400


# ---------------------------------------------------------------------- lange Antworten und Fehler


def test_lange_antwort_kommt_vollstaendig_in_mehreren_nachrichten(talk, llama, nextcloud):
    absatz = "Missa erklärt ausführlich. " * 40
    lang = "\n\n".join(f"Absatz {n}: {absatz}" for n in range(80))
    assert len(lang) > 2 * MAX_LAENGE
    llama.antworten = [text(lang)]
    schicken(talk, ereignis("Erzähl mir alles"))

    nachrichten = [n["daten"] for n in nextcloud.nachrichten()]
    assert len(nachrichten) >= 3
    assert all(len(n["message"]) <= MAX_LAENGE for n in nachrichten)
    assert all(n["replyTo"] == 1567 for n in nachrichten)
    anzahl = len(nachrichten)
    zusammen = []
    for nummer, n in enumerate(nachrichten, 1):
        marke = f"\n\n*(Teil {nummer}/{anzahl})*"
        assert n["message"].endswith(marke)
        zusammen.append(n["message"].removesuffix(marke))
    assert " ".join(" ".join(zusammen).split()) == " ".join(lang.split())


def test_aufteilen():
    assert aufteilen("kurz") == ["kurz"]
    teile = aufteilen("a" * 250, grenze=100)
    assert all(len(t) <= 100 for t in teile)
    assert "".join(t.split("\n\n*(Teil")[0] for t in teile) == "a" * 250


def test_modell_nicht_erreichbar_gibt_freundliche_meldung(talk, llama, nextcloud):
    llama.erreichbar = False
    assert schicken(talk, ereignis()).status_code == 200
    gesendet = nextcloud.nachrichten()
    assert len(gesendet) == 1
    assert "Sprachmodell" in gesendet[0]["daten"]["message"]
    assert nextcloud.reaktionen() == [("POST", "👀"), ("DELETE", "👀")]


def test_zeitlimit_gibt_freundliche_meldung(talk_einstellungen, nextcloud):
    async def langsam(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        await asyncio.sleep(5)
        return httpx.Response(200, json={"choices": [{"message": text("zu spät")}]})

    llama_langsam = FakeLlama()
    llama_langsam.transport = httpx.MockTransport(langsam)
    einstellungen = replace(talk_einstellungen, nc_talk_timeout=0.2)
    with talk_app(einstellungen, llama_langsam, nextcloud) as client:
        assert schicken(client, ereignis()).status_code == 200
    gesendet = nextcloud.nachrichten()
    assert len(gesendet) == 1
    assert "nach 0.2 Sekunden habe ich aufgegeben" in gesendet[0]["daten"]["message"]
    assert nextcloud.reaktionen()[-1] == ("DELETE", "👀")


def test_reaktion_fehlgeschlagen_antwort_kommt_trotzdem(talk, llama, nextcloud):
    nextcloud.fehler_bei_reaktion = True
    schicken(talk, ereignis())
    assert len(nextcloud.nachrichten()) == 1


# ---------------------------------------------------------------------- ausgeschaltet


def test_ohne_talk_einstellungen_startet_missa_normal(client):
    assert client.get("/health").status_code == 200
    assert client.post(WEBHOOK_PFAD, content=b"{}").status_code == 404
    assert client.app.state.talk is None


@pytest.mark.parametrize(
    "aenderung",
    [
        {"nc_talk_enabled": False},
        {"nc_talk_bot_secret": ""},
        {"nc_talk_base_url": ""},
    ],
)
def test_talk_aus_oder_unvollstaendig_kein_endpunkt(
    talk_einstellungen, llama, nextcloud, aenderung
):
    with talk_app(replace(talk_einstellungen, **aenderung), llama, nextcloud) as client:
        assert client.get("/health").status_code == 200
        assert schicken(client, ereignis()).status_code == 404
    assert llama.anfragen == []
    assert nextcloud.anfragen == []


def test_talk_einstellungen_aus_umgebung():
    from missa.core.config import Einstellungen

    e = Einstellungen.aus_umgebung(
        {
            "NC_TALK_ENABLED": "true",
            "NC_TALK_BASE_URL": "https://cloud.louis-degiorgi.ch:8443/",
            "NC_TALK_BOT_SECRET": " abc ",
            "NC_TALK_ALLOWED_USERS": "louis,admin",
            "NC_TALK_TIMEOUT": "120",
        }
    )
    assert e.talk_aktiv
    assert e.nc_talk_base_url == "https://cloud.louis-degiorgi.ch:8443"
    assert e.talk_api_url == e.nc_talk_base_url
    assert e.nc_talk_bot_secret == "abc"
    assert e.nc_talk_timeout == 120.0
    assert not Einstellungen.aus_umgebung({}).talk_aktiv
    assert not Einstellungen.aus_umgebung({"NC_TALK_ENABLED": "nein"}).nc_talk_enabled
    intern = Einstellungen.aus_umgebung({"NC_TALK_API_URL": "http://10.0.30.10/"})
    assert intern.talk_api_url == "http://10.0.30.10"


# ---------------------------------------------------------------------- Rechte-Schicht


def test_talk_notfall_stopp_und_protokoll_mit_account(talk, llama, talk_einstellungen):
    llama.antworten = [text("Alles gesperrt.")]
    schicken(talk, ereignis("Notfall-Stopp!", nachricht_id="2001"))
    assert talk_einstellungen.notfall_datei.exists()
    info = talk.app.state.rechte.notfall.info()
    assert info["quelle"] == "talk/louis"
    [eintrag] = talk.app.state.protokoll.liste(status="notfall_stopp")
    assert (eintrag.kanal, eintrag.account) == ("talk", "louis")
