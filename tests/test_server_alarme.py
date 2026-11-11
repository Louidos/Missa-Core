"""Server-Alarme: Quellen (Monitoring, Homepage, Proxmox), Regeln, Auffälligkeiten, Werkzeuge."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.app import create_app
from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.monitoring import (
    HomepageQuelle,
    JsonQuelle,
    Messwert,
    QuellenFehler,
    json_auslesen,
    metrik_verstehen,
)
from missa.integrationen.proxmox import ProxmoxLeser
from missa.tools import werkzeuge_laden
from missa.tools.server_alarme import ServerWaechter, server_werkzeuge
from tests.conftest import API_KEY, text, werkzeug_aufruf
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

# ----------------------------------------------------------------------------- Hilfen


class FakeQuelle:
    """Monitoring-Quelle, deren Werte der Test setzt."""

    def __init__(self, name: str = "test") -> None:
        self.name = name
        self.werte: dict[tuple[str, str], float] = {}
        self.kaputt = False

    async def messwerte(self) -> list[Messwert]:
        if self.kaputt:
            raise QuellenFehler("Verbindung abgelehnt")
        return [Messwert(z, m, w, "%", self.name) for (z, m), w in self.werte.items()]


@pytest.fixture
def uhr() -> Uhr:
    # Mittwoch, 14:00 – außerhalb der Ruhezeit
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


@pytest.fixture
def quelle() -> FakeQuelle:
    return FakeQuelle()


@pytest.fixture
def waechter(quelle, alarme, planer) -> ServerWaechter:
    return ServerWaechter([quelle], alarme, planer.datei, ZONE)


def ticken(waechter: ServerWaechter, uhr: Uhr, minuten: int = 1) -> list:
    """Lässt den Wächter ``minuten`` Mal im Minutentakt laufen; gibt alle Alarme zurück."""
    gemeldet = []
    for _ in range(minuten):
        gemeldet += asyncio.run(waechter.pruefen())
        uhr.vor(minutes=1)
    return gemeldet


def zustellen(planer: Zeitplaner) -> int:
    return asyncio.run(planer.faellige_zustellen())


def ausfuehren(kasten: Werkzeugkasten, name: str, argumente: dict, **extra) -> str:
    return asyncio.run(kasten.ausfuehren(name, argumente, **extra))


# ----------------------------------------------------------------------------- Formate


@pytest.mark.parametrize(
    ("text", "metrik"),
    [
        ("Auslastung", "cpu"),
        ("CPU", "cpu"),
        ("Arbeitsspeicher", "ram"),
        ("RAM-Auslastung", "ram"),
        ("memory_percent", "ram"),
        ("Festplattenauslastung", "disk"),
        ("Speicherplatz", "disk"),
        ("läuft", "laeuft"),
        ("Temperatur", "temperatur"),
        ("uptime", "uptime"),
        ("", "cpu"),
    ],
)
def test_metrik_verstehen(text, metrik):
    assert metrik_verstehen(text) == metrik


def test_json_liste_und_verschachtelt():
    liste = json_auslesen(
        {
            "messwerte": [
                {"ziel": "AI01", "messwert": "cpu", "wert": 93.5},
                {"host": "wb01", "metric": "memory", "value": "41,5"},
                {"ziel": "kaputt", "messwert": "cpu", "wert": "viel"},
                "Unsinn",
            ]
        }
    )
    assert [(w.ziel, w.metrik, w.wert, w.einheit) for w in liste] == [
        ("ai01", "cpu", 93.5, "%"),
        ("wb01", "ram", 41.5, "%"),
    ]
    verschachtelt = json_auslesen({"int01": {"cpu": 12, "disk": 70, "temperatur": 51}})
    assert {(w.metrik, w.wert) for w in verschachtelt} == {
        ("cpu", 12.0),
        ("disk", 70.0),
        ("temperatur", 51.0),
    }


def test_json_quelle_mit_token_und_fehlern():
    gesehen = []

    def antwort(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        if request.url.path == "/kaputt":
            return httpx.Response(503)
        if request.url.path == "/fremd":
            return httpx.Response(200, json={"irgendwas": 1})
        return httpx.Response(200, json=[{"ziel": "ai01", "messwert": "cpu", "wert": 10}])

    transport = httpx.MockTransport(antwort)
    quelle = JsonQuelle("http://mon.test/werte", "geheim-token", transport=transport)
    assert asyncio.run(quelle.messwerte())[0].wert == 10
    assert gesehen[0].headers["Authorization"] == "Bearer geheim-token"
    with pytest.raises(QuellenFehler, match="503"):
        asyncio.run(JsonQuelle("http://mon.test/kaputt", transport=transport).messwerte())
    with pytest.raises(QuellenFehler, match="Format nicht erkannt"):
        asyncio.run(JsonQuelle("http://mon.test/fremd", transport=transport).messwerte())


def test_homepage_quelle_liest_site_monitor():
    def antwort(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Server",
                        "services": [
                            {"name": "Nextcloud", "siteMonitor": "https://cloud.test"},
                            {"name": "Ohne Monitor", "href": "https://x.test"},
                        ],
                        "groups": [
                            {"name": "Intern", "services": [{"name": "Git", "siteMonitor": "x"}]}
                        ],
                    }
                ],
            )
        dienst = request.url.params["serviceName"]
        if dienst == "Nextcloud":
            return httpx.Response(200, json={"status": 200, "latency": 87.4})
        return httpx.Response(200, json={"status": 502, "latency": 3})

    quelle = HomepageQuelle("http://homepage.test/", transport=httpx.MockTransport(antwort))
    werte = {(w.ziel, w.metrik): w.wert for w in asyncio.run(quelle.messwerte())}
    assert werte == {
        ("nextcloud", "erreichbar"): 1.0,
        ("nextcloud", "antwortzeit"): 87.4,
        ("git", "erreichbar"): 0.0,
    }


# ----------------------------------------------------------------------------- Proxmox

RESSOURCEN = [
    {"type": "node", "node": "pve-id01", "status": "online", "cpu": 0.12, "mem": 8, "maxmem": 32,
     "disk": 40, "maxdisk": 100},
    {"type": "qemu", "vmid": 103, "name": "ai01", "status": "running", "cpu": 0.935,
     "mem": 12, "maxmem": 16, "disk": 0, "maxdisk": 200},
    {"type": "qemu", "vmid": 101, "name": "wb01", "status": "stopped", "cpu": 0, "mem": 0,
     "maxmem": 4},
    {"type": "lxc", "vmid": 120, "name": "dns01", "status": "running", "cpu": 0.01, "mem": 1,
     "maxmem": 2, "disk": 1, "maxdisk": 4},
    {"type": "storage", "storage": "local"},
]  # fmt: skip


def test_proxmox_nur_lesend_mit_token():
    anfragen: list[httpx.Request] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        anfragen.append(request)
        return httpx.Response(200, json={"data": RESSOURCEN})

    leser = ProxmoxLeser(
        "https://pve.test:8006",
        "missa@pve!monitor",
        "token-geheim",
        transport=httpx.MockTransport(antwort),
    )
    werte = {(w.ziel, w.metrik): w.wert for w in asyncio.run(leser.messwerte())}
    assert werte[("ai01", "cpu")] == 93.5
    assert werte[("ai01", "ram")] == 75.0
    assert ("ai01", "disk") not in werte  # qemu ohne Disk-Angabe
    assert werte[("pve-id01", "disk")] == 40.0
    assert werte[("wb01", "laeuft")] == 0.0 and ("wb01", "cpu") not in werte
    assert werte[("dns01", "disk")] == 25.0
    assert all(r.method == "GET" for r in anfragen)
    assert anfragen[0].url.path == "/api2/json/cluster/resources"
    assert anfragen[0].headers["Authorization"] == "PVEAPIToken=missa@pve!monitor=token-geheim"
    # Es gibt keine Methode zum Schreiben, Starten, Stoppen oder Löschen.
    assert not any(
        hasattr(leser, n) for n in ("schreiben", "post", "put", "delete", "starten", "stoppen")
    )


def test_proxmox_abgelehntes_token():
    leser = ProxmoxLeser(
        "https://pve.test:8006",
        "missa@pve!monitor",
        "falsch",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    with pytest.raises(QuellenFehler, match="PVEAuditor"):
        asyncio.run(leser.messwerte())


# ----------------------------------------------------------------------------- Regeln


def test_ruf_mich_an_wird_notfall_regel_und_loest_nach_5_minuten_aus(
    waechter, quelle, planer, kanal, uhr
):
    kasten = Werkzeugkasten(server_werkzeuge(waechter))
    antwort = ausfuehren(
        kasten, "alarmregel_anlegen", {"ziel": "ai01", "grenze": 90, "stufe": "ruf mich an"}
    )
    assert "ai01: CPU über 90 % länger als 5 Minuten → notfall" in antwort
    assert "Telefon" in antwort  # ehrlicher Hinweis: Anruf gibt es noch nicht
    regel = waechter.regeln()[0]
    assert (regel.ziel, regel.metrik, regel.richtung, regel.grenze) == ("ai01", "cpu", "ueber", 90)
    assert (regel.dauer_minuten, regel.stufe) == (5, AlarmStufe.NOTFALL)

    quelle.werte[("ai01", "cpu")] = 95
    assert ticken(waechter, uhr, 5) == []  # 0 … 4 Minuten: noch nicht
    alarme = ticken(waechter, uhr, 1)  # nach 5 Minuten
    assert [a.stufe for a in alarme] == [AlarmStufe.NOTFALL]
    zustellen(planer)
    assert kanal.nachrichten[-1].startswith("🚨 NOTFALL: ai01: CPU über 90 %")
    assert "aktuell 95 %" in kanal.nachrichten[-1]


def test_derselbe_alarm_kommt_nicht_im_minutentakt(waechter, quelle, planer, kanal, uhr):
    waechter.regel_anlegen("ai01", "cpu", "ueber", 90, 0, AlarmStufe.WICHTIG)
    quelle.werte[("ai01", "cpu")] = 99
    alarme = ticken(waechter, uhr, 30)
    assert len(alarme) == 1
    zustellen(planer)
    assert len(kanal.nachrichten) == 1
    # Nach dem Ruhefenster (60 Minuten) kommt eine Erinnerung, dass es noch anhält.
    assert len(ticken(waechter, uhr, 31)) == 1


def test_wieder_normal_kommt_als_info(waechter, quelle, uhr):
    waechter.regel_anlegen("ai01", "cpu", "ueber", 90, 2, AlarmStufe.NOTFALL)
    quelle.werte[("ai01", "cpu")] = 95
    assert len(ticken(waechter, uhr, 3)) == 1
    assert waechter.aktive_alarme()
    quelle.werte[("ai01", "cpu")] = 30
    normal = ticken(waechter, uhr, 1)
    assert [a.stufe for a in normal] == [AlarmStufe.INFO]
    assert "wieder normal" in normal[0].titel
    assert waechter.aktive_alarme() == []


def test_kurze_spitze_loest_nichts_aus(waechter, quelle, uhr):
    waechter.regel_anlegen("ai01", "cpu", "ueber", 90, 5, AlarmStufe.NOTFALL)
    quelle.werte[("ai01", "cpu")] = 95
    ticken(waechter, uhr, 3)
    quelle.werte[("ai01", "cpu")] = 40
    ticken(waechter, uhr, 1)
    quelle.werte[("ai01", "cpu")] = 95
    assert ticken(waechter, uhr, 4) == []


def test_regel_fuer_alle_und_unter(waechter, quelle, uhr):
    waechter.regel_anlegen("*", "disk", "ueber", 85, 0, AlarmStufe.WICHTIG)
    waechter.regel_anlegen("wb01", "laeuft", "unter", 1, 0, AlarmStufe.NOTFALL)
    quelle.werte.update({("ai01", "disk"): 90, ("int01", "disk"): 50, ("wb01", "laeuft"): 0})
    titel = sorted(a.titel for a in ticken(waechter, uhr))
    assert titel == ["ai01: Disk über 85 %", "wb01 läuft nicht"]


def test_regeln_auflisten_aendern_loeschen(waechter, quelle):
    kasten = Werkzeugkasten(server_werkzeuge(waechter))
    ausfuehren(kasten, "alarmregel_anlegen", {"ziel": "ai01", "grenze": "90 %"})
    doppelt = ausfuehren(kasten, "alarmregel_anlegen", {"ziel": "AI01", "grenze": 90})
    assert "aktualisiert" in doppelt and len(waechter.regeln()) == 1
    liste = ausfuehren(kasten, "alarmregeln_anzeigen", {})
    assert "#1: ai01: CPU über 90 % länger als 5 Minuten → wichtig" in liste

    geaendert = ausfuehren(
        kasten,
        "alarmregel_aendern",
        {"regel_id": 1, "grenze": 80, "dauer_minuten": 10, "stufe": "notfall"},
    )
    assert "CPU über 80 % länger als 10 Minuten → notfall" in geaendert

    # Löschen ist zerstörerisch: erst Rückfrage, dann nach Ja.
    rueckfrage = ausfuehren(kasten, "alarmregel_loeschen", {"regel_id": 1}, anfrage_id="a1")
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and "wird gelöscht" in rueckfrage
    assert len(waechter.regeln()) == 1
    erledigt = ausfuehren(
        kasten, "alarmregel_loeschen", {"regel_id": 1}, anfrage_id="a2", nachricht="Ja, lösch sie"
    )
    assert "gelöscht" in erledigt and waechter.regeln() == []


def test_unbekanntes_ziel_wird_angesprochen(waechter, quelle, uhr):
    quelle.werte[("ai01", "cpu")] = 10
    ticken(waechter, uhr)
    kasten = Werkzeugkasten(server_werkzeuge(waechter))
    antwort = ausfuehren(kasten, "alarmregel_anlegen", {"ziel": "ai02", "grenze": 90})
    assert "keine Werte" in antwort and "ai01" in antwort


def test_werte_anzeigen(waechter, quelle):
    quelle.werte.update({("ai01", "cpu"): 12, ("ai01", "ram"): 41})
    kasten = Werkzeugkasten(server_werkzeuge(waechter))
    antwort = ausfuehren(kasten, "server_werte_anzeigen", {"ziel": "ai01"})
    assert "ai01: CPU 12 %, RAM 41 % (test)" in antwort


# ----------------------------------------------------------------------------- Auffälligkeiten


def verlauf_anlegen(waechter: ServerWaechter, uhr: Uhr, werte: list[float]) -> None:
    """Legt für die letzten Tage zur aktuellen Stunde ein Stundenmittel an."""
    from missa.core.datenbank import verbinden

    with verbinden(waechter.datei) as db:
        for tage, wert in enumerate(werte, start=1):
            tag = (uhr.jetzt.date() - timedelta(days=tage)).isoformat()
            db.execute(
                "INSERT INTO messwert_stunden VALUES ('ai01', 'cpu', ?, ?, 60, ?)",
                (tag, uhr.jetzt.hour, wert * 60),
            )


def test_ungewoehnlicher_anstieg_unter_der_grenze_gibt_hinweis(waechter, quelle, uhr):
    waechter.regel_anlegen("ai01", "cpu", "ueber", 90, 5, AlarmStufe.NOTFALL)
    verlauf_anlegen(waechter, uhr, [18, 20, 22, 19, 21, 20, 20])
    quelle.werte[("ai01", "cpu")] = 45  # weit über üblich, aber unter 90
    alarme = ticken(waechter, uhr, 3)
    assert [a.stufe for a in alarme] == [AlarmStufe.INFO]
    assert alarme[0].titel == "ai01: CPU ungewöhnlich hoch"
    assert "üblich um diese Zeit sind etwa 20 %" in alarme[0].text
    # Nicht wiederholt, solange es so bleibt …
    assert ticken(waechter, uhr, 10) == []
    # … aber ein noch stärkerer Anstieg wird als wichtig gemeldet.
    quelle.werte[("ai01", "cpu")] = 70
    alarme = ticken(waechter, uhr, 5)
    assert [a.stufe for a in alarme] == [AlarmStufe.WICHTIG]


def test_kein_hinweis_ohne_genug_verlauf_oder_bei_normalem_wert(waechter, quelle, uhr):
    verlauf_anlegen(waechter, uhr, [20, 20])  # erst 2 Tage
    quelle.werte[("ai01", "cpu")] = 60
    assert ticken(waechter, uhr, 4) == []
    verlauf_anlegen(waechter, Uhr(uhr.jetzt - timedelta(days=2)), [20, 20, 20])
    uhr.vor(minutes=10)  # die 60 % von eben zählen nicht mehr zum aktuellen Wert
    quelle.werte[("ai01", "cpu")] = 28  # nur 8 Punkte über üblich
    assert ticken(waechter, uhr, 4) == []


def test_verlauf_wird_gespeichert(waechter, quelle, uhr):
    from missa.core.datenbank import verbinden

    quelle.werte[("ai01", "cpu")] = 30
    ticken(waechter, uhr, 2)
    quelle.werte[("ai01", "cpu")] = 50
    ticken(waechter, uhr, 2)
    with verbinden(waechter.datei) as db:
        zeile = db.execute("SELECT anzahl, summe FROM messwert_stunden").fetchone()
    assert (zeile["anzahl"], zeile["summe"]) == (4, 160)


# ----------------------------------------------------------------------------- Quellen-Ausfall


def test_quelle_antwortet_nicht_und_wieder(waechter, quelle, uhr):
    quelle.kaputt = True
    assert ticken(waechter, uhr, 15) == []
    alarme = ticken(waechter, uhr, 1)
    assert [a.titel for a in alarme] == ["Monitoring-Quelle test antwortet nicht"]
    assert ticken(waechter, uhr, 30) == []
    quelle.kaputt = False
    assert [a.titel for a in ticken(waechter, uhr)] == ["Monitoring-Quelle test antwortet wieder"]


# ----------------------------------------------------------------------------- Laden und App


def test_ohne_konfiguration_aus():
    e = Einstellungen()
    assert not e.server_monitoring_konfiguriert
    namen = [w.name for w in werkzeuge_laden(e)]
    assert "server_werte_anzeigen" not in namen and "alarmregel_anlegen" not in namen


def test_einstellungen_aus_umgebung():
    e = Einstellungen.aus_umgebung(
        {
            "MONITORING_URL": "http://10.0.0.5:9000/werte",
            "PROXMOX_URL": "https://10.0.10.2:8006/",
            "PROXMOX_TOKEN_ID": "missa@pve!monitor",
            "PROXMOX_TOKEN_SECRET": "abc",
            "PROXMOX_TLS_PRUEFEN": "false",
            "ALARM_DAUER_MINUTEN": "3",
            "ALARM_RUHEZEIT_VON": "aus",
            "ERREICHBARKEIT_DOMAINS": "Cloud.louis-degiorgi.ch, git.louis-degiorgi.ch;",
        }
    )
    assert e.server_monitoring_konfiguriert and e.proxmox_konfiguriert
    assert e.proxmox_url == "https://10.0.10.2:8006" and not e.proxmox_tls_pruefen
    assert e.alarm_dauer_minuten == 3 and e.alarm_ruhezeit_von is None
    assert e.erreichbarkeit_domains == ("cloud.louis-degiorgi.ch", "git.louis-degiorgi.ch")
    assert "abc" in e.geheimnisse


def monitoring_transport(werte: dict[str, float]) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: httpx.Response(
            200, json=[{"ziel": "ai01", "messwert": m, "wert": w} for m, w in werte.items()]
        )
    )


def test_ruf_mich_an_ueber_den_chat(einstellungen, llama, tmp_path):
    """„Ruf mich an, wenn ai01 über 90 % ist“ → Regel gespeichert → löst als Notfall aus."""
    from dataclasses import replace

    e = replace(einstellungen, monitoring_url="http://monitoring.test/werte")
    kanal = FakeKanal()
    app = create_app(
        e,
        llama_transport=llama.transport,
        server_transport=monitoring_transport({"cpu": 12}),
        zustellung=Zustellung([kanal]),
    )
    llama.antworten = [
        werkzeug_aufruf("alarmregel_anlegen", {"ziel": "ai01", "grenze": 90, "stufe": "notfall"}),
        text("Mach ich! Liegt ai01 länger als 5 Minuten über 90 %, melde ich mich sofort."),
    ]
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        antwort = client.post(
            "/v1/chat/completions",
            json={
                "model": "missa",
                "messages": [{"role": "user", "content": "Ruf mich an, wenn ai01 über 90 % ist"}],
            },
        )
        assert antwort.status_code == 200
        werkzeug_antwort = llama.anfragen[1]["messages"][-1]["content"]
        assert "→ notfall" in werkzeug_antwort
        alarme_api = client.get("/api/alarme").json()
        assert alarme_api["regeln"][0]["stufe"] == "notfall"
        assert alarme_api["ruhezeit"] == {"von": "22:00", "bis": "07:00"}
        server_api = client.get("/api/server").json()
        assert server_api["monitoring"] is True and server_api["quellen"] == ["monitoring"]

    # Die gespeicherte Regel löst mit hohen Werten als Notfall aus (Uhr im Test gesteuert).
    uhr = Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))
    planer = Zeitplaner(e.datenbank, ZONE, Zustellung([kanal]), uhr=uhr)
    quelle = FakeQuelle("monitoring")
    quelle.werte[("ai01", "cpu")] = 97
    waechter = ServerWaechter([quelle], Alarme(e.datenbank, ZONE, planer), e.datenbank, ZONE)
    alarme = ticken(waechter, uhr, 6)
    assert [a.stufe for a in alarme] == [AlarmStufe.NOTFALL]
    zustellen(planer)
    assert kanal.nachrichten[-1].startswith("🚨 NOTFALL: ai01: CPU über 90 %")


def test_api_ohne_monitoring(client):
    assert client.get("/api/server").json()["monitoring"] is False
    antwort = client.get("/api/alarme").json()
    assert antwort["regeln"] == [] and antwort["anzahl"] == 0


def test_kontext_zeigt_aktive_alarme(waechter, quelle, uhr):
    waechter.regel_anlegen("ai01", "cpu", "ueber", 90, 0, AlarmStufe.NOTFALL)
    assert waechter.kontext("Was ist los?") is None
    quelle.werte[("ai01", "cpu")] = 95
    ticken(waechter, uhr)
    assert "ai01: CPU über 90 % seit 14:00 Uhr (notfall)" in waechter.kontext("Was ist los?")


def test_geheimnisse_nicht_im_protokoll(tmp_path):
    """Das Proxmox-Token-Secret und das Monitoring-Token werden im Protokoll geschwärzt."""
    from missa.core.rechte import schwaerzen

    e = Einstellungen(
        proxmox_token_secret="sehr-geheimes-token", monitoring_token="mon-token-123", daten=tmp_path
    )
    text = json.dumps(
        schwaerzen(
            {"kopf": "PVEAPIToken=missa@pve!monitor=sehr-geheimes-token mon-token-123"},
            e.geheimnisse,
        )
    )
    assert "sehr-geheimes-token" not in text and "mon-token-123" not in text
