"""Geräte-Anschluss v1: Token, Anmeldung, Stream, Aufträge, Ergebnisse (mit Fake-Gerät)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.config import Einstellungen
from missa.core.geraete import (
    MAX_ERGEBNIS,
    Geraete,
    GeraeteFehler,
    GeraeteTokens,
    GeraetNichtErreichbar,
    GeraetZeitlimit,
    bearer_token,
    token_hash,
)
from missa.core.schutz import MAX_FEHLVERSUCHE
from tests.conftest import API_KEY, JETZT, FakeLlama
from tests.geraet_fake import SCHWEIGEN, FakeGeraet, laufender_server, warten, werkzeug

ZONE = JETZT.tzinfo
PROJEKT = Path(__file__).resolve().parents[1]
SCRIPT = PROJEKT / "scripts" / "einrichten" / "geraet-anlegen.sh"


@pytest.fixture
def mit_geraeten(einstellungen: Einstellungen) -> Einstellungen:
    return dataclasses.replace(einstellungen, geraete_aktiv=True, geraete_frist=5)


@pytest.fixture
def token(mit_geraeten: Einstellungen) -> str:
    return GeraeteTokens(mit_geraeten.datenbank).anlegen("laptop")


@pytest.fixture
def app_client(mit_geraeten, llama: FakeLlama, token):
    app = create_app(mit_geraeten, llama_transport=llama.transport, uhr=lambda: JETZT)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        yield client


def anmeldung(**zusatz: Any) -> dict[str, Any]:
    return {
        "protokoll": 1,
        "geraet": "laptop",
        "sitzungen": ["laptop-chat"],
        "werkzeuge": [werkzeug("rechner_status")],
        **zusatz,
    }


# ----------------------------------------------------------------------------- Tokens


def test_token_wird_nur_als_hash_gespeichert(tmp_path):
    tokens = GeraeteTokens(tmp_path / "missa.db")
    token = tokens.anlegen("laptop")
    assert token.startswith("mg_") and len(token) > 40
    with sqlite3.connect(tmp_path / "missa.db") as db:
        inhalt = db.execute("SELECT name, token_hash FROM geraete").fetchall()
    assert inhalt == [("laptop", token_hash(token))]
    assert token not in str(inhalt)
    assert tokens.pruefen(token) == "laptop"
    assert tokens.pruefen(token.encode()) == "laptop"
    assert tokens.pruefen("falsch") is None
    assert tokens.pruefen(None) is None


def test_gesperrter_token_gilt_nicht_mehr_und_neu_anlegen_geht(tmp_path):
    tokens = GeraeteTokens(tmp_path / "missa.db")
    alt = tokens.anlegen("laptop")
    with pytest.raises(GeraeteFehler, match="gibt es schon"):
        tokens.anlegen("laptop")
    assert tokens.sperren("laptop")
    assert not tokens.sperren("laptop")
    assert tokens.pruefen(alt) is None
    assert not tokens.aktiv("laptop")
    neu = tokens.anlegen("laptop")
    assert tokens.pruefen(neu) == "laptop"
    assert tokens.pruefen(alt) is None


@pytest.mark.parametrize("name", ["", "Laptop", "mit leer", "a" * 33, "-x", "ü"])
def test_ungueltige_geraetenamen(tmp_path, name):
    with pytest.raises(GeraeteFehler):
        GeraeteTokens(tmp_path / "missa.db").anlegen(name)


def test_bearer_token_lesen():
    assert bearer_token("Bearer abc") == b"abc"
    assert bearer_token(b"bearer  abc ") == b"abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer ") is None
    assert bearer_token(None) is None


@pytest.mark.skipif(shutil.which("bash") is None, reason="braucht bash")
def test_script_legt_an_listet_und_sperrt(tmp_path):
    def starten(*argumente: str) -> subprocess.CompletedProcess[str]:
        umgebung = {
            **os.environ,
            "MISSA_ENV_DATEI": str(tmp_path / "missa.env"),
            "MISSA_PYTHON": sys.executable,
            "MISSA_BENUTZER": "gibt-es-nicht",
            "MISSA_DATEN": str(tmp_path / "daten"),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *argumente],
            env=umgebung,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    angelegt = starten("laptop")
    assert angelegt.returncode == 0, angelegt.stderr
    token = next(z.strip() for z in angelegt.stdout.splitlines() if z.strip().startswith("mg_"))
    tokens = GeraeteTokens(tmp_path / "daten" / "missa.db")
    assert tokens.pruefen(token) == "laptop"
    assert token not in (tmp_path / "daten" / "missa.db").read_bytes().decode("latin-1")

    doppelt = starten("laptop")
    assert doppelt.returncode == 1 and "gibt es schon" in doppelt.stderr
    liste = starten("--liste")
    assert liste.returncode == 0 and "laptop: aktiv" in liste.stdout
    gesperrt = starten("--sperren", "laptop")
    assert gesperrt.returncode == 0, gesperrt.stderr
    assert tokens.pruefen(token) is None
    assert "gesperrt" in starten("--liste").stdout
    assert starten().returncode == 1  # ohne Namen
    assert starten("--sperren", "gibt-es-nicht").returncode == 1


# ----------------------------------------------------------------------------- Verwaltung


class Leser:
    """Liest den Stream der Verwaltung wie ein Gerät (ohne HTTP)."""

    def __init__(self, geraete: Geraete, name: str = "laptop", gueltig=None) -> None:
        self.ereignisse: list[str] = []
        self.aufgabe = asyncio.create_task(self._lesen(geraete, name, gueltig))

    async def _lesen(self, geraete: Geraete, name: str, gueltig) -> None:
        async for teil in geraete.stream(name, gueltig):
            self.ereignisse.append(teil)

    async def warten(self, text: str) -> str:
        for _ in range(200):
            for teil in self.ereignisse:
                if text in teil:
                    return teil
            await asyncio.sleep(0.01)
        raise AssertionError(f"{text!r} kam nicht: {self.ereignisse}")

    async def stoppen(self) -> None:
        self.aufgabe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.aufgabe


def verwaltung(tmp_path: Path, **optionen: Any) -> Geraete:
    tokens = GeraeteTokens(tmp_path / "missa.db")
    tokens.anlegen("laptop")
    return Geraete(tokens, ZONE, **{"frist_s": 2, "offline_nach": 0.1, **optionen})


def kennung(ereignis: str) -> str:
    import json

    return json.loads(ereignis.split("data: ", 1)[1])["id"]


def test_anmelden_prueft_protokoll_und_namen(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path)
        with pytest.raises(GeraeteFehler, match="Protokoll"):
            geraete.anmelden("laptop", protokoll=2, geraet_name="laptop")
        with pytest.raises(GeraeteFehler, match="passt nicht"):
            geraete.anmelden("laptop", protokoll=1, geraet_name="handy")
        antwort = geraete.anmelden(
            "laptop",
            protokoll=1,
            geraet_name="laptop",
            sitzungen=["a", "a", " ", "b"],
            werkzeuge=[
                werkzeug("rechner_status"),
                werkzeug("rechner_status"),
                {"name": "kaputt", "beschreibung": "x", "stufe": "gefaehrlich"},
                {"beschreibung": "ohne Namen"},
                "kein Objekt",
                {**werkzeug("rechner_x"), "parameter": {"type": "string"}},
            ],
        )
        assert antwort["ok"] and antwort["geraet"] == "laptop"
        assert antwort["angenommen"] == ["rechner_status"]
        gruende = {a["name"]: a["grund"] for a in antwort["abgelehnt"]}
        assert "doppelt" in gruende["rechner_status"]
        assert "stufe" in gruende["kaputt"]
        assert "JSON-Schema" in gruende["rechner_x"]
        assert len(antwort["abgelehnt"]) == 5
        assert geraete.geraet("laptop").sitzungen == ("a", "b")
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_auftrag_und_ergebnis(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        leser = Leser(geraete)
        laufend = asyncio.create_task(geraete.auftrag("laptop", "rechner_status", {"kurz": True}))
        ereignis = await leser.warten("event: auftrag")
        assert '"werkzeug": "rechner_status"' in ereignis
        assert '"argumente": {"kurz": true}' in ereignis
        assert '"frist_s": 2' in ereignis
        kennung_ = kennung(ereignis)
        assert geraete.ergebnis("handy", {"id": kennung_, "ok": True})[0] == 404
        assert geraete.ergebnis("laptop", {"id": "unbekannt", "ok": True})[0] == 404
        status, inhalt = geraete.ergebnis("laptop", {"id": kennung_, "ok": True, "ergebnis": "ok"})
        assert (status, inhalt) == (200, {"ok": True})
        ergebnis = await laufend
        assert ergebnis.ok and ergebnis.text == "ok"
        # Jede ID höchstens einmal: ein zweites Ergebnis wird abgelehnt.
        assert geraete.ergebnis("laptop", {"id": kennung_, "ok": True})[0] == 409
        await leser.stoppen()
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_fehler_und_zu_langes_ergebnis(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        leser = Leser(geraete)
        erst = asyncio.create_task(geraete.auftrag("laptop", "rechner_befehl", {}))
        a = kennung(await leser.warten("rechner_befehl"))
        geraete.ergebnis(
            "laptop", {"id": a, "ok": False, "art": "abgelehnt", "fehler": "Nein  danke"}
        )
        ergebnis = await erst
        assert (ergebnis.ok, ergebnis.art, ergebnis.fehler) == (False, "abgelehnt", "Nein danke")

        zweit = asyncio.create_task(geraete.auftrag("laptop", "rechner_datei_lesen", {}))
        b = kennung(await leser.warten("rechner_datei_lesen"))
        geraete.ergebnis("laptop", {"id": b, "ok": True, "ergebnis": "x" * (MAX_ERGEBNIS + 50)})
        lang = await zweit
        assert lang.text.startswith("x" * MAX_ERGEBNIS) and "gekürzt" in lang.text

        dritt = asyncio.create_task(geraete.auftrag("laptop", "rechner_fenster_anzeigen", {}))
        c = kennung(await leser.warten("rechner_fenster_anzeigen"))
        geraete.ergebnis("laptop", {"id": c, "ok": False, "art": "quatsch"})
        assert (await dritt).art == "fehler"
        await leser.stoppen()
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_ohne_ergebnis_zeitlimit_und_abbruch(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path, frist_s=1)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        leser = Leser(geraete)
        with pytest.raises(GeraetZeitlimit):
            await geraete.auftrag("laptop", "rechner_status", {})
        abbruch = await leser.warten("event: abbruch")
        auftrag = next(e for e in leser.ereignisse if "event: auftrag" in e)
        assert kennung(abbruch) == kennung(auftrag)
        # Ein verspätetes Ergebnis wird nicht mehr angenommen.
        assert geraete.ergebnis("laptop", {"id": kennung(auftrag), "ok": True})[0] == 409
        await leser.stoppen()
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_offline_nach_abbruch_des_streams(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path, offline_nach=0.2)
        wechsel: list[bool] = []
        geraete.bei_aenderung.append(lambda g: wechsel.append(g.online))
        with pytest.raises(GeraetNichtErreichbar):
            await geraete.auftrag("laptop", "rechner_status", {})
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        leser = Leser(geraete)
        await leser.warten(": verbunden")
        assert geraete.ist_online("laptop")
        await leser.stoppen()
        # Kurz nach dem Abbruch noch online, nach der Frist offline.
        assert geraete.ist_online("laptop")
        laufend = asyncio.create_task(geraete.auftrag("laptop", "rechner_status", {}))
        await asyncio.sleep(0.4)
        assert not geraete.ist_online("laptop")
        with pytest.raises(GeraetNichtErreichbar):
            await laufend
        assert wechsel[-1] is False
        assert geraete.status()[0]["online"] is False
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_neue_verbindung_ersetzt_die_alte(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        alt = Leser(geraete)
        await alt.warten(": verbunden")
        neu = Leser(geraete)
        await neu.warten(": verbunden")
        await asyncio.sleep(0.05)
        assert alt.aufgabe.done()  # die alte Verbindung ist beendet
        laufend = asyncio.create_task(geraete.auftrag("laptop", "rechner_status", {}))
        ereignis = await neu.warten("event: auftrag")
        assert not any("event: auftrag" in e for e in alt.ereignisse)
        geraete.ergebnis("laptop", {"id": kennung(ereignis), "ok": True, "ergebnis": "ok"})
        assert (await laufend).ok
        assert geraete.ist_online("laptop")
        await neu.stoppen()
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_ping_und_gesperrter_token_beendet_den_stream(tmp_path):
    async def ablauf():
        geraete = verwaltung(tmp_path, ping_sekunden=0.05)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        leser = Leser(geraete, gueltig=lambda: geraete.tokens.aktiv("laptop"))
        await leser.warten(": ping")
        geraete.tokens.sperren("laptop")
        await asyncio.wait_for(leser.aufgabe, 2)
        assert not geraete.ist_online("laptop")
        await geraete.schliessen()

    asyncio.run(ablauf())


# ----------------------------------------------------------------------------- HTTP


def test_ohne_schalter_gibt_es_die_endpunkte_nicht(client):
    for methode, pfad in [
        ("POST", "/api/geraete/anmelden"),
        ("GET", "/api/geraete/auftraege"),
        ("POST", "/api/geraete/ergebnis"),
        ("GET", "/api/geraete"),
    ]:
        assert client.request(methode, pfad, json={}).status_code == 404
    assert "geraete" not in client.get("/health").json()


def test_anmelden_mit_gueltigem_token(app_client, token):
    antwort = app_client.post(
        "/api/geraete/anmelden", json=anmeldung(), headers={"Authorization": f"Bearer {token}"}
    )
    assert antwort.status_code == 200
    assert antwort.json() == {
        "ok": True,
        "geraet": "laptop",
        "angenommen": ["rechner_status"],
        "abgelehnt": [],
    }
    falsch = app_client.post(
        "/api/geraete/anmelden",
        json=anmeldung(protokoll=7),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert falsch.status_code == 400 and "Protokoll" in falsch.json()["error"]["message"]


@pytest.mark.parametrize("pfad", ["/api/geraete/anmelden", "/api/geraete/ergebnis"])
def test_falscher_token_und_api_schluessel_ergeben_401(app_client, pfad):
    for kopf in ("Bearer falsch", f"Bearer {API_KEY}", ""):
        antwort = app_client.post(pfad, content=b"{kaputt", headers={"Authorization": kopf})
        assert antwort.status_code == 401
        assert antwort.json()["error"]["code"] == "nicht_angemeldet"
    assert app_client.get("/api/geraete/auftraege").status_code == 401  # API-Schlüssel


def test_gesperrter_token_ergibt_401(app_client, token, mit_geraeten):
    kopf = {"Authorization": f"Bearer {token}"}
    assert app_client.post("/api/geraete/anmelden", json=anmeldung(), headers=kopf).is_success
    GeraeteTokens(mit_geraeten.datenbank).sperren("laptop")
    assert app_client.post("/api/geraete/anmelden", json=anmeldung(), headers=kopf).status_code == (
        401
    )


def test_nach_zehn_fehlversuchen_429(app_client, token):
    for _ in range(MAX_FEHLVERSUCHE):
        app_client.post("/api/geraete/anmelden", headers={"Authorization": "Bearer falsch"})
    gesperrt = app_client.post(
        "/api/geraete/anmelden", json=anmeldung(), headers={"Authorization": f"Bearer {token}"}
    )
    assert gesperrt.status_code == 429
    assert gesperrt.headers["Retry-After"]


def test_geraete_token_gilt_nicht_fuer_die_api(app_client, token):
    kopf = {"Authorization": f"Bearer {token}"}
    assert app_client.get("/v1/models", headers=kopf).status_code == 401
    assert app_client.get("/api/geraete", headers=kopf).status_code == 401


def test_health_und_liste_zeigen_geraete_nur_mit_anmeldung(app_client, token):
    app_client.post(
        "/api/geraete/anmelden", json=anmeldung(), headers={"Authorization": f"Bearer {token}"}
    )
    [eintrag] = app_client.get("/health").json()["geraete"]
    assert eintrag["name"] == "laptop" and eintrag["online"] is True
    assert eintrag["werkzeuge"] == ["rechner_status"]
    assert eintrag["seit"].startswith("2026-11-09T19:45")
    ohne = app_client.get("/health", headers={"Authorization": ""})
    assert ohne.status_code == 200 and "geraete" not in ohne.json()

    liste = app_client.get("/api/geraete").json()
    assert liste["anzahl"] == 1
    [geraet] = liste["geraete"]
    assert geraet["sitzungen"] == ["laptop-chat"]
    assert geraet["werkzeuge"][0] == {
        "name": "rechner_status",
        "stufe": "lesen",
        "beschreibung": "Gerätewerkzeug rechner_status.",
    }
    assert geraet["gesperrt"] is None and geraet["angelegt"]


def test_ergebnis_fuer_unbekannten_auftrag(app_client, token):
    antwort = app_client.post(
        "/api/geraete/ergebnis",
        json={"id": "gibt-es-nicht", "ok": True, "ergebnis": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert antwort.status_code == 404
    leer = app_client.post(
        "/api/geraete/ergebnis", json={"ok": True}, headers={"Authorization": f"Bearer {token}"}
    )
    assert leer.status_code == 400


# ----------------------------------------------------------------------------- Ende zu Ende


@pytest.fixture
def server(mit_geraeten, llama: FakeLlama, token):
    app = create_app(mit_geraeten, llama_transport=llama.transport, uhr=lambda: JETZT)
    app.state.geraete.ping_sekunden = 0.2

    @app.post("/test/auftrag")
    async def test_auftrag(daten: dict) -> dict:
        try:
            ergebnis = await app.state.geraete.auftrag(
                "laptop", daten["werkzeug"], daten["argumente"]
            )
        except Exception as fehler:
            return {"fehler": type(fehler).__name__}
        return dataclasses.asdict(ergebnis)

    with laufender_server(app) as basis:
        yield basis, app


def test_fake_geraet_bekommt_auftrag_und_liefert_ergebnis(server, token):
    basis, _app = server
    geraet = FakeGeraet(basis, token, werkzeuge=[werkzeug("rechner_status")])
    geraet.antworten["rechner_status"] = lambda a: f"Akku 80 %, Argumente {a['argumente']}"
    try:
        assert geraet.anmelden().json()["angenommen"] == ["rechner_status"]
        geraet.verbinden()
        with httpx.Client(base_url=basis, trust_env=False, timeout=10) as api:
            api.headers["Authorization"] = f"Bearer {API_KEY}"
            antwort = api.post(
                "/test/auftrag", json={"werkzeug": "rechner_status", "argumente": {"kurz": 1}}
            ).json()
            assert antwort == {
                "ok": True,
                "text": "Akku 80 %, Argumente {'kurz': 1}",
                "art": "",
                "fehler": "",
            }
            assert geraet.ergebnis_antworten[-1].status_code == 200
            # Ein zweites Ergebnis für dieselbe ID wird abgelehnt.
            nochmal = geraet.ergebnis({"id": geraet.auftraege[-1]["id"], "ok": True})
            assert nochmal.status_code == 409
            warten(lambda: geraet.pings >= 1)
            assert api.get("/health").json()["geraete"][0]["online"] is True
    finally:
        geraet.schliessen()


def test_fake_geraet_schweigt_zeitlimit(server, token):
    basis, app = server
    app.state.geraete.frist_s = 1
    geraet = FakeGeraet(basis, token)
    geraet.antworten["rechner_status"] = SCHWEIGEN
    try:
        geraet.anmelden()
        geraet.verbinden()
        with httpx.Client(base_url=basis, trust_env=False, timeout=10) as api:
            api.headers["Authorization"] = f"Bearer {API_KEY}"
            antwort = api.post(
                "/test/auftrag", json={"werkzeug": "rechner_status", "argumente": {}}
            )
        assert antwort.json() == {"fehler": "GeraetZeitlimit"}
        warten(lambda: geraet.abbrueche == [geraet.auftraege[0]["id"]])
    finally:
        geraet.schliessen()
