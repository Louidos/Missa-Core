"""Fehlersuche in Logs: relevante Zeilen, bekannte Ursachen, „Warum geht Nextcloud nicht?“."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.werkzeuge import Werkzeugkasten
from missa.integrationen.anmeldungen import NextcloudLog, nextcloud_fehler
from missa.integrationen.logs import in_stuecke, relevante_zeilen, ursachen_erkennen
from missa.tools.container import ContainerDienst
from missa.tools.fehlersuche import Fehlersuche, dienste_lesen, fehlersuche_werkzeuge
from tests.conftest import API_KEY, text, werkzeug_aufruf
from tests.ssh_fake import FakeHosts, container, ps
from tests.test_zeitplaner import ZONE

JETZT = datetime(2026, 11, 11, 14, 0, tzinfo=ZONE)

APP_LOG = "\n".join(
    [
        "2026-11-11T12:58:01Z 10.0.70.10 - - GET /status.php 200",
        "2026-11-11T12:58:02Z PHP Warning: Undefined array key in lib/x.php",
        *[
            f"2026-11-11T12:59:{i:02d}Z Doctrine\\DBAL\\Exception: An exception occurred in the "
            "driver: SQLSTATE[HY000] [2002] Connection refused"
            for i in range(12)
        ],
        "2026-11-11T13:00:00Z 10.0.70.10 - - GET /index.php 503",
    ]
)
NC_LOG = "\n".join(
    json.dumps(z)
    for z in (
        {"time": "2026-11-11T12:59:00+00:00", "level": 3, "app": "core",
         "message": "SQLSTATE[HY000] [2002] Connection refused"},
        {"time": "2026-11-11T12:59:01+00:00", "level": 1, "app": "core", "message": "nur info"},
        {"time": "2026-11-11T12:59:02+00:00", "level": 4, "app": "index",
         "message": "Fatal", "exception": {"Exception": "PDOException", "Message": "down"}},
    )
)  # fmt: skip


@pytest.fixture
def hosts() -> FakeHosts:
    fake = FakeHosts()
    fake.antworten[("int01", "docker-ps")] = ps(
        container("nextcloud-app", "Up 2 hours (unhealthy)"),
        container("nextcloud-db", "Exited (1) 3 minutes ago", "exited", "mariadb:11"),
        container("redis", "Up 2 hours"),
    )
    fake.antworten[("wb01", "docker-ps")] = ps(container("homepage", "Up 1 day"))
    fake.antworten[("rp01", "docker-ps")] = (64, "", "missa-befehl: Docker ist nicht installiert")

    def logs(argumente: list[str]) -> str:
        if argumente[0] == "nextcloud-app":
            return APP_LOG
        if argumente[0] == "nextcloud-db":
            return "2026-11-11T12:57:00Z [ERROR] InnoDB: No space left on device"
        return ""

    fake.antworten[("int01", "docker-logs")] = logs
    fake.antworten[("int01", "journal")] = (
        "2026-11-11T13:59:00+0100 int01 kernel: EXT4-fs error (device sda1): something\n"
        "-- No entries --"
    )
    fake.antworten[("int01", "nextcloud-log")] = NC_LOG
    return fake


@pytest.fixture
def suche(hosts) -> Fehlersuche:
    ssh = hosts.verbindung()
    return Fehlersuche(
        ssh,
        ContainerDienst(ssh, uhr=lambda: JETZT),
        nextcloud_log=NextcloudLog("int01", "datei"),
        uhr=lambda: JETZT,
    )


def test_relevante_zeilen_fassen_gleiches_zusammen():
    zeilen = APP_LOG.splitlines()
    relevant = relevante_zeilen(zeilen)
    assert len(relevant) == 3  # Warning, 12× SQLSTATE zusammengefasst, 503
    assert relevant[1][1] == 12 and "SQLSTATE" in relevant[1][0]
    assert all("status.php" not in z for z, _ in relevant)


@pytest.mark.parametrize(
    ("zeile", "name"),
    [
        ("write error: No space left on device", "disk_voll"),
        ("Out of memory: Killed process 1234 (php-fpm)", "speicher"),
        ("SQLSTATE[HY000] [2002] Connection refused", "datenbank"),
        ("Nextcloud is in maintenance mode", "wartungsmodus"),
        ("x509: certificate has expired or is not yet valid", "zertifikat"),
        ("dial tcp 10.0.30.20:80: connect: connection refused", "backend"),
        ("bind() to 0.0.0.0:80 failed (98: Address already in use)", "port_belegt"),
        ("fopen(/var/www/data/x): Permission denied", "rechte"),
    ],
)
def test_ursachen_erkennen(zeile, name):
    assert ursachen_erkennen([zeile])[0][0].name == name


def test_allgemeine_verbindung_weicht_genauerer_ursache():
    namen = [u.name for u, _, _ in ursachen_erkennen(APP_LOG.splitlines())]
    assert namen == ["datenbank"]


def test_in_stuecke():
    zeilen = [f"zeile {i:04d} " + "x" * 90 for i in range(200)]
    stuecke = in_stuecke(zeilen, 2000)
    assert stuecke[0][-1] == zeilen[-1]  # Teil 1 = die neuesten
    assert stuecke[-1][0] == zeilen[0]
    assert sum(len(s) for s in stuecke) == 200
    assert all(sum(len(z) + 1 for z in s) <= 2000 for s in stuecke)


def test_nextcloud_fehler():
    zeilen = nextcloud_fehler(NC_LOG)
    assert len(zeilen) == 2
    assert "[fehler] core: SQLSTATE" in zeilen[0]
    assert "[fatal] index: Fatal – PDOException: down" in zeilen[1]


def test_dienste_lesen():
    assert dienste_lesen("Nextcloud:int01:nextcloud-app,mariadb; caddy:rp01:caddy.service") == {
        "nextcloud": [("int01", "nextcloud-app"), ("int01", "mariadb")],
        "caddy": [("rp01", "caddy.service")],
    }
    assert dienste_lesen("git:wb01") == {"git": [("wb01", "")]}
    with pytest.raises(ValueError):
        dienste_lesen("nurname")


def test_warum_geht_nextcloud_nicht(suche, hosts):
    antwort = asyncio.run(suche.suchen("Nextcloud"))
    assert antwort.startswith("Fehlersuche „nextcloud“.")
    for quelle in ("int01/nextcloud-app", "int01/nextcloud-db", "int01/nextcloud.log"):
        assert quelle in antwort
    assert "Container nextcloud-db auf int01: Exited (1) 3 minutes ago ⚠️ ist abgestürzt" in antwort
    assert "Die Festplatte ist voll" in antwort
    assert "Die Datenbank ist nicht erreichbar" in antwort
    assert "(12×)" in antwort
    assert "Erkläre Louis in 2–4 einfachen Sätzen" in antwort
    # gesucht wurde nur lesend, der Container wurde nicht angefasst
    assert hosts.befehle("docker-restart") == []
    eine_stunde = int((JETZT - timedelta(hours=1)).timestamp())
    assert ("int01", ["prio=3", f"seit=@{eine_stunde}", "zeilen=150"]) in [
        (h, sorted(a)) for h, a in hosts.befehle("journal")
    ]


def test_zuordnung_und_unbekannter_dienst(hosts):
    ssh = hosts.verbindung()
    suche = Fehlersuche(
        ssh,
        ContainerDienst(ssh, uhr=lambda: JETZT),
        dienste=dienste_lesen("caddy:rp01:caddy.service"),
        uhr=lambda: JETZT,
    )
    hosts.antworten[("rp01", "journal")] = (
        "2026-11-11T13:58:00+0100 rp01 caddy[9]: dial tcp 10.0.30.20:443: connect: "
        "connection refused"
    )
    antwort = asyncio.run(suche.suchen("caddy"))
    assert "rp01/caddy.service" in antwort
    assert "Der Reverse-Proxy erreicht den eigentlichen Dienst dahinter nicht" in antwort
    assert any("unit=caddy.service" in a for _, a in hosts.befehle("journal"))

    nichts = asyncio.run(suche.suchen("gibtsnicht", host="wb01"))
    assert "wb01/*gibtsnicht*" in nichts
    assert "keine auffälligen Zeilen" in nichts or "nicht erkannt" in nichts


def test_host_nicht_erreichbar_wird_ehrlich_gesagt(suche, hosts):
    hosts.offline.add("int01")
    antwort = asyncio.run(suche.suchen("nextcloud", host="int01"))
    assert "Nicht prüfbar:" in antwort and "Keine SSH-Verbindung zu int01" in antwort


def test_journal_lesen_werkzeug(suche, hosts):
    hosts.antworten[("wb01", "journal")] = "\n".join(
        f"2026-11-11T13:{i:02d}:00+0100 wb01 app[1]: Zeile {i}" for i in range(60)
    )
    kasten = Werkzeugkasten(fehlersuche_werkzeuge(suche))
    antwort = asyncio.run(
        kasten.ausfuehren(
            "journal_lesen",
            {"host": "wb01", "unit": "caddy.service", "prioritaet": "fehler", "stunden": 2},
        )
    )
    assert "System-Log (journal) von wb01, Unit caddy.service, letzte 2 Stunde(n)" in antwort
    assert "Zeile 59" in antwort
    angaben = hosts.befehle("journal")[-1][1]
    assert "unit=caddy.service" in angaben and "prio=3" in angaben
    falsch = asyncio.run(kasten.ausfuehren("journal_lesen", {"host": "wb01", "unit": "x; id"}))
    assert falsch.startswith("Fehler:")


def test_ueber_den_chat(einstellungen, llama, hosts):
    """„Warum geht Nextcloud nicht?“ → Missa liest die Logs und nennt eine verständliche Ursache."""
    e = replace(einstellungen, nextcloud_log_host="int01", nextcloud_log_quelle="datei")
    app = create_app(e, llama_transport=llama.transport, ssh_verbindung=hosts.verbindung())
    llama.antworten = [
        werkzeug_aufruf("fehlersuche", {"dienst": "nextcloud"}),
        text("Die Datenbank von Nextcloud ist abgestürzt, weil die Festplatte voll ist."),
    ]
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        antwort = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Warum geht Nextcloud nicht?"}]},
        )
        assert antwort.status_code == 200
        ergebnis = llama.anfragen[1]["messages"][-1]["content"]
        assert "Die Festplatte ist voll" in ergebnis
        assert "Festplatte voll" in antwort.json()["choices"][0]["message"]["content"]
        werkzeuge = client.get("/health").json()["werkzeuge"]
        for name in ("container_anzeigen", "container_neustarten", "fehlersuche", "journal_lesen"):
            assert name in werkzeuge
