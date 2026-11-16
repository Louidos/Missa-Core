"""Mehrteilige Anfragen und verständliche Fehlerantworten von ``/v1/chat/completions``."""

from __future__ import annotations

import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.chat import KURZ_WERKZEUG_ERGEBNIS
from missa.core.werkzeuge import Stufe, Werkzeug, Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.erinnerungen import erinnerungs_werkzeuge
from missa.tools.kalender import Kalender, kalender_werkzeuge
from missa.tools.kalender_protokoll import KalenderProtokoll
from missa.tools.nextcloud import NextcloudVerbindung
from tests.caldav_fake import FakeDAV, FakeSammlung
from tests.conftest import API_KEY, JETZT, FakeLlama, sse_lesen, text, werkzeug_aufruf

ZONE = ZoneInfo("Europe/Zurich")
SATZ = (
    "Verschiebe das Home Office um eine Woche, mache morgen um 15:30–16 Uhr einen Eintrag "
    "Check-in und erinnere mich eine halbe Stunde vor dem Termin daran"
)
FRAGE = {"messages": [{"role": "user", "content": "Hallo"}]}

#: So meldet llama.cpp, dass eine Anfrage nicht in den Kontext passt.
ZU_GROSS = httpx.Response(
    400,
    json={
        "error": {
            "code": 400,
            "message": "the request exceeds the available context size, try increasing it",
            "type": "exceed_context_size_error",
            "n_prompt_tokens": 40321,
            "n_ctx": 32768,
        }
    },
)


def _letzte_ergebnis(nutzlast: dict[str, Any]) -> str:
    return nutzlast["messages"][-1]["content"]


# ---------------------------------------------------------------------- der Satz aus dem Auftrag


@pytest.fixture
def kalender_kasten(tmp_path) -> tuple[Werkzeugkasten, FakeSammlung]:
    sammlung = FakeSammlung("Persönlich")
    verbindung = NextcloudVerbindung(
        "https://cloud.example.ch", "louis", "pw", client_fabrik=FakeDAV(sammlung)
    )
    kalender = Kalender(
        verbindung,
        "Persönlich",
        ZONE,
        Bestaetigungen(),
        KalenderProtokoll(tmp_path / "kalender.db", ZONE),
    )
    kalender.termin_eintragen("Home Office", "2026-11-12T08:00", "2026-11-12T17:00")
    zeitplaner = Zeitplaner(tmp_path / "zeitplaner.db", ZONE, Zustellung())
    kasten = Werkzeugkasten(
        [
            *kalender_werkzeuge(
                verbindung, "Persönlich", ZONE, Bestaetigungen(), kalender=kalender
            ),
            *erinnerungs_werkzeuge(zeitplaner),
        ]
    )
    return kasten, sammlung


def test_drei_aufgaben_in_einem_satz_liefern_eine_antwort(
    einstellungen, llama: FakeLlama, kalender_kasten
):
    """Verschieben (braucht ein Ja), eintragen, erinnern – am Ende kommt eine Antwort."""
    kasten, sammlung = kalender_kasten
    ergebnisse: list[str] = []

    def verschieben(nutzlast):
        anzeige = _letzte_ergebnis(nutzlast)
        ergebnisse.append(anzeige)
        termin_id = re.search(r"\[ID (\w+)\]", anzeige).group(1)
        return werkzeug_aufruf(
            "termin_verschieben", {"termin_id": termin_id, "start": "2026-11-19T08:00"}, "c2"
        )

    def danach(name, argumente, kennung):
        def antwort(nutzlast):
            ergebnisse.append(_letzte_ergebnis(nutzlast))
            return werkzeug_aufruf(name, argumente, kennung)

        return antwort

    def schluss(nutzlast):
        ergebnisse.append(_letzte_ergebnis(nutzlast))
        return text(
            "Check-in ist eingetragen und die Erinnerung steht. Soll ich das Home Office "
            "wirklich auf Donnerstag, 19.11., verschieben?"
        )

    llama.antworten = [
        werkzeug_aufruf("termine_anzeigen", {"von": "2026-11-09", "bis": "2026-11-30"}, "c1"),
        verschieben,
        danach(
            "termin_eintragen",
            {"titel": "Check-in", "start": "2026-11-10T15:30", "ende": "2026-11-10T16:00"},
            "c3",
        ),
        danach("erinnerung_anlegen", {"was": "Check-in", "wann": "in 30 Minuten"}, "c4"),
        schluss,
    ]
    app = create_app(
        einstellungen, werkzeuge=kasten, llama_transport=llama.transport, uhr=lambda: JETZT
    )
    with TestClient(app) as client:
        antwort = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"messages": [{"role": "user", "content": SATZ}]},
        )

    assert antwort.status_code == 200
    inhalt = antwort.json()["choices"][0]["message"]["content"]
    assert "wirklich" in inhalt and "verschieben" in inhalt
    assert "Home Office" in ergebnisse[0]
    assert ergebnisse[1].startswith("NICHT AUSGEFÜHRT – termin_verschieben")
    assert ergebnisse[2].startswith("Eingetragen")
    assert ergebnisse[3].startswith("Erinnerung gespeichert")
    titel = sorted(str(o.icalendar_component["summary"]) for o in sammlung.objekte)
    assert titel == ["Check-in", "Home Office"]
    # Home Office ist ohne Ja noch nicht verschoben.
    home = next(o for o in sammlung.objekte if "Home" in str(o.icalendar_component["summary"]))
    assert home.icalendar_component["dtstart"].dt.day == 12
    assert len(llama.anfragen) == 5


# ---------------------------------------------------------------------- Fehlerwege


@pytest.mark.parametrize(
    ("antwort", "status", "code", "stichwort"),
    [
        (httpx.ReadTimeout("zu langsam"), 504, "zeitlimit", "LLAMA_TIMEOUT"),
        (ZU_GROSS, 502, "anfrage_zu_gross", "zu gross"),
        (httpx.ConnectError("Verbindung abgelehnt"), 502, "modell_nicht_erreichbar", "läuft"),
        (httpx.Response(503, text="Loading model"), 502, "modell_nicht_erreichbar", "Minute"),
        (httpx.Response(200, json={"error": "kaputt"}), 502, "unerwartete_antwort", "Log"),
        (httpx.Response(200, text="kein json"), 502, "unerwartete_antwort", "Log"),
        (httpx.Response(500, text="Failed to parse input"), 502, "modell_fehler", "500"),
    ],
)
def test_fehlerweg_hat_code_und_deutschen_satz(
    client, llama: FakeLlama, caplog, antwort, status, code, stichwort
):
    llama.antworten = [antwort, antwort]
    with caplog.at_level(logging.ERROR):
        ergebnis = client.post("/v1/chat/completions", json=FRAGE)
    assert ergebnis.status_code == status
    fehler = ergebnis.json()["error"]
    assert "choices" not in ergebnis.json()
    assert fehler["code"] == code
    assert fehler["type"] == "llama_fehler"
    assert stichwort in fehler["message"]
    assert fehler["message"].startswith("Das Sprachmodell") or "Anfrage" in fehler["message"]
    # Im Log: Anfrage-ID und Originaltext des llama-servers.
    assert fehler["anfrage_id"] in caplog.text
    assert code in caplog.text


def test_originaltext_des_llama_servers_steht_im_log(client, llama: FakeLlama, caplog):
    llama.antworten = [httpx.Response(500, text="Failed to parse input at pos 42")]
    with caplog.at_level(logging.ERROR):
        client.post("/v1/chat/completions", json=FRAGE)
    assert "Failed to parse input at pos 42" in caplog.text


def test_unerwarteter_fehler_ist_json_mit_code(client, llama: FakeLlama):
    def kaputt(*_args, **_kwargs):
        raise RuntimeError("Datenbank weg")

    client.app.state.dienst._merken = kaputt
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.status_code == 500
    fehler = antwort.json()["error"]
    assert fehler["code"] == "interner_fehler"
    assert "unerwarteter Fehler" in fehler["message"]
    assert fehler["anfrage_id"]


def test_fehler_ohne_modell_haben_feste_codes(client):
    assert client.post("/v1/chat/completions", json={"messages": []}).json()["error"]["code"] == (
        "ungueltige_anfrage"
    )
    client.headers.pop("Authorization")
    assert client.post("/v1/chat/completions", json=FRAGE).json()["error"]["code"] == (
        "nicht_angemeldet"
    )


def test_fehler_im_stream_hat_code(client, llama: FakeLlama):
    llama.antworten = [httpx.ReadTimeout("zu langsam")]
    antwort = client.post("/v1/chat/completions", json={**FRAGE, "stream": True})
    ereignisse = sse_lesen(antwort)
    fehler = next(e for e in ereignisse if isinstance(e, dict) and "error" in e)["error"]
    assert fehler["code"] == "zeitlimit"
    assert "LLAMA_TIMEOUT" in fehler["message"]
    assert ereignisse[-1] == "[DONE]"


# ---------------------------------------------------------------------- zu gross: gekürzt nochmal


@pytest.fixture
def werkzeuge() -> Werkzeugkasten:
    def lang() -> str:
        return "x" * 20_000

    return Werkzeugkasten(
        [Werkzeug("lang", "Liefert viel Text.", {"type": "object"}, lang, stufe=Stufe.LESEN)]
    )


def test_zu_grosse_anfrage_wird_einmal_gekuerzt_wiederholt(client, llama: FakeLlama):
    sitzung = {"X-Missa-Session": "handy"}
    for nummer in range(3):
        llama.antworten = [text(f"Antwort {nummer}")]
        client.post(
            "/v1/chat/completions",
            headers=sitzung,
            json={"messages": [{"role": "user", "content": f"Frage {nummer}"}]},
        )
    llama.anfragen.clear()
    llama.antworten = [
        werkzeug_aufruf("lang", {}),
        ZU_GROSS,
        text("Passt jetzt."),
    ]
    antwort = client.post(
        "/v1/chat/completions",
        headers=sitzung,
        json={"messages": [{"role": "user", "content": "Und jetzt viel"}]},
    )
    assert antwort.status_code == 200
    assert antwort.json()["choices"][0]["message"]["content"] == "Passt jetzt."
    abgelehnt, gekuerzt = llama.anfragen[1], llama.anfragen[2]
    assert len(gekuerzt["messages"]) < len(abgelehnt["messages"])
    rollen = [n["role"] for n in gekuerzt["messages"]]
    assert rollen == ["system", "user", "assistant", "tool"]
    assert gekuerzt["messages"][1]["content"] == "Und jetzt viel"
    werkzeug_text = gekuerzt["messages"][-1]["content"]
    assert len(werkzeug_text) < KURZ_WERKZEUG_ERGEBNIS + 100
    assert "gekürzt" in werkzeug_text
    assert gekuerzt["tools"] == abgelehnt["tools"]


def test_zu_gross_auch_nach_kuerzen_gibt_klaren_fehler(client, llama: FakeLlama):
    llama.antworten = [werkzeug_aufruf("lang", {}), ZU_GROSS, ZU_GROSS]
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.status_code == 502
    assert antwort.json()["error"]["code"] == "anfrage_zu_gross"
    assert len(llama.anfragen) == 3  # erster Aufruf, abgelehnt, einmal gekürzt – nicht öfter


def test_ohne_etwas_zum_kuerzen_kein_zweiter_versuch(client, llama: FakeLlama):
    llama.antworten = [ZU_GROSS, text("nie")]
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.json()["error"]["code"] == "anfrage_zu_gross"
    assert len(llama.anfragen) == 1


def test_zu_gross_im_stream_wird_gekuerzt_wiederholt(client, llama: FakeLlama):
    llama.antworten = [werkzeug_aufruf("lang", {}), ZU_GROSS, text("Gekürzt geht es.")]
    antwort = client.post("/v1/chat/completions", json={**FRAGE, "stream": True})
    inhalt = "".join(
        e["choices"][0]["delta"].get("content") or ""
        for e in sse_lesen(antwort)
        if isinstance(e, dict) and "choices" in e
    )
    assert "Gekürzt geht es." in inhalt
    assert len(llama.anfragen) == 3
