"""Erreichbarkeits-Prüfungen als Tabelle: Startwert, Werkzeuge, eigenes Intervall, Pause."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.config import Einstellungen
from missa.core.datenbank import stand_lesen, verbinden
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.erreichbarkeit import ErreichbarkeitsPruefer, PruefZiel
from missa.tools.erreichbarkeit import (
    MAX_PRUEFUNGEN,
    MERKER_STARTWERT,
    SCHEMA,
    ErreichbarkeitsWaechter,
    erreichbarkeit_werkzeuge,
)
from missa.tools.zertifikate import ZertifikatsWaechter
from tests.test_erreichbarkeit import DOMAINS, Netz
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr


@pytest.fixture
def netz() -> Netz:
    return Netz()


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))


@pytest.fixture
def planer(tmp_path: Path, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)


def bauen(netz: Netz, planer: Zeitplaner, startwert=(), **extra) -> ErreichbarkeitsWaechter:
    return ErreichbarkeitsWaechter(
        netz.pruefer(()),
        Alarme(planer.datei, ZONE, planer),
        planer.datei,
        ZONE,
        startwert=startwert,
        **extra,
    )


@pytest.fixture
def waechter(netz, planer) -> ErreichbarkeitsWaechter:
    return bauen(netz, planer, DOMAINS)


@pytest.fixture
def kasten(waechter) -> Werkzeugkasten:
    return Werkzeugkasten(erreichbarkeit_werkzeuge(waechter))


def ausfuehren(kasten: Werkzeugkasten, name: str, argumente: dict, **extra) -> str:
    return asyncio.run(kasten.ausfuehren(name, argumente, **extra))


def ticken(waechter: ErreichbarkeitsWaechter, uhr: Uhr, minuten: int = 1) -> list:
    """Wie der Zeitplaner: jede Minute einmal ``pruefen``."""
    gemeldet = []
    for _ in range(minuten):
        gemeldet += asyncio.run(waechter.pruefen())
        uhr.vor(minutes=1)
    return gemeldet


# ----------------------------------------------------------------------------- Startwert


def test_startwert_wird_einmal_uebernommen(netz, planer, caplog):
    with caplog.at_level(logging.INFO, logger="missa.tools.erreichbarkeit"):
        waechter = bauen(netz, planer, DOMAINS, intervall_minuten=7, stufe=AlarmStufe.NOTFALL)
    assert [p.schluessel for p in waechter.pruefungen()] == list(DOMAINS)
    assert {(p.intervall_minuten, p.stufe) for p in waechter.pruefungen()} == {
        (7, AlarmStufe.NOTFALL)
    }
    assert "3 Prüfung(en) aus ERREICHBARKEIT_DOMAINS übernommen" in caplog.text
    assert stand_lesen(planer.datei, MERKER_STARTWERT) is not None

    # Neustart mit einem zusätzlichen Eintrag in der env: nichts wird übernommen, nur ein
    # Hinweis im Log – und der nur einmal, nicht bei jedem Start.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="missa.tools.erreichbarkeit"):
        neu = bauen(netz, planer, (*DOMAINS, "neu.x.test:8443"))
        bauen(netz, planer, (*DOMAINS, "neu.x.test:8443"))
    assert len(neu.pruefungen()) == 3
    assert caplog.text.count("neu.x.test:8443") == 1


def test_nach_loeschen_aller_pruefungen_kein_neuer_startwert(waechter, netz, planer):
    for pruefung in waechter.pruefungen():
        waechter.loeschen(pruefung)
    assert bauen(netz, planer, DOMAINS).pruefungen() == []


def test_ohne_env_kommt_der_startwert_spaeter(netz, planer):
    leer = bauen(netz, planer, ())
    assert leer.pruefungen() == []
    assert stand_lesen(planer.datei, MERKER_STARTWERT) is None
    # Noch nie übernommen und Tabelle leer: der erste Start mit Einträgen gilt als Startwert.
    assert len(bauen(netz, planer, DOMAINS[:1]).pruefungen()) == 1


def test_im_chat_angelegt_dann_kein_startwert_mehr(netz, planer):
    leer = bauen(netz, planer, ())
    pruefung, _ = leer.anlegen(PruefZiel("eigen.x.test"))
    leer.loeschen(pruefung)
    assert bauen(netz, planer, DOMAINS).pruefungen() == []


def test_bestehender_zustand_bleibt_beim_update_erhalten(netz, planer, uhr):
    """Altes System: nur Tabelle ``erreichbarkeit`` mit gemeldetem Ausfall, noch keine Prüfungen."""
    alt = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).isoformat()
    with verbinden(planer.datei) as db:
        db.execute(
            "CREATE TABLE erreichbarkeit (domain TEXT PRIMARY KEY, status TEXT NOT NULL CHECK "
            "(status IN ('ok', 'gestoert')), seit TEXT NOT NULL, fehler_in_folge INTEGER NOT "
            "NULL DEFAULT 0, gemeldet INTEGER NOT NULL DEFAULT 0, letzte_pruefung TEXT NOT NULL, "
            "problem TEXT NOT NULL DEFAULT '', adressen TEXT NOT NULL DEFAULT '', http_status "
            "INTEGER, zertifikat_bis TEXT)"
        )
        db.execute(
            "INSERT INTO erreichbarkeit (domain, status, seit, fehler_in_folge, gemeldet, "
            "letzte_pruefung, problem) VALUES (?, 'gestoert', ?, 5, 1, ?, 'HTTPS 502')",
            (DOMAINS[1], alt, alt),
        )
    netz.http_status[DOMAINS[1]] = 502
    waechter = bauen(netz, planer, DOMAINS)
    assert len(waechter.pruefungen()) == 3
    assert waechter.zustand()[DOMAINS[1]]["gemeldet"] == 1
    assert ticken(waechter, uhr, 6) == []  # schon gemeldet – nicht noch einmal
    netz.http_status.clear()
    assert [a.titel for a in ticken(waechter, uhr, 6)] == [f"{DOMAINS[1]} ist wieder erreichbar"]


def test_erreichbarkeit_bauen_nimmt_env_als_startwert(tmp_path, planer):
    from missa.tools.homelab_bauen import erreichbarkeit_bauen

    einstellungen = Einstellungen.aus_umgebung(
        {
            "MISSA_DATEN": str(tmp_path),
            "ERREICHBARKEIT_DOMAINS": "cloud.x.test:8443,git.x.test",
            "ERREICHBARKEIT_INTERVALL_MINUTEN": "10",
            "ERREICHBARKEIT_STUFE": "info",
        }
    )
    waechter = erreichbarkeit_bauen(einstellungen, Alarme(einstellungen.datenbank, ZONE, planer))
    assert [p.zeile() for p in waechter.pruefungen()] == [
        "#1: cloud.x.test:8443 – alle 10 Min., Ausfall → info",
        "#2: git.x.test – alle 10 Min., Ausfall → info",
    ]
    assert isinstance(waechter.pruefer, ErreichbarkeitsPruefer) and waechter.pruefer.ziele == ()


# ----------------------------------------------------------------------------- Zeitplan


def test_jede_pruefung_hat_ihr_eigenes_intervall(netz, planer, uhr):
    waechter = bauen(netz, planer, ())
    waechter.anlegen(PruefZiel("schnell.x.test"), intervall_minuten=1)
    waechter.anlegen(PruefZiel("langsam.x.test"), intervall_minuten=10)
    ticken(waechter, uhr, 20)
    aufrufe = [u.split("/")[2] for u in netz.urls]
    assert aufrufe.count("schnell.x.test") == 20
    assert aufrufe.count("langsam.x.test") == 2


def test_pausierte_pruefung_laeuft_nicht(waechter, kasten, netz, uhr):
    antwort = ausfuehren(kasten, "erreichbarkeit_pruefung_pausieren", {"pruefung": "git"})
    assert "pausiert" in antwort
    netz.dns_kaputt.add(DOMAINS[1])
    assert ticken(waechter, uhr, 15) == []
    assert not any(DOMAINS[1] in u for u in netz.urls)
    assert DOMAINS[1] not in waechter.zustand()
    weiter = ausfuehren(
        kasten, "erreichbarkeit_pruefung_pausieren", {"pruefung": "git", "pausiert": False}
    )
    assert "läuft wieder" in weiter
    assert [a.titel for a in ticken(waechter, uhr, 6)] == [f"{DOMAINS[1]} ist nicht erreichbar"]


def test_ausfall_mit_der_stufe_der_pruefung(waechter, netz, uhr):
    pruefung = waechter.finden("ki")
    waechter.aendern(pruefung, stufe=AlarmStufe.NOTFALL)
    netz.offline.add(DOMAINS[2])
    alarme = ticken(waechter, uhr, 6)
    assert [(a.stufe, a.titel) for a in alarme] == [
        (AlarmStufe.NOTFALL, f"{DOMAINS[2]} ist nicht erreichbar")
    ]
    assert "Fehlalarm" in alarme[0].text
    assert "erreichbarkeit_pruefung_aendern" in waechter.kontext("Was ist los?")


# ----------------------------------------------------------------------------- Werkzeuge


def test_pruefungen_anzeigen(waechter, kasten, netz, uhr):
    netz.http_status[DOMAINS[1]] = 502
    ticken(waechter, uhr)
    ausfuehren(kasten, "erreichbarkeit_pruefung_pausieren", {"pruefung": 3})
    liste = ausfuehren(kasten, "erreichbarkeit_pruefungen_anzeigen", {})
    assert f"#1: {DOMAINS[0]} – alle 5 Min., Ausfall → wichtig; zuletzt 11.11. 14:00" in liste
    assert "erreichbar (HTTP 200)" in liste
    assert f"#2: {DOMAINS[1]}" in liste and "NICHT erreichbar – HTTPS antwortet mit Fehler 502" in (
        liste
    )
    assert f"#3: {DOMAINS[2]} – alle 5 Min., Ausfall → wichtig, PAUSIERT" in liste


def test_pruefungen_anzeigen_leer(netz, planer):
    kasten = Werkzeugkasten(erreichbarkeit_werkzeuge(bauen(netz, planer, ())))
    assert "keine Erreichbarkeits-Prüfungen" in ausfuehren(
        kasten, "erreichbarkeit_pruefungen_anzeigen", {}
    )


def test_pruefung_anlegen_und_nicht_doppelt(waechter, kasten):
    antwort = ausfuehren(
        kasten,
        "erreichbarkeit_pruefung_anlegen",
        {"ziel": "Cloud.X.test", "port": 8443, "intervall_minuten": 2, "notiz": "Talk"},
    )
    assert antwort.startswith("Prüfung angelegt: #4: cloud.x.test:8443 – alle 2 Min.")
    assert "Notiz: Talk" in antwort
    doppelt = ausfuehren(
        kasten, "erreichbarkeit_pruefung_anlegen", {"ziel": "cloud.x.test:8443", "stufe": "info"}
    )
    assert "gab es schon – aktualisiert" in doppelt and "Ausfall → info" in doppelt
    assert len(waechter.pruefungen()) == 4
    mit_pfad = ausfuehren(
        kasten, "erreichbarkeit_pruefung_anlegen", {"ziel": "ki.x.test:8443", "pfad": "health"}
    )
    assert "#5: ki.x.test:8443/health" in mit_pfad


@pytest.mark.parametrize(
    ("argumente", "hinweis"),
    [
        ({"ziel": "10.0.30.10"}, "IP-Adresse"),
        ({"ziel": "localhost", "port": 8080}, "nicht erlaubt"),
        ({"ziel": "https://cloud.x.test"}, "ohne https://"),
        ({"ziel": "cloud.x.test", "port": 70000}, "1 bis 65535"),
        ({"ziel": "cloud.x.test", "pfad": "/a b"}, "Pfad"),
        ({"ziel": "cloud.x.test", "intervall_minuten": 0}, "1 bis 1440"),
        ({"ziel": "cloud.x.test", "intervall_minuten": 1441}, "1 bis 1440"),
        ({"ziel": "cloud.x.test", "intervall_minuten": "oft"}, "kein Intervall"),
        ({"ziel": "cloud.x.test", "stufe": "egal"}, "Alarm-Stufe"),
        ({"ziel": "cloud.x.test", "notiz": "x" * 201}, "zu lang"),
    ],
)
def test_pruefung_anlegen_lehnt_ungueltiges_ab(waechter, kasten, argumente, hinweis):
    antwort = ausfuehren(kasten, "erreichbarkeit_pruefung_anlegen", argumente)
    assert antwort.startswith("Fehler:") and hinweis in antwort
    assert len(waechter.pruefungen()) == 3


def test_hoechstens_50_pruefungen(netz, planer):
    waechter = bauen(netz, planer, ())
    for nummer in range(MAX_PRUEFUNGEN):
        waechter.anlegen(PruefZiel(f"d{nummer}.x.test"))
    kasten = Werkzeugkasten(erreichbarkeit_werkzeuge(waechter))
    antwort = ausfuehren(kasten, "erreichbarkeit_pruefung_anlegen", {"ziel": "zuviel.x.test"})
    assert "mehr gehen nicht" in antwort
    # Eine vorhandene aktualisieren geht weiterhin.
    assert "aktualisiert" in ausfuehren(
        kasten, "erreichbarkeit_pruefung_anlegen", {"ziel": "d1.x.test", "intervall_minuten": 9}
    )


def test_pruefung_lieber_auf_8443(waechter, kasten, netz, uhr):
    """„Prüf cloud lieber auf Port 8443“ – gilt sofort, ohne Neustart und ohne env."""
    netz.zu.add(443)
    ticken(waechter, uhr, 6)  # auf 443 kaputt → Ausfall gemeldet
    assert waechter.zustand()[DOMAINS[0]]["gemeldet"] == 1
    antwort = ausfuehren(
        kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": "cloud", "port": 8443}
    )
    assert f"Prüfung geändert: #1: {DOMAINS[0]}:8443" in antwort and "ohne Neustart" in antwort
    assert DOMAINS[0] not in waechter.zustand()  # der alte Ausfall ist erledigt
    netz.zertifikat_ports.clear()
    assert ticken(waechter, uhr, 6) == []
    assert 8443 in netz.zertifikat_ports
    assert waechter.zustand()[f"{DOMAINS[0]}:8443"]["status"] == "ok"
    # Nach einem Neustart gilt die Änderung weiter.
    neu = bauen(netz, waechter.alarme.zeitplaner, DOMAINS)
    assert neu.pruefungen()[0].schluessel == f"{DOMAINS[0]}:8443"


def test_pruefung_aendern_intervall_pfad_und_fehler(waechter, kasten):
    stunde = ausfuehren(
        kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": "git", "intervall_minuten": 60}
    )
    assert f"#2: {DOMAINS[1]} – jede Stunde" in stunde
    pfad = ausfuehren(
        kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": "#3", "pfad": "/health"}
    )
    assert f"#3: {DOMAINS[2]}/health" in pfad
    nichts = ausfuehren(kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": 1})
    assert "Es fehlen neue Angaben" in nichts
    unbekannt = ausfuehren(kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": "foo"})
    assert "gibt es nicht" in unbekannt
    mehrdeutig = ausfuehren(
        kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": "louis", "port": 1}
    )
    assert "mehrere Prüfungen" in mehrdeutig
    kaputt = ausfuehren(kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": "git", "port": 0})
    assert "1 bis 65535" in kaputt
    waechter.anlegen(PruefZiel(DOMAINS[1], 8443))
    gleich = ausfuehren(kasten, "erreichbarkeit_pruefung_aendern", {"pruefung": 2, "port": 8443})
    assert "wird schon geprüft" in gleich


def test_pruefung_loeschen_nur_nach_ja(waechter, kasten):
    rueckfrage = ausfuehren(
        kasten, "erreichbarkeit_pruefung_loeschen", {"pruefung": "git"}, anfrage_id="a1"
    )
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and "wird gelöscht" in rueckfrage
    assert len(waechter.pruefungen()) == 3
    erledigt = ausfuehren(
        kasten,
        "erreichbarkeit_pruefung_loeschen",
        {"pruefung": "git"},
        anfrage_id="a2",
        nachricht="Ja, lösch die Prüfung",
    )
    assert "Prüfung gelöscht" in erledigt
    assert [p.schluessel for p in waechter.pruefungen()] == [DOMAINS[0], DOMAINS[2]]


def test_sofort_pruefen_aus_der_tabelle(waechter, kasten, netz):
    ausfuehren(kasten, "erreichbarkeit_pruefung_pausieren", {"pruefung": "ki"})
    netz.zertifikat_kaputt.add(DOMAINS[0])
    alle = ausfuehren(kasten, "erreichbarkeit_pruefen", {})
    assert DOMAINS[2] not in alle  # pausiert
    assert f"{DOMAINS[0]}: NICHT erreichbar" in alle and "erreichbarkeit_pruefung_aendern" in alle
    pausiert = ausfuehren(kasten, "erreichbarkeit_pruefen", {"domain": "ki"})
    assert f"{DOMAINS[2]}: erreichbar" in pausiert and "[Prüfung pausiert]" in pausiert
    keine = ausfuehren(kasten, "erreichbarkeit_pruefen", {"domain": "nas"})
    assert "prüfe ich nicht" in keine and "erreichbarkeit_pruefung_anlegen" in keine


def test_kein_werkzeug_schreibt_in_die_env():
    quelle = Path(__file__).resolve().parents[1] / "src/missa/tools/erreichbarkeit.py"
    text = quelle.read_text(encoding="utf-8")
    for verboten in ("write_text", "open(", "os.environ", "env_datei", "/etc/missa"):
        assert verboten not in text


# ----------------------------------------------------------------------------- Zertifikate


def test_zertifikate_folgen_den_aktiven_pruefungen(waechter, planer, uhr):
    ports: list[tuple[str, int]] = []

    async def zertifikat(domain: str, _timeout: float, port: int = 443) -> datetime:
        ports.append((domain, port))
        return uhr.jetzt.astimezone(UTC).replace(year=2027)

    zertifikate = ZertifikatsWaechter(
        waechter.alarme,
        planer.datei,
        zertifikat_quelle=waechter.zertifikat_ziele,
        domain_namen=None,
        zertifikat=zertifikat,
        uhr=uhr,
    )
    assert zertifikate.zertifikat_domains == DOMAINS
    assert zertifikate.domain_namen == ("louis-degiorgi.test",)
    waechter.aendern(waechter.finden("cloud"), port=8443, pfad="/health")
    waechter.pausieren(waechter.finden("ki"))
    assert zertifikate.zertifikat_domains == (f"{DOMAINS[0]}:8443", DOMAINS[1])
    asyncio.run(zertifikate.ablaeufe())
    assert sorted(ports) == [(DOMAINS[0], 8443), (DOMAINS[1], 443)]


def test_schema_legt_tabelle_ohne_datenverlust_an(planer):
    with verbinden(planer.datei) as db:
        db.executescript(SCHEMA)
        db.executescript(SCHEMA)  # zweimal ist harmlos
        namen = {z["name"] for z in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"erreichbarkeit", "erreichbarkeit_pruefungen"} <= namen


# ----------------------------------------------------------------------------- Im Gespräch


def test_im_chat_lieber_auf_8443_ohne_neustart(einstellungen, llama, netz, uhr):
    """Ganzer Weg: Modell ruft das Werkzeug auf, die Änderung steht danach in der API."""
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from tests.conftest import API_KEY, text, werkzeug_aufruf

    planer = Zeitplaner(einstellungen.datenbank, ZONE, Zustellung([FakeKanal()]), uhr=uhr)
    waechter = bauen(netz, planer, DOMAINS)
    app = create_app(einstellungen, llama_transport=llama.transport, erreichbarkeit=waechter)
    llama.antworten = [
        werkzeug_aufruf("erreichbarkeit_pruefung_aendern", {"pruefung": "cloud", "port": 8443}),
        text("Erledigt – ich prüfe cloud ab jetzt auf Port 8443."),
    ]
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        antwort = client.post(
            "/v1/chat/completions",
            json={
                "model": "missa",
                "messages": [
                    {"role": "user", "content": "Prüf cloud.louis-degiorgi.test lieber auf 8443"}
                ],
            },
        )
        assert antwort.status_code == 200
        assert "Prüfung geändert" in llama.anfragen[1]["messages"][-1]["content"]
        pruefungen = client.get("/api/server").json()["erreichbarkeit_pruefungen"]
    assert pruefungen[0]["ziel"] == f"{DOMAINS[0]}:8443" and pruefungen[0]["port"] == 8443
    assert waechter.pruefungen()[0].ziel == PruefZiel(DOMAINS[0], 8443)


def test_lagebild_ohne_und_mit_pruefungen(netz, planer, uhr):
    from missa.core.lagebild import erreichbarkeit_quelle

    waechter = bauen(netz, planer, ())
    quelle = erreichbarkeit_quelle(waechter)
    assert quelle() == "Keine aktiven Prüfungen."
    waechter.anlegen(PruefZiel("cloud.x.test", 8443))
    assert quelle() == "Noch nicht geprüft."
    ticken(waechter, uhr)
    assert quelle() == "Alle 1 Domains erreichbar."
