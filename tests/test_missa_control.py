"""Gruppe 2: Funktionen aus Missa Control als Werkzeuge (mit Nachbau des Panels)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.config import Einstellungen
from missa.core.rechte import rechte_bauen
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler, Werkzeugkasten, im_hintergrund
from missa.integrationen.missa_control import MissaControl
from missa.tools.panel import GRUPPE, PanelWerkzeuge, panel_bauen
from tests.conftest import API_KEY, JETZT, FakeLlama, text, werkzeug_aufruf
from tests.missa_control_fake import TOKEN, URL, FakePanel, funktion, text_ergebnis


@pytest.fixture
def panel() -> FakePanel:
    p = FakePanel()
    p.funktionen = [
        funktion("wetterstation_lesen", "Liest die Wetterstation.", nur_lesen=True),
        funktion(
            "licht_schalten",
            "Schaltet das Licht (erst nachfragen).",
            nur_lesen=False,
            an={"type": "boolean"},
        ),
    ]
    p.ergebnisse["wetterstation_lesen"] = "Draussen 7 °C, leichter Regen."
    return p


@pytest.fixture
def mit_panel(einstellungen: Einstellungen) -> Einstellungen:
    return dataclasses.replace(einstellungen, missa_control_url=URL, missa_control_token=TOKEN)


@pytest.fixture
def app_client(mit_panel, llama: FakeLlama, panel: FakePanel):
    kasten = Werkzeugkasten(
        [
            Werkzeug(
                "wetter",
                "Eingebautes Wetter.",
                {"type": "object"},
                lambda: "Sonne",
                stufe=Stufe.LESEN,
            )
        ]
    )
    app = create_app(
        mit_panel,
        werkzeuge=kasten,
        llama_transport=llama.transport,
        panel_transport=panel.transport,
        uhr=lambda: JETZT,
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        yield client


def bauen(einstellungen: Einstellungen, panel: FakePanel, kasten: Werkzeugkasten | None = None):
    kasten = kasten if kasten is not None else Werkzeugkasten()
    werkzeuge = panel_bauen(einstellungen, kasten, panel.transport)
    assert werkzeuge is not None
    return werkzeuge, kasten


def ausfuehren(kasten: Werkzeugkasten, name: str, argumente: dict | None = None) -> str:
    return asyncio.run(kasten.ausfuehren(name, argumente or {}, "anfrage-1", nachricht="los"))


# ---------------------------------------------------------------------- Einstellungen


def test_einstellungen_aus_umgebung(tmp_path):
    datei = tmp_path / "panel.token"
    e = Einstellungen.aus_umgebung(
        {
            "MISSA_API_KEY": "x",
            "MISSA_CONTROL_URL": "http://127.0.0.1:8090/",
            "MISSA_CONTROL_TOKEN_FILE": str(datei),
            "MISSA_CONTROL_AKTUALISIEREN": "30",
            "MISSA_CONTROL_ZEITLIMIT": "400",
            "MISSA_CONTROL_MAX_WERKZEUGE": "10",
            "MISSA_CONTROL_AUSLASSEN": "kalender_, gedaechtnis_",
            "MISSA_CONTROL_IM_HINTERGRUND": "alles",
        }
    )
    assert e.missa_control_url == "http://127.0.0.1:8090"
    assert e.missa_control_token_file == datei
    assert e.missa_control_konfiguriert
    assert e.missa_control_aktualisieren == 30
    assert e.missa_control_zeitlimit == 400
    assert e.missa_control_max_werkzeuge == 10
    assert e.missa_control_auslassen == ("kalender_", "gedaechtnis_")
    assert e.missa_control_im_hintergrund == "alles"
    e.pruefen()


def test_standardwerte_und_ohne_token_aus():
    e = Einstellungen.aus_umgebung({"MISSA_CONTROL_URL": "http://127.0.0.1:8090"})
    assert not e.missa_control_konfiguriert
    assert e.missa_control_aktualisieren == 60
    assert e.missa_control_zeitlimit == 330
    assert e.missa_control_max_werkzeuge == 40
    assert e.missa_control_im_hintergrund == "nur_lesen"
    assert panel_bauen(e, Werkzeugkasten()) is None


def test_ungueltiger_hintergrund_wert_wird_abgelehnt():
    e = Einstellungen(api_key="x", missa_control_im_hintergrund="manchmal")
    with pytest.raises(ValueError, match="MISSA_CONTROL_IM_HINTERGRUND"):
        e.pruefen()


def test_token_ist_ein_geheimnis(tmp_path):
    datei = tmp_path / "t"
    datei.write_text("aus-datei\n", encoding="utf-8")
    e = Einstellungen(missa_control_token="geheim", missa_control_token_file=datei)
    assert "geheim" in e.geheimnisse
    assert "aus-datei" in e.geheimnisse


def test_token_aus_datei_wird_bei_jedem_aufruf_gelesen(einstellungen, panel, tmp_path):
    datei = tmp_path / "panel.token"
    datei.write_text("alt\n", encoding="utf-8")
    e = dataclasses.replace(einstellungen, missa_control_url=URL, missa_control_token_file=datei)
    werkzeuge, kasten = bauen(e, panel)
    assert not asyncio.run(werkzeuge.aktualisieren())
    datei.write_text(f"{TOKEN}\n", encoding="utf-8")
    assert asyncio.run(werkzeuge.aktualisieren())
    assert "wetterstation_lesen" in kasten


# ---------------------------------------------------------------------- Liste und Stufen


@pytest.mark.parametrize("sse", [False, True])
def test_liste_laden_und_stufen(mit_panel, panel, sse):
    panel.sse = sse
    werkzeuge, kasten = bauen(mit_panel, panel)
    assert asyncio.run(werkzeuge.aktualisieren())
    assert kasten.gruppe(GRUPPE) == ("wetterstation_lesen", "licht_schalten")
    lesen = kasten.get("wetterstation_lesen")
    schreiben = kasten.get("licht_schalten")
    assert lesen.stufe is Stufe.LESEN
    assert schreiben.stufe is Stufe.SCHREIBEN  # bewusst nie zerstörerisch
    assert lesen.beschreibung == "Liest die Wetterstation."
    assert schreiben.parameter["properties"] == {"an": {"type": "boolean"}}
    assert panel.kopfzeilen[-1]["Authorization"] == f"Bearer {TOKEN}"


def test_ohne_readonly_hinweis_gilt_schreiben(mit_panel, panel):
    panel.funktionen = [{"name": "unklar", "description": "x", "inputSchema": {}}]
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    assert kasten.get("unklar").stufe is Stufe.SCHREIBEN
    assert kasten.get("unklar").parameter == {"type": "object", "properties": {}}


def test_namensgleichheit_eingebautes_werkzeug_gewinnt(mit_panel, panel, caplog):
    panel.funktionen.append(funktion("wetter", "Panel-Wetter"))
    eingebaut = Werkzeug(
        "wetter", "Eingebaut", {"type": "object"}, lambda: "Sonne", stufe=Stufe.LESEN
    )
    werkzeuge, kasten = bauen(mit_panel, panel, Werkzeugkasten([eingebaut]))
    with caplog.at_level(logging.WARNING):
        asyncio.run(werkzeuge.aktualisieren())
    assert kasten.get("wetter") is eingebaut
    assert "wetter" not in kasten.gruppe(GRUPPE)
    assert "Funktion wetter ausgelassen" in caplog.text
    assert "wetter" in werkzeuge.ausgelassen


def test_obergrenze(mit_panel, panel, caplog):
    panel.funktionen = [funktion(f"f{i}") for i in range(5)]
    e = dataclasses.replace(mit_panel, missa_control_max_werkzeuge=3)
    werkzeuge, kasten = bauen(e, panel)
    with caplog.at_level(logging.WARNING):
        asyncio.run(werkzeuge.aktualisieren())
    assert kasten.gruppe(GRUPPE) == ("f0", "f1", "f2")
    assert "MISSA_CONTROL_MAX_WERKZEUGE" in caplog.text
    assert "f3, f4" in caplog.text


def test_vorsilben_auslassen(mit_panel, panel):
    panel.funktionen.append(funktion("kalender_termine_anzeigen"))
    e = dataclasses.replace(mit_panel, missa_control_auslassen=("kalender_",))
    werkzeuge, kasten = bauen(e, panel)
    asyncio.run(werkzeuge.aktualisieren())
    assert "kalender_termine_anzeigen" not in kasten
    assert "wetterstation_lesen" in kasten


def test_aktualisierung_zur_laufzeit(mit_panel, panel):
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    panel.funktionen = [funktion("rollladen_lesen"), panel.funktionen[1]]
    asyncio.run(werkzeuge.aktualisieren())
    assert "rollladen_lesen" in kasten
    assert "wetterstation_lesen" not in kasten
    assert kasten.gruppe(GRUPPE) == ("rollladen_lesen", "licht_schalten")


def test_unbekannte_funktion_laedt_sofort_neu(mit_panel, panel):
    werkzeuge, kasten = bauen(mit_panel, panel)

    async def ablauf():
        await werkzeuge.aktualisieren()
        panel.funktionen = [funktion("neu_lesen")]
        text = await kasten.ausfuehren("wetterstation_lesen", {}, "a1")
        await werkzeuge._nachladen
        return text

    text = asyncio.run(ablauf())
    assert "gibt es nicht" in text
    assert panel.listen_abrufe == 2
    assert kasten.gruppe(GRUPPE) == ("neu_lesen",)


# ---------------------------------------------------------------------- Aufruf


def test_aufruf_liefert_text_und_steht_im_protokoll(mit_panel, panel):
    rechte, protokoll = rechte_bauen(mit_panel)
    werkzeuge, kasten = bauen(mit_panel, panel, Werkzeugkasten(rechte=rechte))
    asyncio.run(werkzeuge.aktualisieren())
    assert ausfuehren(kasten, "licht_schalten", {"an": True}) == "licht_schalten erledigt"
    assert panel.aufrufe == [("licht_schalten", {"an": True})]
    [eintrag] = protokoll.liste(None, None, 10, "licht_schalten", None)
    assert eintrag.status == "ausgefuehrt"
    assert eintrag.stufe == "schreiben"


def test_fehler_ergebnis_wird_werkzeugfehler_mit_satz_des_panels(mit_panel, panel):
    panel.ergebnisse["licht_schalten"] = text_ergebnis(
        "Die Aktion „licht_schalten“ wurde vom Besitzer im Panel abgelehnt und nicht ausgeführt.",
        fehler=True,
    )
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    text = ausfuehren(kasten, "licht_schalten")
    assert text == (
        "Fehler: Die Aktion „licht_schalten“ wurde vom Besitzer im Panel abgelehnt und "
        "nicht ausgeführt."
    )


def test_lange_texte_werden_gekuerzt_und_bilder_ausgelassen(mit_panel, panel):
    panel.ergebnisse["wetterstation_lesen"] = {
        "content": [
            {"type": "text", "text": "a" * 10_000},
            {"type": "image", "data": "...", "mimeType": "image/png"},
        ],
        "isError": False,
    }
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    text = ausfuehren(kasten, "wetterstation_lesen")
    assert len(text) < 6300
    assert "gekürzt" in text
    assert "1 Inhalt(e) ohne Text ausgelassen (image)" in text


def test_lange_wartezeit_auf_freigabe(mit_panel, panel):
    panel.warten = 0.3
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    assert ausfuehren(kasten, "licht_schalten") == "licht_schalten erledigt"
    # Freigaben dürfen bis zu 5 Minuten dauern: Zeitlimit pro Aufruf 330 s.
    assert werkzeuge.verbindung._client.timeout.read == 330


def test_zeitlimit_ueberschritten_sagt_nicht_erreichbar(mit_panel, panel):
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    panel.ausfall = httpx.ReadTimeout("zu langsam")
    assert "Missa Control ist gerade nicht erreichbar" in ausfuehren(kasten, "licht_schalten")


def test_panel_weg_alte_werkzeuge_bleiben(mit_panel, panel):
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    panel.erreichbar = False
    assert not asyncio.run(werkzeuge.aktualisieren())
    assert "wetterstation_lesen" in kasten
    assert not werkzeuge.verbunden
    text = ausfuehren(kasten, "wetterstation_lesen")
    assert text.startswith("Fehler: Missa Control ist gerade nicht erreichbar")


def test_token_falsch_klare_meldung_im_log(mit_panel, panel, caplog):
    panel.token = "anderer"
    werkzeuge, kasten = bauen(mit_panel, panel)
    with caplog.at_level(logging.ERROR):
        assert not asyncio.run(werkzeuge.aktualisieren())
    assert "Token ungültig (401)" in caplog.text
    assert "Clients" in caplog.text
    assert len(kasten) == 0


def test_hintergrund_darf_standardmaessig_nur_lesen(mit_panel, panel):
    werkzeuge, kasten = bauen(mit_panel, panel)
    asyncio.run(werkzeuge.aktualisieren())
    marke = im_hintergrund.set(True)
    try:
        gesperrt = ausfuehren(kasten, "licht_schalten")
        gelesen = ausfuehren(kasten, "wetterstation_lesen")
    finally:
        im_hintergrund.reset(marke)
    assert "NICHT AUSGEFÜHRT" in gesperrt and "nur lesen" in gesperrt
    assert gelesen == "Draussen 7 °C, leichter Regen."
    assert panel.aufrufe == [("wetterstation_lesen", {})]


def test_hintergrund_alles_erlaubt_schreiben(mit_panel, panel):
    e = dataclasses.replace(mit_panel, missa_control_im_hintergrund="alles")
    werkzeuge, kasten = bauen(e, panel)
    asyncio.run(werkzeuge.aktualisieren())
    marke = im_hintergrund.set(True)
    try:
        assert ausfuehren(kasten, "licht_schalten") == "licht_schalten erledigt"
    finally:
        im_hintergrund.reset(marke)


def test_notfall_stopp_sperrt_schreibende_panel_funktionen(mit_panel, panel):
    rechte, _ = rechte_bauen(mit_panel)
    werkzeuge, kasten = bauen(mit_panel, panel, Werkzeugkasten(rechte=rechte))
    asyncio.run(werkzeuge.aktualisieren())
    assert rechte.notfall_ausloesen("Test", "api")
    assert "Notfall-Stopp ist aktiv" in ausfuehren(kasten, "licht_schalten")
    assert ausfuehren(kasten, "wetterstation_lesen") == "Draussen 7 °C, leichter Regen."
    assert panel.aufrufe == [("wetterstation_lesen", {})]


# ---------------------------------------------------------------------- in der App


def test_im_gespraech_nutzt_missa_eine_panel_funktion(app_client, llama: FakeLlama, panel):
    llama.antworten = [
        werkzeug_aufruf("wetterstation_lesen", {}),
        lambda nutzlast: text(f"Laut Station: {nutzlast['messages'][-1]['content']}"),
    ]
    antwort = app_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Wie ist das Wetter draussen?"}]},
    )
    assert antwort.status_code == 200
    assert "7 °C" in antwort.json()["choices"][0]["message"]["content"]
    namen = [t["function"]["name"] for t in llama.anfragen[0]["tools"]]
    assert {"wetter", "wetterstation_lesen", "licht_schalten"} <= set(namen)
    aktionen = app_client.get("/api/aktionen", params={"werkzeug": "wetterstation_lesen"})
    assert aktionen.json()["aktionen"][0]["status"] == "ausgefuehrt"


def test_health_zeigt_panel_status(app_client):
    daten = app_client.get("/health").json()
    assert daten["missa_control"]["verbunden"] is True
    assert daten["missa_control"]["funktionen"] == 2
    assert daten["missa_control"]["letzte_aktualisierung"].startswith("2026-11-09T19:45")
    assert "wetterstation_lesen" in daten["werkzeuge"]


def test_aktualisierung_im_zeitplaner_registriert(app_client):
    auftrag = app_client.app.state.zeitplaner._intervalle["missa-control"]
    assert auftrag.intervall == timedelta(seconds=60)


def test_start_ohne_panel_laeuft_normal(mit_panel, llama, panel):
    panel.erreichbar = False
    app = create_app(
        mit_panel,
        werkzeuge=Werkzeugkasten(),
        llama_transport=llama.transport,
        panel_transport=panel.transport,
        uhr=lambda: JETZT,
    )
    with TestClient(app) as client:
        daten = client.get("/health").json()
        assert client.get("/health").status_code == 200
    assert daten["missa_control"] == {
        "verbunden": False,
        "funktionen": 0,
        "letzte_aktualisierung": None,
        "fehler": daten["missa_control"]["fehler"],
    }
    assert "nicht erreichbar" in daten["missa_control"]["fehler"]


def test_ohne_einstellungen_aendert_sich_nichts(client):
    daten = client.get("/health").json()
    assert "missa_control" not in daten
    assert client.app.state.panel is None
    assert "missa-control" not in client.app.state.zeitplaner._intervalle


def test_eigener_client_versteht_json_und_sse(panel):
    async def ablauf(sse: bool):
        panel.sse = sse
        verbindung = MissaControl(URL, TOKEN, transport=panel.transport)
        try:
            return await verbindung.funktionen(), await verbindung.aufrufen(
                "wetterstation_lesen", {}
            )
        finally:
            await verbindung.schliessen()

    for sse in (False, True):
        funktionen, ergebnis = asyncio.run(ablauf(sse))
        assert [f.name for f in funktionen] == ["wetterstation_lesen", "licht_schalten"]
        assert ergebnis.text == "Draussen 7 °C, leichter Regen."
        assert not ergebnis.fehler


def test_werkzeugfehler_ohne_panel(mit_panel, panel):
    werkzeuge, _ = bauen(mit_panel, panel)
    panel.erreichbar = False
    with pytest.raises(WerkzeugFehler, match="nicht erreichbar"):
        asyncio.run(werkzeuge.aufrufen("x", Stufe.LESEN, {}))


def test_panel_werkzeuge_typ(mit_panel, panel):
    werkzeuge, _ = bauen(mit_panel, panel)
    assert isinstance(werkzeuge, PanelWerkzeuge)
