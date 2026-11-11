"""Alarm-Stufen, Ruhezeit, Ruhefenster und regelmäßige Aufträge im Zeitplaner."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from missa.core.alarme import Alarme, AlarmStufe, alarm_werkzeuge, stufe_verstehen
from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def planer(tmp_path: Path, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def alarme(planer) -> Alarme:
    return Alarme(planer.datei, ZONE, planer)


@pytest.mark.parametrize(
    ("text", "stufe"),
    [
        ("notfall", AlarmStufe.NOTFALL),
        ("Ruf mich an", AlarmStufe.NOTFALL),
        ("anrufen", AlarmStufe.NOTFALL),
        ("weck mich", AlarmStufe.NOTFALL),
        ("wichtig", AlarmStufe.WICHTIG),
        ("Warnung", AlarmStufe.WICHTIG),
        ("info", AlarmStufe.INFO),
        ("nur ein Hinweis", AlarmStufe.INFO),
    ],
)
def test_stufe_verstehen(text, stufe):
    assert stufe_verstehen(text) is stufe


def test_unbekannte_stufe():
    with pytest.raises(WerkzeugFehler, match="Alarm-Stufe"):
        stufe_verstehen("irgendwann")


def test_notfall_kommt_sofort_und_markiert(alarme, planer, kanal):
    alarm = alarme.melden("notfall", "ai01: CPU über 90 %", "seit 14:00", schluessel="x")
    assert alarm is not None and alarm.stufe is AlarmStufe.NOTFALL
    assert asyncio.run(planer.faellige_zustellen()) == 1
    assert kanal.nachrichten[0].startswith("🚨 NOTFALL: ai01: CPU über 90 %")
    assert "seit 14:00" in kanal.nachrichten[0]


def test_gleicher_alarm_nicht_im_minutentakt(alarme, planer, kanal, uhr):
    assert alarme.melden("wichtig", "Disk voll", schluessel="disk") is not None
    for _ in range(10):
        uhr.vor(minutes=1)
        assert alarme.melden("wichtig", "Disk voll", schluessel="disk") is None
    asyncio.run(planer.faellige_zustellen())
    assert len(kanal.nachrichten) == 1
    assert alarme.unterdrueckt == 10
    uhr.vor(minutes=51)  # 61 Minuten nach dem ersten: Ruhefenster vorbei
    assert alarme.melden("wichtig", "Disk voll", schluessel="disk") is not None


def test_dringender_geht_durch_das_ruhefenster(alarme):
    assert alarme.melden("info", "auffällig", schluessel="a") is not None
    assert alarme.melden("info", "auffällig", schluessel="a") is None
    assert alarme.melden("wichtig", "sehr auffällig", schluessel="a") is not None
    assert alarme.melden("notfall", "kritisch", schluessel="a") is not None


def test_ruhezeit_schiebt_auf_ausser_notfall(alarme, planer, kanal, uhr):
    uhr.jetzt = datetime(2026, 11, 11, 23, 30, tzinfo=ZONE)
    wichtig = alarme.melden("wichtig", "git nicht erreichbar", schluessel="w")
    notfall = alarme.melden("notfall", "ai01 brennt", schluessel="n")
    assert wichtig.status == "aufgeschoben"
    assert wichtig.zustellen_ab == datetime(2026, 11, 12, 7, 0, tzinfo=ZONE)
    assert notfall.status == "gesendet"
    asyncio.run(planer.faellige_zustellen())
    assert len(kanal.nachrichten) == 1 and "ai01 brennt" in kanal.nachrichten[0]
    uhr.jetzt = datetime(2026, 11, 12, 7, 0, 30, tzinfo=ZONE)
    asyncio.run(planer.faellige_zustellen())
    assert "git nicht erreichbar" in kanal.nachrichten[1]
    assert "Ruhezeit um 23:30" in kanal.nachrichten[1]


def test_ruhezeit_einstellen_bleibt_gespeichert(alarme, planer):
    alarme.ruhezeit_einstellen(time(23, 0), time(6, 30))
    neu = Alarme(planer.datei, ZONE, planer)
    assert neu.ruhezeit() == (time(23, 0), time(6, 30))
    neu.ruhezeit_einstellen(None, None)
    assert Alarme(planer.datei, ZONE, planer).ruhezeit() is None
    assert not neu.in_ruhezeit(datetime(2026, 11, 11, 3, 0, tzinfo=ZONE))


def test_werkzeuge_aendern_ruhezeit_und_ruhefenster(alarme):
    kasten = Werkzeugkasten(alarm_werkzeuge(alarme))
    antwort = asyncio.run(
        kasten.ausfuehren(
            "alarm_einstellungen",
            {"ruhezeit_von": "21:30", "ruhezeit_bis": "6", "wiederholen_nach_minuten": 120},
        )
    )
    assert "21:30–06:00" in antwort and "120 Minuten" in antwort
    assert alarme.ruhefenster() == timedelta(minutes=120)
    alarme.melden("info", "Testalarm", schluessel="t")
    liste = asyncio.run(kasten.ausfuehren("alarme_anzeigen", {}))
    assert "[info] Testalarm" in liste
    aus = asyncio.run(kasten.ausfuehren("alarm_einstellungen", {"ruhezeit_aus": True}))
    assert "keine Ruhezeit" in aus


def test_regelmaessiger_auftrag_laeuft_im_takt_und_ueberlebt_neustart(planer, uhr):
    laeufe: list[datetime] = []

    async def messen() -> None:
        laeufe.append(uhr.jetzt)

    planer.regelmaessig("messen", timedelta(minutes=1), messen)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == ["messen"]
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == []
    uhr.vor(seconds=59)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == []
    uhr.vor(seconds=1)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == ["messen"]
    assert len(laeufe) == 2

    # Neustart: Der letzte Lauf steht in der Datenbank – nicht sofort wieder laufen.
    neu = Zeitplaner(planer.datei, ZONE, Zustellung(), uhr=uhr)
    neu.regelmaessig("messen", timedelta(minutes=1), messen)
    uhr.vor(seconds=30)
    assert asyncio.run(neu.auftraege_ausfuehren(warten=True)) == []
    uhr.vor(seconds=30)
    assert asyncio.run(neu.auftraege_ausfuehren(warten=True)) == ["messen"]


def test_zu_kurzes_intervall_wird_abgelehnt(planer):
    with pytest.raises(ValueError):
        planer.regelmaessig("zu-oft", timedelta(seconds=1), lambda: None)


def test_fehlerhafter_auftrag_stoppt_die_schleife_nicht(planer, uhr):
    def kaputt() -> None:
        raise RuntimeError("Proxmox weg")

    planer.regelmaessig("kaputt", timedelta(minutes=1), kaputt)
    asyncio.run(planer.auftraege_ausfuehren(warten=True))
    uhr.vor(minutes=1)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == ["kaputt"]
