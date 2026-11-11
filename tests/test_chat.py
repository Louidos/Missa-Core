"""Chat-Completions: Systemprompt, Werkzeug-Schleife, Streaming, Verlauf."""

from __future__ import annotations

import sqlite3

import pytest

from missa.core.chat import KEIN_ERGEBNIS, MAX_RUNDEN
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler, Werkzeugkasten
from tests.conftest import FakeLlama, sse_lesen, text, werkzeug_aufruf

FRAGE = {"messages": [{"role": "user", "content": "Hallo Missa"}]}


def frage(inhalt: str, **weitere) -> dict:
    return {"messages": [{"role": "user", "content": inhalt}], **weitere}


@pytest.fixture
def werkzeuge() -> Werkzeugkasten:
    def wetter(ort: str) -> str:
        return f"In {ort} scheint die Sonne."

    def kaputt() -> str:
        raise RuntimeError("Server weg")

    def verboten() -> str:
        raise WerkzeugFehler("Kalender nicht gefunden")

    leer = {"type": "object", "properties": {}}
    return Werkzeugkasten(
        [
            Werkzeug(
                "wetter",
                "Liefert das Wetter.",
                {"type": "object", "properties": {"ort": {"type": "string"}}, "required": ["ort"]},
                wetter,
                stufe=Stufe.LESEN,
            ),
            Werkzeug("kaputt", "Geht immer schief.", leer, kaputt, stufe=Stufe.LESEN),
            Werkzeug(
                "verboten", "Meldet einen erwarteten Fehler.", leer, verboten, stufe=Stufe.LESEN
            ),
        ]
    )


# ---------------------------------------------------------------------- Antwortformat


def test_antwort_im_openai_format(client, llama: FakeLlama):
    llama.antworten = [text("Na klar! Hallo Louis.")]
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["object"] == "chat.completion"
    assert daten["model"] == "missa"
    assert daten["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Na klar! Hallo Louis.",
    }
    assert daten["choices"][0]["finish_reason"] == "stop"
    assert daten["usage"]["total_tokens"] == 15


def test_parameter_werden_durchgereicht(client, llama: FakeLlama):
    client.post("/v1/chat/completions", json=frage("Hi", temperature=0.2, max_tokens=50, foo=1))
    anfrage = llama.anfragen[-1]
    assert anfrage["temperature"] == 0.2
    assert anfrage["max_tokens"] == 50
    assert "foo" not in anfrage
    assert anfrage["stream"] is False


# ---------------------------------------------------------------------- Systemprompt


def test_systemprompt_enthaelt_charakter_zeit_und_werkzeuge(client, llama: FakeLlama):
    client.post("/v1/chat/completions", json=FRAGE)
    system = llama.system()
    assert system.startswith("Du bist Missa. Du sagst gern 'Na klar!'.")
    assert "Montag, 9. November 2026, 19:45 Uhr" in system
    assert "Europe/Zurich" in system
    assert "morgen: Dienstag, 10. November 2026 = 2026-11-10" in system
    assert "- wetter: Liefert das Wetter." in system
    assert llama.anfragen[-1]["tools"][0]["function"]["name"] == "wetter"


def test_geaenderter_charakter_wirkt_ohne_neustart(client, llama: FakeLlama, charakter_datei):
    client.post("/v1/chat/completions", json=FRAGE)
    assert "Na klar!" in llama.system()
    charakter_datei.write_text("Du bist Missa, heute sehr förmlich.", encoding="utf-8")
    client.post("/v1/chat/completions", json=FRAGE)
    assert llama.system().startswith("Du bist Missa, heute sehr förmlich.")
    assert "Na klar!" not in llama.system()


def test_fehlende_charakterdatei_nutzt_ersatz(client, llama: FakeLlama, charakter_datei):
    charakter_datei.unlink()
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.status_code == 200
    assert llama.system().startswith("Du bist Missa, die persönliche Assistentin von Louis.")


def test_client_system_wird_angehaengt_und_ersetzt_charakter_nicht(client, llama: FakeLlama):
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "Du bist ein Pirat."},
                {"role": "user", "content": "Hallo"},
            ]
        },
    )
    nachrichten = llama.anfragen[-1]["messages"]
    assert [n["role"] for n in nachrichten] == ["system", "user"]
    system = nachrichten[0]["content"]
    assert system.startswith("Du bist Missa.")
    assert system.index("Du bist Missa.") < system.index("Du bist ein Pirat.")
    assert "## Zusätzliche Hinweise der App\nDu bist ein Pirat." in system


# ---------------------------------------------------------------------- Werkzeug-Schleife


def test_werkzeug_wird_ausgefuehrt_und_ergebnis_zurueckgeschickt(client, llama: FakeLlama):
    llama.antworten = [werkzeug_aufruf("wetter", {"ort": "Bern"}), text("In Bern ist es sonnig.")]
    antwort = client.post("/v1/chat/completions", json=frage("Wie ist das Wetter in Bern?"))
    assert antwort.json()["choices"][0]["message"]["content"] == "In Bern ist es sonnig."
    assert len(llama.anfragen) == 2
    zweite = llama.anfragen[1]["messages"]
    assert zweite[-2]["role"] == "assistant"
    assert zweite[-2]["tool_calls"][0]["function"]["name"] == "wetter"
    assert zweite[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "wetter",
        "content": "In Bern scheint die Sonne.",
    }
    assert antwort.json()["usage"]["total_tokens"] == 30


@pytest.mark.parametrize(
    ("name", "argumente", "erwartet"),
    [
        ("kaputt", {}, "Fehler beim Ausführen von kaputt: Server weg"),
        ("verboten", {}, "Fehler: Kalender nicht gefunden"),
        ("gibtsnicht", {}, "Fehler: Das Werkzeug „gibtsnicht“ gibt es nicht."),
        ("wetter", {}, "Fehler: Falsche Parameter für wetter"),
        ("wetter", {"ort": "Bern", "x": 1}, "Fehler: Falsche Parameter für wetter"),
    ],
)
def test_werkzeugfehler_gehen_als_text_an_das_modell(client, llama, name, argumente, erwartet):
    llama.antworten = [werkzeug_aufruf(name, argumente), text("Das hat leider nicht geklappt.")]
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.status_code == 200
    assert llama.anfragen[1]["messages"][-1]["content"].startswith(erwartet)


def test_ungueltiges_json_in_argumenten(client, llama: FakeLlama):
    aufruf = werkzeug_aufruf("wetter", {})
    aufruf["tool_calls"][0]["function"]["arguments"] = "{kaputt"
    llama.antworten = [aufruf, text("Ups.")]
    client.post("/v1/chat/completions", json=FRAGE)
    assert "kein gültiges JSON" in llama.anfragen[1]["messages"][-1]["content"]


def test_hoechstens_fuenf_werkzeug_runden(client, llama: FakeLlama):
    llama.antworten = [werkzeug_aufruf("wetter", {"ort": "Bern"})] * MAX_RUNDEN + [
        text("Jetzt aber: sonnig.")
    ]
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.json()["choices"][0]["message"]["content"] == "Jetzt aber: sonnig."
    assert len(llama.anfragen) == MAX_RUNDEN + 1
    assert all("tools" in a for a in llama.anfragen[:MAX_RUNDEN])
    assert "tools" not in llama.anfragen[-1]


def test_endlose_werkzeug_aufrufe_enden_mit_ehrlicher_meldung(client, llama: FakeLlama):
    llama.antworten = [werkzeug_aufruf("wetter", {"ort": "Bern"})] * (MAX_RUNDEN + 1)
    antwort = client.post("/v1/chat/completions", json=FRAGE)
    assert antwort.json()["choices"][0]["message"]["content"] == KEIN_ERGEBNIS


def test_ohne_werkzeuge_kein_tools_feld(einstellungen, llama: FakeLlama):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from tests.conftest import API_KEY

    app = create_app(einstellungen, werkzeuge=Werkzeugkasten(), llama_transport=llama.transport)
    with TestClient(app) as test_client:
        test_client.post(
            "/v1/chat/completions", json=FRAGE, headers={"Authorization": f"Bearer {API_KEY}"}
        )
    assert "tools" not in llama.anfragen[-1]
    assert "keine Werkzeuge" in llama.system()


# ---------------------------------------------------------------------- Streaming


def test_stream_liefert_openai_chunks(client, llama: FakeLlama):
    llama.antworten = [text("Na klar! Hallo.")]
    antwort = client.post("/v1/chat/completions", json=frage("Hallo", stream=True))
    assert antwort.status_code == 200
    assert antwort.headers["content-type"].startswith("text/event-stream")
    ereignisse = sse_lesen(antwort)
    assert ereignisse[-1] == "[DONE]"
    chunks = ereignisse[:-1]
    assert all(c["object"] == "chat.completion.chunk" and c["model"] == "missa" for c in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    inhalt = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert inhalt == "Na klar! Hallo."
    assert llama.anfragen[-1]["stream"] is True


def test_stream_mit_werkzeug(client, llama: FakeLlama):
    llama.antworten = [werkzeug_aufruf("wetter", {"ort": "Chur"}), text("In Chur: Sonne.")]
    antwort = client.post("/v1/chat/completions", json=frage("Wetter?", stream=True))
    chunks = sse_lesen(antwort)[:-1]
    inhalt = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert inhalt == "In Chur: Sonne."
    assert llama.anfragen[1]["messages"][-1]["content"] == "In Chur scheint die Sonne."
    assert llama.anfragen[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == (
        '{"ort": "Chur"}'
    )


def test_stream_meldet_llama_fehler(client, llama: FakeLlama):
    llama.erreichbar = False
    antwort = client.post("/v1/chat/completions", json=frage("Hallo", stream=True))
    ereignisse = sse_lesen(antwort)
    assert ereignisse[-1] == "[DONE]"
    assert "error" in ereignisse[-2]


# ---------------------------------------------------------------------- Verlauf


def test_zweiter_aufruf_derselben_sitzung_kennt_vorherige_nachricht(client, llama: FakeLlama):
    kopf = {"X-Missa-Session": "handy"}
    llama.antworten = [text("Freut mich, Louis!"), text("Du heißt Louis.")]
    client.post("/v1/chat/completions", headers=kopf, json=frage("Ich heiße Louis."))
    client.post("/v1/chat/completions", headers=kopf, json=frage("Wie heiße ich?"))
    nachrichten = llama.anfragen[1]["messages"]
    assert [(n["role"], n["content"]) for n in nachrichten[1:]] == [
        ("user", "Ich heiße Louis."),
        ("assistant", "Freut mich, Louis!"),
        ("user", "Wie heiße ich?"),
    ]


def test_sitzung_ueber_user_feld(client, llama: FakeLlama):
    client.post("/v1/chat/completions", json=frage("Eins", user="tablet"))
    client.post("/v1/chat/completions", json=frage("Zwei", user="tablet"))
    assert [n["content"] for n in llama.anfragen[1]["messages"][1:]] == [
        "Eins",
        "Hallo, ich bin Missa.",
        "Zwei",
    ]


def test_header_hat_vorrang_vor_user_feld(client, llama: FakeLlama):
    client.post(
        "/v1/chat/completions", headers={"X-Missa-Session": "a"}, json=frage("A1", user="b")
    )
    client.post("/v1/chat/completions", json=frage("B1", user="b"))
    assert len(llama.anfragen[1]["messages"]) == 2


def test_ohne_sitzung_keine_speicherung(client, llama: FakeLlama):
    client.post("/v1/chat/completions", json=frage("Geheim"))
    client.post("/v1/chat/completions", json=frage("Weißt du es noch?"))
    assert len(llama.anfragen[1]["messages"]) == 2
    with sqlite3.connect(client.app.state.dienst.verlauf.datei) as db:
        assert db.execute("SELECT COUNT(*) FROM nachrichten").fetchone()[0] == 0


def test_sitzungen_sind_getrennt(client, llama: FakeLlama):
    client.post("/v1/chat/completions", headers={"X-Missa-Session": "a"}, json=frage("Für A"))
    client.post("/v1/chat/completions", headers={"X-Missa-Session": "b"}, json=frage("Für B"))
    assert len(llama.anfragen[1]["messages"]) == 2


def test_nur_die_letzten_20_nachrichten(client, llama: FakeLlama):
    verlauf = client.app.state.dienst.verlauf
    for nummer in range(30):
        verlauf.speichern("lang", "user" if nummer % 2 == 0 else "assistant", f"N{nummer}")
    client.post("/v1/chat/completions", headers={"X-Missa-Session": "lang"}, json=frage("Neu"))
    inhalte = [n["content"] for n in llama.anfragen[0]["messages"][1:]]
    assert inhalte == [f"N{n}" for n in range(10, 30)] + ["Neu"]


def test_app_mit_eigenem_verlauf_bekommt_nichts_doppelt(client, llama: FakeLlama):
    kopf = {"X-Missa-Session": "web"}
    client.post("/v1/chat/completions", headers=kopf, json=frage("Erste Frage"))
    voll = {
        "messages": [
            {"role": "user", "content": "Erste Frage"},
            {"role": "assistant", "content": "Hallo, ich bin Missa."},
            {"role": "user", "content": "Zweite Frage"},
        ]
    }
    client.post("/v1/chat/completions", headers=kopf, json=voll)
    inhalte = [n["content"] for n in llama.anfragen[1]["messages"][1:]]
    assert inhalte == ["Erste Frage", "Hallo, ich bin Missa.", "Zweite Frage"]
    # Die neue Frage wird trotzdem gespeichert.
    gespeichert = client.app.state.dienst.verlauf.laden("web")
    assert [n["content"] for n in gespeichert][-2:] == ["Zweite Frage", "Hallo, ich bin Missa."]


def test_stream_speichert_verlauf(client, llama: FakeLlama):
    kopf = {"X-Missa-Session": "stream"}
    llama.antworten = [text("Gestreamt."), text("Zweite.")]
    client.post("/v1/chat/completions", headers=kopf, json=frage("Eins", stream=True))
    client.post("/v1/chat/completions", headers=kopf, json=frage("Zwei"))
    inhalte = [n["content"] for n in llama.anfragen[1]["messages"][1:]]
    assert inhalte == ["Eins", "Gestreamt.", "Zwei"]


def test_nachricht_mit_teilen(client, llama: FakeLlama):
    kopf = {"X-Missa-Session": "teile"}
    inhalt = [{"type": "text", "text": "Was siehst du?"}, {"type": "image_url", "image_url": {}}]
    client.post(
        "/v1/chat/completions",
        headers=kopf,
        json={"messages": [{"role": "user", "content": inhalt}]},
    )
    gespeichert = client.app.state.dienst.verlauf.laden("teile")
    assert gespeichert[0]["content"] == "Was siehst du?\n[image_url]"
    # An llama-server geht der Inhalt unverändert.
    assert llama.anfragen[0]["messages"][-1]["content"] == inhalt
