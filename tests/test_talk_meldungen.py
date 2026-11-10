"""Missa schreibt von sich aus in Talk: Werkzeug talk_nachricht_senden mit Rate-Limit."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from missa.channels.nextcloud_talk import MAX_LAENGE, TalkClient
from missa.core.app import create_app
from missa.core.config import Einstellungen
from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten
from missa.tools import werkzeuge_laden
from missa.tools.talk import Ratenbegrenzer, TalkMelder, talk_werkzeuge
from tests.conftest import API_KEY, JETZT, text, werkzeug_aufruf
from tests.test_nextcloud_talk import BASIS, SECRET, FakeNextcloud

ZIEL = "missa123"


@pytest.fixture
def nextcloud() -> FakeNextcloud:
    return FakeNextcloud()


class Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


def melder(nextcloud: FakeNextcloud, token: str = ZIEL, rate: int = 10, uhr=None) -> TalkMelder:
    client = TalkClient(BASIS, SECRET, transport=nextcloud.transport)
    begrenzer = Ratenbegrenzer(rate, uhr) if uhr else Ratenbegrenzer(rate)
    return TalkMelder(client, token, begrenzer)


def senden(m: TalkMelder, inhalt: str) -> str:
    return asyncio.run(m.senden(inhalt))


# ---------------------------------------------------------------------- Senden


def test_schreibt_in_die_eingestellte_unterhaltung(nextcloud):
    ergebnis = senden(melder(nextcloud), "Erinnerung: In 15 Minuten Zahnarzt.")
    assert ergebnis == "Nachricht in Talk gesendet."
    [anfrage] = nextcloud.anfragen
    assert anfrage["methode"] == "POST"
    assert anfrage["pfad"] == f"/ocs/v2.php/apps/spreed/api/v1/bot/{ZIEL}/message"
    assert anfrage["signatur_ok"]
    assert anfrage["daten"]["message"] == "Erinnerung: In 15 Minuten Zahnarzt."
    assert "replyTo" not in anfrage["daten"]


def test_das_ziel_ist_kein_parameter(nextcloud):
    """Das Modell kann keine andere Unterhaltung wählen."""
    [werkzeug] = talk_werkzeuge(melder(nextcloud))
    assert werkzeug.name == "talk_nachricht_senden"
    assert set(werkzeug.parameter["properties"]) == {"text"}
    ergebnis = asyncio.run(
        Werkzeugkasten([werkzeug]).ausfuehren(
            "talk_nachricht_senden", {"text": "Hi", "token": "andere-unterhaltung"}
        )
    )
    assert ergebnis.startswith("Fehler: Falsche Parameter")
    assert nextcloud.anfragen == []


def test_lange_nachricht_wird_aufgeteilt(nextcloud):
    ergebnis = senden(melder(nextcloud), "Wort " * 20_000)
    nachrichten = nextcloud.nachrichten()
    assert len(nachrichten) >= 2
    assert all(len(n["daten"]["message"]) <= MAX_LAENGE for n in nachrichten)
    assert ergebnis == f"Nachricht in Talk gesendet ({len(nachrichten)} Teile)."


def test_leere_nachricht(nextcloud):
    with pytest.raises(WerkzeugFehler, match="leer"):
        senden(melder(nextcloud), "   ")
    assert nextcloud.anfragen == []


def test_nextcloud_lehnt_ab_gibt_verstaendlichen_fehler():
    falsch = FakeNextcloud()
    client = TalkClient(BASIS, "falsches-secret-" + "z" * 40, transport=falsch.transport)
    with pytest.raises(WerkzeugFehler, match="konnte nicht gesendet werden"):
        asyncio.run(TalkMelder(client, ZIEL, Ratenbegrenzer(10)).senden("Hallo"))


# ---------------------------------------------------------------------- ohne Token


def test_ohne_token_passiert_nichts_mit_klarer_meldung(nextcloud):
    with pytest.raises(WerkzeugFehler, match="NC_TALK_NOTIFY_CONVERSATION ist leer"):
        senden(melder(nextcloud, token=""), "Hallo")
    assert nextcloud.anfragen == []


def test_ohne_talk_gibt_es_kein_werkzeug():
    assert "talk_nachricht_senden" not in [w.name for w in werkzeuge_laden(Einstellungen())]


def test_mit_talk_aber_ohne_token_klare_meldung_im_werkzeug(nextcloud, caplog):
    client = TalkClient(BASIS, SECRET, transport=nextcloud.transport)
    with caplog.at_level("INFO"):
        kasten = werkzeuge_laden(Einstellungen(), talk_client=client)
    assert "NC_TALK_NOTIFY_CONVERSATION ist leer" in caplog.text
    ergebnis = asyncio.run(kasten.ausfuehren("talk_nachricht_senden", {"text": "Hallo"}))
    assert ergebnis.startswith("Fehler: Es ist keine Talk-Unterhaltung")
    assert nextcloud.anfragen == []


# ---------------------------------------------------------------------- Rate-Limit


def test_rate_limit_greift(nextcloud):
    uhr = Uhr()
    m = melder(nextcloud, uhr=uhr)
    for nummer in range(10):
        senden(m, f"Nachricht {nummer}")
    with pytest.raises(WerkzeugFehler, match="höchstens 10 Talk-Nachrichten pro Minute"):
        senden(m, "Nummer 11")
    assert len(nextcloud.nachrichten()) == 10

    uhr.jetzt += 30
    with pytest.raises(WerkzeugFehler, match="in etwa 30 Sekunden"):
        senden(m, "immer noch zu früh")
    uhr.jetzt += 30
    senden(m, "Jetzt wieder erlaubt")
    assert len(nextcloud.nachrichten()) == 11


def test_rate_limit_einstellbar(nextcloud):
    m = melder(nextcloud, rate=2, uhr=Uhr())
    senden(m, "eins")
    senden(m, "zwei")
    with pytest.raises(WerkzeugFehler, match="höchstens 2"):
        senden(m, "drei")


def test_aufgeteilte_nachricht_zaehlt_jeden_teil(nextcloud):
    m = melder(nextcloud, rate=2, uhr=Uhr())
    with pytest.raises(WerkzeugFehler, match="höchstens 2"):
        senden(m, "x " * 50_000)  # wären 4 Teile
    assert nextcloud.anfragen == []


def test_ratenbegrenzer():
    uhr = Uhr()
    begrenzer = Ratenbegrenzer(3, uhr)
    assert all(begrenzer.versuchen() for _ in range(3))
    assert not begrenzer.versuchen()
    assert begrenzer.wartezeit() == 60
    uhr.jetzt += 60
    assert begrenzer.versuchen(3)


def test_einstellungen_aus_umgebung():
    e = Einstellungen.aus_umgebung(
        {"NC_TALK_NOTIFY_CONVERSATION": " missa123 ", "NC_TALK_NOTIFY_RATE": "5"}
    )
    assert e.nc_talk_notify_conversation == "missa123"
    assert e.nc_talk_notify_rate == 5
    assert Einstellungen.aus_umgebung({}).nc_talk_notify_rate == 10


# ---------------------------------------------------------------------- über die API


def test_missa_schreibt_ueber_das_werkzeug_in_talk(einstellungen, llama, nextcloud):
    """Z. B. ein Cronjob bittet Missa per API um eine Erinnerung – sie schreibt sie in Talk."""
    talk = replace(
        einstellungen,
        nc_talk_enabled=True,
        nc_talk_base_url=BASIS,
        nc_talk_bot_secret=SECRET,
        nc_talk_allowed_users=("louis",),
        nc_talk_notify_conversation=ZIEL,
    )
    llama.antworten = [
        werkzeug_aufruf("talk_nachricht_senden", {"text": "⏰ In 15 Minuten: Zahnarzt."}),
        text("Erledigt, ich habe Louis in Talk erinnert."),
    ]
    app = create_app(
        talk, llama_transport=llama.transport, talk_transport=nextcloud.transport, uhr=lambda: JETZT
    )
    with TestClient(app) as client:
        antwort = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "messages": [
                    {"role": "user", "content": "Erinnere Louis per Talk an den Zahnarzt."}
                ]
            },
        )
    assert antwort.status_code == 200
    assert "talk_nachricht_senden" in [t["function"]["name"] for t in llama.anfragen[0]["tools"]]
    assert llama.anfragen[1]["messages"][-1]["content"] == "Nachricht in Talk gesendet."
    [gesendet] = nextcloud.nachrichten()
    assert gesendet["pfad"].endswith(f"/bot/{ZIEL}/message")
    assert gesendet["daten"]["message"] == "⏰ In 15 Minuten: Zahnarzt."
    assert gesendet["signatur_ok"]
