"""Meldungen an angeschlossene Geräte: Zustellweg „geraet“, Modi, Ruhezeit, Ende zu Ende."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from missa.core.alarme import Alarme
from missa.core.app import create_app
from missa.core.config import Einstellungen, KonfigurationsFehler
from missa.core.geraete import Geraete, GeraeteTokens, GeraeteZustellkanal, meldung_aus_text
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import ZustellFehler, Zustellung
from tests.conftest import API_KEY, JETZT, FakeLlama
from tests.geraet_fake import FakeGeraet, laufender_server, warten

ZONE = JETZT.tzinfo


class FakeTalk:
    """Ersetzt Nextcloud Talk als Zustellkanal."""

    name = "talk"

    def __init__(self, kaputt: bool = False) -> None:
        self.texte: list[str] = []
        self.kaputt = kaputt

    async def senden(self, text: str) -> None:
        if self.kaputt:
            raise ZustellFehler("Talk antwortet nicht")
        self.texte.append(text)


class FakeGeraeteKanal:
    name = "geraet"

    def __init__(self, online: bool = True, kaputt: bool = False) -> None:
        self.online = online
        self.kaputt = kaputt
        self.texte: list[str] = []

    async def senden(self, text: str) -> None:
        if self.kaputt or not self.online:
            raise ZustellFehler("Kein Gerät ist gerade online.")
        self.texte.append(text)


# ----------------------------------------------------------------------------- Text


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        (
            "🚨 NOTFALL: rp01 antwortet nicht\nSeit 5 Minuten kein Ping.",
            {
                "titel": "NOTFALL: rp01 antwortet nicht",
                "text": "Seit 5 Minuten kein Ping.",
                "stufe": "notfall",
            },
        ),
        (
            "⚠️ Wichtig: Platte fast voll",
            {"titel": "Wichtig: Platte fast voll", "text": "", "stufe": "wichtig"},
        ),
        ("ℹ️ Hinweis: Update da", {"titel": "Hinweis: Update da", "text": "", "stufe": "info"}),
        (
            "⏰ Erinnerung: Rail-Kit",
            {"titel": "Erinnerung: Rail-Kit", "text": "", "stufe": "wichtig"},
        ),
        (
            "Guten Morgen!\nHeute 3 Termine.",
            {"titel": "Guten Morgen!", "text": "Heute 3 Termine.", "stufe": "info"},
        ),
        ("", {"titel": "Missa", "text": "", "stufe": "info"}),
    ],
)
def test_meldung_aus_text(text, erwartet):
    assert meldung_aus_text(text) == erwartet


def test_langer_titel_wird_gekuerzt():
    meldung = meldung_aus_text("x" * 300)
    assert len(meldung["titel"]) == 120
    assert meldung["text"] == "x" * 300


# ----------------------------------------------------------------------------- Modi


def zustellung(modus: str, talk: FakeTalk, geraet: FakeGeraeteKanal) -> Zustellung:
    z = Zustellung([talk])
    z.geraet_setzen(geraet, modus)
    return z


def test_zusaetzlich_talk_und_geraet():
    talk, geraet = FakeTalk(), FakeGeraeteKanal()
    z = zustellung("zusaetzlich", talk, geraet)
    assert asyncio.run(z.senden("⏰ Erinnerung: A")) == "talk+geraet"
    assert talk.texte == geraet.texte == ["⏰ Erinnerung: A"]
    assert z.namen == ["talk", "geraet"]


def test_zusaetzlich_geraet_offline_nur_talk():
    talk, geraet = FakeTalk(), FakeGeraeteKanal(online=False)
    z = zustellung("zusaetzlich", talk, geraet)
    assert asyncio.run(z.senden("A")) == "talk"
    assert talk.texte == ["A"] and geraet.texte == []


def test_zusaetzlich_talk_kaputt_geraet_reicht():
    talk, geraet = FakeTalk(kaputt=True), FakeGeraeteKanal()
    z = zustellung("zusaetzlich", talk, geraet)
    # Zugestellt (über das Gerät) – ein neuer Versuch käme sonst doppelt aufs Gerät.
    assert asyncio.run(z.senden("A")) == "geraet"


def test_zusaetzlich_beides_kaputt_ist_ein_fehler():
    z = zustellung("zusaetzlich", FakeTalk(kaputt=True), FakeGeraeteKanal(kaputt=True))
    with pytest.raises(ZustellFehler, match="Talk antwortet nicht"):
        asyncio.run(z.senden("A"))


def test_bevorzugt_nur_geraet_wenn_online():
    talk, geraet = FakeTalk(), FakeGeraeteKanal()
    z = zustellung("bevorzugt", talk, geraet)
    assert asyncio.run(z.senden("A")) == "geraet"
    assert talk.texte == [] and geraet.texte == ["A"]
    geraet.online = False
    assert asyncio.run(z.senden("B")) == "talk"
    assert talk.texte == ["B"] and geraet.texte == ["A"]
    geraet.online, geraet.kaputt = True, True
    assert asyncio.run(z.senden("C")) == "talk"


def test_aus_nie_ans_geraet():
    talk, geraet = FakeTalk(), FakeGeraeteKanal()
    z = zustellung("aus", talk, geraet)
    assert asyncio.run(z.senden("A")) == "talk"
    assert geraet.texte == [] and z.namen == ["talk"]


def test_ohne_geraet_wie_bisher():
    talk = FakeTalk()
    z = Zustellung([talk])
    assert asyncio.run(z.senden("A")) == "talk" and z.namen == ["talk"]
    assert not Zustellung().verfuegbar


def test_benannter_kanal_geht_nur_dorthin():
    talk, geraet = FakeTalk(), FakeGeraeteKanal()
    z = zustellung("zusaetzlich", talk, geraet)
    assert asyncio.run(z.senden("A", "talk")) == "talk"
    assert asyncio.run(z.senden("B", "geraet")) == "geraet"
    assert talk.texte == ["A"] and geraet.texte == ["B"]


def test_unbekannter_modus():
    with pytest.raises(ValueError, match="Modus"):
        Zustellung().geraet_setzen(FakeGeraeteKanal(), "laut")
    e = Einstellungen.aus_umgebung({"MISSA_GERAETE_MELDUNGEN": "Bevorzugt"})
    assert e.geraete_meldungen == "bevorzugt"
    assert Einstellungen.aus_umgebung({}).geraete_meldungen == "zusaetzlich"
    with pytest.raises(KonfigurationsFehler, match="MISSA_GERAETE_MELDUNGEN"):
        dataclasses.replace(e, api_key="x" * 20, geraete_meldungen="laut").pruefen()


# ----------------------------------------------------------------------------- Mit Verwaltung


class Mithoerer:
    """Liest die Meldungen aus dem Stream der Verwaltung."""

    def __init__(self, geraete: Geraete) -> None:
        self.meldungen: list[dict[str, Any]] = []
        self.aufgabe = asyncio.create_task(self._lesen(geraete))

    async def _lesen(self, geraete: Geraete) -> None:
        async for teil in geraete.stream("laptop"):
            if teil.startswith("event: meldung"):
                self.meldungen.append(json.loads(teil.split("data: ", 1)[1]))

    async def stoppen(self) -> None:
        self.aufgabe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.aufgabe


def umgebung(tmp_path: Path, jetzt: datetime):
    tokens = GeraeteTokens(tmp_path / "missa.db")
    tokens.anlegen("laptop")
    geraete = Geraete(tokens, ZONE, offline_nach=0.1)
    talk = FakeTalk()
    z = Zustellung([talk])
    z.geraet_setzen(GeraeteZustellkanal(geraete), "zusaetzlich")
    uhr = [jetzt]
    planer = Zeitplaner(tmp_path / "missa.db", ZONE, z, uhr=lambda: uhr[0])
    alarme = Alarme(tmp_path / "missa.db", ZONE, planer, uhr=lambda: uhr[0])
    return geraete, talk, planer, alarme, uhr


def test_faellige_erinnerung_erreicht_das_geraet(tmp_path):
    async def ablauf():
        geraete, talk, planer, _, _ = umgebung(tmp_path, JETZT)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        hoerer = Mithoerer(geraete)
        planer.anlegen("Rail-Kit einpacken", JETZT - timedelta(seconds=1))
        assert await planer.faellige_zustellen() == 1
        await asyncio.sleep(0.05)
        assert hoerer.meldungen == [
            {"titel": "Erinnerung: Rail-Kit einpacken", "text": "", "stufe": "wichtig"}
        ]
        assert talk.texte == ["⏰ Erinnerung: Rail-Kit einpacken"]
        # Nichts doppelt: einmal zugestellt ist zugestellt.
        assert await planer.faellige_zustellen() == 0
        await asyncio.sleep(0.05)
        assert len(hoerer.meldungen) == 1 and len(talk.texte) == 1
        await hoerer.stoppen()
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_geraet_offline_kommt_sie_ueber_talk(tmp_path):
    async def ablauf():
        geraete, talk, planer, _, _ = umgebung(tmp_path, JETZT)
        planer.anlegen("Rail-Kit einpacken", JETZT - timedelta(seconds=1))
        assert await planer.faellige_zustellen() == 1
        assert talk.texte == ["⏰ Erinnerung: Rail-Kit einpacken"]
        await geraete.schliessen()

    asyncio.run(ablauf())


def test_in_der_ruhezeit_nur_notfaelle(tmp_path):
    async def ablauf():
        nachts = datetime(2026, 11, 9, 23, 30, tzinfo=ZONE)
        geraete, talk, planer, alarme, uhr = umgebung(tmp_path, nachts)
        geraete.anmelden("laptop", protokoll=1, geraet_name="laptop")
        hoerer = Mithoerer(geraete)
        alarme.melden("info", "Update verfügbar", schluessel="a")
        alarme.melden("wichtig", "Platte zu 90 % voll", schluessel="b")
        alarme.melden("notfall", "rp01 antwortet nicht", schluessel="c")
        await planer.faellige_zustellen()
        await asyncio.sleep(0.05)
        assert [m["stufe"] for m in hoerer.meldungen] == ["notfall"]
        assert hoerer.meldungen[0]["titel"] == "NOTFALL: rp01 antwortet nicht"
        assert len(talk.texte) == 1
        # Am Morgen kommt der Rest gesammelt – einmal, auf beiden Wegen.
        uhr[0] = datetime(2026, 11, 10, 7, 1, tzinfo=ZONE)
        await planer.faellige_zustellen()
        await asyncio.sleep(0.05)
        assert len(hoerer.meldungen) == 2 and len(talk.texte) == 2
        assert "Platte" in hoerer.meldungen[1]["text"]
        await hoerer.stoppen()
        await geraete.schliessen()

    asyncio.run(ablauf())


# ----------------------------------------------------------------------------- Ende zu Ende


@pytest.mark.parametrize("modus", ["zusaetzlich", "bevorzugt", "aus"])
def test_ende_zu_ende_mit_fake_geraet(einstellungen: Einstellungen, llama: FakeLlama, modus):
    mit = dataclasses.replace(einstellungen, geraete_aktiv=True, geraete_meldungen=modus)
    token = GeraeteTokens(mit.datenbank).anlegen("laptop")
    talk = FakeTalk()
    app = create_app(
        mit, llama_transport=llama.transport, zustellung=Zustellung([talk]), uhr=lambda: JETZT
    )
    app.state.geraete.ping_sekunden = 0.2

    @app.post("/test/zustellen")
    async def zustellen(daten: dict) -> dict:
        # Der Zeitplaner des Dienstes läuft mit der echten Uhr (und stellt selbst zu).
        planer = app.state.zeitplaner
        planer.anlegen(daten["text"], datetime.now(ZONE))
        await planer.faellige_zustellen()
        return {}

    with laufender_server(app) as basis:
        geraet = FakeGeraet(basis, token)
        try:
            geraet.anmelden()
            geraet.verbinden()
            with httpx.Client(base_url=basis, trust_env=False, timeout=10) as api:
                api.headers["Authorization"] = f"Bearer {API_KEY}"
                api.post("/test/zustellen", json={"text": "Zahnarzt"})
                if modus == "aus":
                    warten(lambda: talk.texte)
                    assert api.get("/api/erinnerungen").json()["kanaele"] == ["talk"]
                else:
                    warten(lambda: geraet.meldungen)
                    assert geraet.meldungen == [
                        {"titel": "Erinnerung: Zahnarzt", "text": "", "stufe": "wichtig"}
                    ]
                    assert api.get("/api/erinnerungen").json()["kanaele"] == ["talk", "geraet"]
                if modus != "bevorzugt":
                    warten(lambda: talk.texte)
                assert talk.texte == ([] if modus == "bevorzugt" else ["⏰ Erinnerung: Zahnarzt"])
        finally:
            geraet.schliessen()
    assert modus != "aus" or geraet.meldungen == []
