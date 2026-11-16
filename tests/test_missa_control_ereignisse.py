"""Gruppe 3: Ereignisse aus Missa Control von sich aus melden (mit Nachbau des Panels)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.config import Einstellungen
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.missa_control import MissaControl, PanelNichtErreichbar
from missa.tools.panel_ereignisse import ERSTE_WARTEZEIT, FREIGABE_HINWEIS, PanelEreignisse
from tests.conftest import JETZT
from tests.missa_control_fake import TOKEN, URL, FakePanel

ZONE = ZoneInfo("Europe/Zurich")
NACHMITTAG = datetime(2026, 11, 16, 14, 0, tzinfo=ZONE)
NACHT = datetime(2026, 11, 16, 23, 30, tzinfo=ZONE)


class TalkNachbau:
    """Ein Zustellkanal wie Nextcloud Talk, der sich die Nachrichten merkt."""

    name = "talk"

    def __init__(self) -> None:
        self.nachrichten: list[str] = []

    async def senden(self, text: str) -> None:
        self.nachrichten.append(text)


class Uhr:
    def __init__(self, jetzt: datetime) -> None:
        self.jetzt = jetzt

    def __call__(self) -> datetime:
        return self.jetzt


@pytest.fixture
def panel() -> FakePanel:
    return FakePanel()


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(NACHMITTAG)


@pytest.fixture
def talk() -> TalkNachbau:
    return TalkNachbau()


@pytest.fixture
def umgebung(tmp_path, panel, uhr, talk):
    datei = tmp_path / "missa.db"
    planer = Zeitplaner(datei, ZONE, Zustellung([talk]), uhr=uhr)
    alarme = Alarme(datei, ZONE, planer, uhr=uhr)
    verbindung = MissaControl(URL, TOKEN, transport=panel.transport)
    return datei, planer, alarme, verbindung


def melder(umgebung, **weitere) -> PanelEreignisse:
    datei, _, alarme, verbindung = umgebung
    return PanelEreignisse(verbindung, alarme, datei, **weitere)


def zugestellt(umgebung) -> list[str]:
    """Stellt alles Fällige zu und liefert, was in Talk angekommen ist."""
    _, planer, _, _ = umgebung
    asyncio.run(planer.faellige_zustellen())
    return planer.zustellung._kanaele[0].nachrichten


def einmal(m: PanelEreignisse) -> None:
    asyncio.run(m.einmal())


def gestartet(m: PanelEreignisse, panel: FakePanel, nach: int = 0) -> None:
    """Merkt eine Startnummer, als wäre der erste Start schon vorbei."""
    m._merken(nach)


# ---------------------------------------------------------------------- Zustellung


@pytest.mark.parametrize(
    ("dringlichkeit", "stufe"),
    [("normal", AlarmStufe.INFO), ("hoch", AlarmStufe.WICHTIG), ("dringend", AlarmStufe.NOTFALL)],
)
def test_zustellung_je_dringlichkeit(umgebung, panel, dringlichkeit, stufe):
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(dringlichkeit=dringlichkeit)
    einmal(m)
    _, _, alarme, _ = umgebung
    [alarm] = alarme.liste()
    assert alarm.stufe is stufe
    assert alarm.quelle == "Missa Control/Regeln"
    [nachricht] = zugestellt(umgebung)
    # Wörtlich aus titel und nachricht – ohne Sprachmodell.
    assert "Regel „CPU-Alarm“ ausgelöst" in nachricht
    assert "Die CPU von ai01 liegt seit 5 Minuten über 90 %." in nachricht
    assert nachricht.startswith(stufe.symbol)


def test_niedrig_nur_ins_log(umgebung, panel, caplog):
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(art="geraet", titel="Knopf gedrückt", dringlichkeit="niedrig")
    with caplog.at_level(logging.INFO):
        einmal(m)
    assert umgebung[2].liste() == []
    assert zugestellt(umgebung) == []
    assert "Knopf gedrückt" in caplog.text
    assert m.letzte_id() == 1


def test_ruhezeit_schiebt_auf_notfall_nicht(umgebung, panel, uhr):
    uhr.jetzt = NACHT
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(
        art="integration_ausgefallen",
        titel="Home Assistant fällt aus",
        dringlichkeit="hoch",
        quelle="Home Assistant",
    )
    panel.ereignis(art="regel", titel="Wasser im Keller", dringlichkeit="dringend")
    einmal(m)
    alarme = {a.titel: a for a in umgebung[2].liste()}
    assert alarme["Home Assistant fällt aus"].status == "aufgeschoben"
    assert alarme["Wasser im Keller"].status == "gesendet"
    [sofort] = zugestellt(umgebung)
    assert "Wasser im Keller" in sofort


def test_ruhefenster_gleiches_ereignis_nicht_wiederholt(umgebung, panel):
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis()
    panel.ereignis()
    panel.ereignis(art="integration_wieder_da", titel="Home Assistant ist wieder da")
    einmal(m)
    assert len(umgebung[2].liste()) == 2
    assert m.letzte_id() == 3


def test_freigabe_sofort_mit_hinweis(umgebung, panel):
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(
        art="freigabe",
        titel="Freigabe: licht_schalten",
        nachricht="Missa möchte „LED“ am Gerät „Wohnzimmer“ einschalten.",
        quelle="Gateway",
        freigabe_id=7,
    )
    einmal(m)
    [nachricht] = zugestellt(umgebung)
    assert "Freigabe: licht_schalten" in nachricht
    assert "Missa möchte „LED“ am Gerät „Wohnzimmer“ einschalten." in nachricht
    assert FREIGABE_HINWEIS in nachricht


def test_freigabe_in_ruhezeit_nur_im_log(umgebung, panel, uhr, caplog):
    uhr.jetzt = NACHT
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(art="freigabe", titel="Freigabe: licht_schalten", nachricht="…")
    with caplog.at_level(logging.INFO):
        einmal(m)
    assert umgebung[2].liste() == []
    assert "in der Ruhezeit nicht gemeldet" in caplog.text


def test_zwei_freigaben_kommen_beide(umgebung, panel):
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(art="freigabe", titel="Freigabe: licht_schalten", freigabe_id=1)
    panel.ereignis(art="freigabe", titel="Freigabe: licht_schalten", freigabe_id=2)
    einmal(m)
    assert len(zugestellt(umgebung)) == 2


def test_test_ereignis_kommt_als_bestaetigung_in_talk_an(umgebung, panel, uhr):
    uhr.jetzt = NACHT  # auch in der Ruhezeit: du hast den Test gerade selbst ausgelöst
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(
        art="test", titel="Test-Ereignis", nachricht="Das ist ein Test-Ereignis aus dem Panel."
    )
    einmal(m)
    [nachricht] = zugestellt(umgebung)
    assert nachricht.startswith("✅ Test-Ereignis aus Missa Control ist angekommen.")
    assert "Das ist ein Test-Ereignis aus dem Panel." in nachricht


# ---------------------------------------------------------------------- Start, Abbruch, Neustart


def test_erster_start_beginnt_bei_jetzt(umgebung, panel):
    for _ in range(3):
        panel.ereignis()
    m = melder(umgebung)
    assert m.letzte_id() is None
    einmal(m)
    assert umgebung[2].liste() == []  # alte Ereignisse werden nicht nachgeliefert
    assert m.letzte_id() == 3
    kopf, nach = panel.stream_anfragen[-1]
    assert nach == 3
    assert kopf["Last-Event-ID"] == "3"
    assert kopf["Authorization"] == f"Bearer {TOKEN}"


def test_erster_start_ohne_ereignisse_merkt_null(umgebung, panel):
    m = melder(umgebung)
    einmal(m)
    assert m.letzte_id() == 0
    panel.ereignis()
    einmal(m)
    assert len(umgebung[2].liste()) == 1


def test_fortsetzen_nach_abbruch_ohne_doppelte(umgebung, panel):
    m = melder(umgebung)
    gestartet(m, panel)
    for nummer in range(3):
        panel.ereignis(titel=f"Regel {nummer} ausgelöst")
    panel.stream_abbruch_nach = 2
    with pytest.raises(PanelNichtErreichbar):
        einmal(m)
    assert m.letzte_id() == 2
    panel.stream_abbruch_nach = None
    einmal(m)
    assert panel.stream_anfragen[-1][1] == 2
    titel = sorted(a.titel for a in umgebung[2].liste())
    assert titel == ["Regel 0 ausgelöst", "Regel 1 ausgelöst", "Regel 2 ausgelöst"]


def test_nach_neustart_nichts_doppelt_nichts_verloren(umgebung, panel):
    m = melder(umgebung)
    gestartet(m, panel)
    panel.ereignis(titel="Vor dem Neustart")
    einmal(m)
    # Neustart von Missa-Core: neues Objekt, gleiche Datenbank; dazwischen kam etwas Neues.
    panel.ereignis(titel="Während Missa weg war")
    neu = melder(umgebung)
    einmal(neu)
    einmal(neu)  # auch ein Neustart des Panels liefert nichts doppelt
    titel = sorted(a.titel for a in umgebung[2].liste())
    assert titel == ["Vor dem Neustart", "Während Missa weg war"]


def test_wachsende_wartezeit_beim_neuverbinden(umgebung, panel):
    panel.erreichbar = False
    gewartet: list[float] = []

    async def schlafen(sekunden: float) -> None:
        gewartet.append(sekunden)
        if len(gewartet) >= 5:
            raise asyncio.CancelledError

    m = melder(umgebung, schlafen=schlafen)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m.laufen())
    assert gewartet == [ERSTE_WARTEZEIT * 2**i for i in range(5)]


def test_token_falsch_klare_meldung(umgebung, panel, caplog):
    panel.token = "anderer"

    async def schlafen(_sekunden: float) -> None:
        raise asyncio.CancelledError

    m = melder(umgebung, schlafen=schlafen)
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        asyncio.run(m.laufen())
    assert "Token ungültig (401)" in caplog.text
    assert "Clients" in caplog.text


# ---------------------------------------------------------------------- in der App


@pytest.fixture
def mit_panel(einstellungen: Einstellungen) -> Einstellungen:
    return dataclasses.replace(einstellungen, missa_control_url=URL, missa_control_token=TOKEN)


def test_einstellung_aus_umgebung():
    assert Einstellungen.aus_umgebung({}).missa_control_ereignisse is True
    aus = Einstellungen.aus_umgebung({"MISSA_CONTROL_EREIGNISSE": "aus"})
    assert aus.missa_control_ereignisse is False


def test_ereignisse_laufen_in_der_app(mit_panel, llama, panel):
    from missa.core.app import create_app

    talk = TalkNachbau()
    panel.ereignis(titel="Altes Ereignis")
    app = create_app(
        mit_panel,
        llama_transport=llama.transport,
        panel_transport=panel.transport,
        zustellung=Zustellung([talk]),
        uhr=lambda: JETZT,
    )
    with TestClient(app) as client:
        melder_app = client.app.state.panel_ereignisse
        assert melder_app is not None
        for _ in range(50):
            if melder_app.letzte_id() is not None and panel.stream_anfragen:
                break
            asyncio.run(asyncio.sleep(0.02))
        status = client.get("/health").json()["missa_control"]["ereignisse"]
    assert status["letzte_id"] == 1
    assert panel.stream_anfragen[0][1] == 1


def test_ereignisse_aus(mit_panel, llama, panel):
    from missa.core.app import create_app

    e = dataclasses.replace(mit_panel, missa_control_ereignisse=False)
    app = create_app(e, llama_transport=llama.transport, panel_transport=panel.transport)
    with TestClient(app) as client:
        assert client.app.state.panel_ereignisse is None
        assert client.get("/health").json()["missa_control"]["ereignisse"] == "aus"
    assert panel.stream_anfragen == []
