"""Docker-Container: Lesen, Hänger erkennen, Logs in Teilen, Neustart nur nach Ja."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.rechte import Aktionsprotokoll, Rechte
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.docker import dauer_lesen, ps_lesen
from missa.tools.container import ContainerDienst, container_werkzeuge
from tests.ssh_fake import FakeHosts, container, ps
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

STATS = "\n".join(
    json.dumps(s)
    for s in (
        {"Name": "nextcloud", "CPUPerc": "1.20%", "MemUsage": "312MiB / 3.8GiB", "MemPerc": "8%"},
        {"Name": "mariadb", "CPUPerc": "0.40%", "MemUsage": "120MiB / 3.8GiB", "MemPerc": "3%"},
    )
)


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))


@pytest.fixture
def planer(tmp_path: Path, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)


@pytest.fixture
def alarme(planer) -> Alarme:
    return Alarme(planer.datei, ZONE, planer)


@pytest.fixture
def hosts() -> FakeHosts:
    fake = FakeHosts()
    fake.antworten[("wb01", "docker-ps")] = ps(
        container("nextcloud", "Up 3 hours (healthy)"),
        container("mariadb", "Up 3 hours"),
        container("backup", "Exited (0) 2 hours ago", "exited"),
    )
    fake.antworten[("wb01", "docker-stats")] = STATS
    return fake


@pytest.fixture
def dienst(hosts, alarme, uhr) -> ContainerDienst:
    return ContainerDienst(hosts.verbindung(), alarme, uhr=uhr)


def test_dauer_lesen():
    assert dauer_lesen("Up 12 minutes (health: starting)") == timedelta(minutes=12)
    assert dauer_lesen("Up About an hour") == timedelta(hours=1)
    assert dauer_lesen("Up Less than a second") == timedelta(0)
    assert dauer_lesen("Up 3 days") == timedelta(days=3)
    assert dauer_lesen("Created") is None


def test_probleme_erkennen():
    liste = ps_lesen(
        "wb01",
        ps(
            container("a", "Up 2 hours (healthy)"),
            container("b", "Up 2 hours (unhealthy)"),
            container("c", "Restarting (1) 5 seconds ago", "restarting"),
            container("d", "Up 15 minutes (health: starting)"),
            container("e", "Up 2 minutes (health: starting)"),
            container("f", "Exited (137) 3 minutes ago", "exited"),
            container("g", "Exited (0) 1 hour ago", "exited"),
            container("h", "Dead", "dead"),
        ),
    )
    probleme = {c.name: c.problem() for c in liste}
    assert probleme["a"] == "" and probleme["e"] == "" and probleme["g"] == ""
    assert "ungesund" in probleme["b"]
    assert "Neustart-Schleife" in probleme["c"]
    assert "hängt beim Start" in probleme["d"]
    assert "Exit-Code 137" in probleme["f"]
    assert "tot" in probleme["h"]


def test_welche_container_laufen_auf_wb01(dienst):
    kasten = Werkzeugkasten(container_werkzeuge(dienst))
    antwort = asyncio.run(kasten.ausfuehren("container_anzeigen", {"host": "wb01"}))
    assert antwort.startswith("Container auf wb01 (2 von 3 laufen, Stand 14:00 Uhr):")
    assert "- nextcloud: Up 3 hours (healthy) | CPU 1.20%, RAM 312MiB (8%)" in antwort
    assert "- mariadb: Up 3 hours" in antwort
    assert "- backup: Exited (0) 2 hours ago" in antwort


def test_alle_hosts_und_nicht_erreichbarer_host(dienst, hosts):
    hosts.offline.add("rp01")
    antwort = asyncio.run(dienst.anzeigen())
    assert "Container auf wb01" in antwort
    assert "rp01: nicht abrufbar – Keine SSH-Verbindung zu rp01" in antwort


def test_neustart_erst_nach_ja_mit_protokoll(dienst, hosts, tmp_path):
    protokoll = Aktionsprotokoll(tmp_path / "protokoll.db", ZONE)
    kasten = Werkzeugkasten(container_werkzeuge(dienst), Rechte(protokoll))
    angaben = {"host": "wb01", "container": "nextcloud"}

    rueckfrage = asyncio.run(kasten.ausfuehren("container_neustarten", angaben, "a1", kanal="talk"))
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT")
    assert "„nextcloud“ auf wb01 wird neu gestartet" in rueckfrage
    assert hosts.befehle("docker-restart") == []

    # Eine Frage ist kein Ja.
    asyncio.run(
        kasten.ausfuehren("container_neustarten", angaben, "a2", nachricht="Sicher?", kanal="talk")
    )
    assert hosts.befehle("docker-restart") == []

    antwort = asyncio.run(
        kasten.ausfuehren("container_neustarten", angaben, "a3", nachricht="Ja", kanal="talk")
    )
    assert "neu gestartet" in antwort
    assert hosts.befehle("docker-restart") == [("wb01", ["nextcloud"])]

    eintraege = [a for a in protokoll.liste() if a.werkzeug == "container_neustarten"]
    status = [(a.status, a.bestaetigt, a.stufe) for a in reversed(eintraege)]
    assert status[0] == ("wartet_auf_bestaetigung", False, "zerstoererisch")
    assert status[-1] == ("ausgefuehrt", True, "zerstoererisch")


def test_neustart_im_hintergrund_nie(dienst, hosts):
    from missa.core.werkzeuge import im_hintergrund

    kasten = Werkzeugkasten(container_werkzeuge(dienst))

    async def nachts():
        marke = im_hintergrund.set(True)
        try:
            return await kasten.ausfuehren(
                "container_neustarten", {"host": "wb01", "container": "nextcloud"}, "n1"
            )
        finally:
            im_hintergrund.reset(marke)

    assert "im Hintergrund" in asyncio.run(nachts())
    assert hosts.befehle("docker-restart") == []


def test_container_name_eindeutig(dienst, hosts):
    hosts.antworten[("wb01", "docker-ps")] = ps(
        container("nextcloud-app", "Up 1 hour"), container("nextcloud-db", "Up 1 hour")
    )
    kasten = Werkzeugkasten(container_werkzeuge(dienst))
    antwort = asyncio.run(
        kasten.ausfuehren("container_logs", {"host": "wb01", "container": "nextcloud"})
    )
    assert "mehreren Containern: nextcloud-app, nextcloud-db" in antwort
    antwort = asyncio.run(
        kasten.ausfuehren("container_logs", {"host": "wb01", "container": "redis"})
    )
    assert "keinen Container „redis“" in antwort


def test_logs_in_teilen_und_nur_probleme(dienst, hosts):
    zeilen = [
        f"2026-11-11T13:{i // 60:02d}:{i % 60:02d}Z GET /status.php 200 {i}" for i in range(400)
    ]
    zeilen[350] = "2026-11-11T13:55:50Z PHP Fatal error: Allowed memory size of 512 bytes exhausted"
    hosts.antworten[("wb01", "docker-logs")] = "\n".join(zeilen)
    kasten = Werkzeugkasten(container_werkzeuge(dienst))
    teil1 = asyncio.run(
        kasten.ausfuehren("container_logs", {"host": "wb01", "container": "nextcloud"})
    )
    assert "Teil 1 von" in teil1 and "teil=2" in teil1 and "nachtaufgabe_anlegen" in teil1
    assert zeilen[-1] in teil1 and zeilen[0] not in teil1
    assert hosts.befehle("docker-logs")[-1] == ("wb01", ["nextcloud", "200"])
    probleme = asyncio.run(
        kasten.ausfuehren(
            "container_logs",
            {"host": "wb01", "container": "nextcloud", "nur_probleme": True, "zeilen": 1000},
        )
    )
    assert "PHP Fatal error" in probleme and "status.php" not in probleme


# ----------------------------------------------------------------------------- Hänger


def test_haenger_werden_gemeldet_und_rueckkehr(dienst, hosts, uhr):
    assert asyncio.run(dienst.pruefen()) == []  # alles gut, altes „Exited (0)“ zählt nicht
    hosts.antworten[("wb01", "docker-ps")] = ps(
        container("nextcloud", "Up 3 hours (unhealthy)"),
        container("mariadb", "Exited (1) 1 minute ago", "exited"),
    )
    uhr.vor(minutes=5)
    alarme = asyncio.run(dienst.pruefen())
    assert sorted((a.stufe, a.titel) for a in alarme) == [
        (AlarmStufe.WICHTIG, "Container mariadb auf wb01 ist abgestürzt (Exit-Code 1)"),
        (
            AlarmStufe.WICHTIG,
            "Container nextcloud auf wb01 ist ungesund (der Healthcheck schlägt fehl)",
        ),
    ]
    uhr.vor(minutes=5)
    assert asyncio.run(dienst.pruefen()) == []  # nicht im 5-Minuten-Takt wiederholt
    hosts.antworten[("wb01", "docker-ps")] = ps(
        container("nextcloud", "Up 1 minute (healthy)"), container("mariadb", "Up 1 minute")
    )
    uhr.vor(minutes=5)
    zurueck = asyncio.run(dienst.pruefen())
    assert sorted(a.titel for a in zurueck) == [
        "Container mariadb auf wb01 läuft wieder normal",
        "Container nextcloud auf wb01 läuft wieder normal",
    ]


def test_alter_absturz_beim_ersten_blick_und_ignorierte(hosts, alarme, uhr):
    hosts.antworten[("wb01", "docker-ps")] = ps(
        container("altlast", "Exited (1) 3 weeks ago", "exited"),
        container("loop", "Restarting (1) 2 seconds ago", "restarting"),
        container("einmaljob", "Restarting (1) 2 seconds ago", "restarting"),
    )
    dienst = ContainerDienst(hosts.verbindung(), alarme, ignorieren=["wb01:einmaljob"], uhr=uhr)
    titel = [a.titel for a in asyncio.run(dienst.pruefen())]
    assert titel == ["Container loop auf wb01 startet ständig neu (Neustart-Schleife)"]


def test_host_per_ssh_weg(dienst, hosts, uhr):
    hosts.offline.add("int01")
    for _ in range(3):
        assert not [a for a in asyncio.run(dienst.pruefen()) if "int01" in a.titel]
        uhr.vor(minutes=5)
    uhr.vor(minutes=1)
    titel = [a.titel for a in asyncio.run(dienst.pruefen())]
    assert "Ich komme per SSH nicht auf int01" in titel
    hosts.offline.clear()
    uhr.vor(minutes=5)
    assert "int01 ist per SSH wieder erreichbar" in [a.titel for a in asyncio.run(dienst.pruefen())]


# ----------------------------------------------------------------------- Konfiguration und API


def test_einstellungen_aus_umgebung():
    from missa.core.config import Einstellungen, KonfigurationsFehler

    e = Einstellungen.aus_umgebung(
        {
            "SSH_HOSTS": "wb01=10.0.20.10,int01=10.0.30.20",
            "CONTAINER_IGNORIEREN": "wb01:backup, einmaljob",
            "LOGIN_BEKANNTE_IPS": "10.0.30.5",
            "LOGIN_FEHLVERSUCHE_GRENZE": "20",
            "NEXTCLOUD_LOG_HOST": "INT01",
            "NEXTCLOUD_LOG_QUELLE": "journal:nextcloud",
            "FEHLERSUCHE_DIENSTE": "nextcloud:int01:nextcloud-app",
        }
    )
    assert e.ssh_konfiguriert and e.ssh_benutzer == "missa"
    assert e.container_ignorieren == ("wb01:backup", "einmaljob")
    assert e.login_bekannte_ips == ("10.0.30.5",) and e.login_fehlversuche_grenze == 20
    assert e.login_automatisch_benutzer == ("missa",)
    assert e.nextcloud_log_host == "int01"
    from dataclasses import replace

    gueltig = replace(e, api_key="x")
    gueltig.pruefen()
    for feld, wert in (
        ("ssh_hosts", "wb01=a:b"),
        ("nextcloud_log_quelle", "irgendwo"),
        ("fehlersuche_dienste", "nurname"),
    ):
        with pytest.raises(KonfigurationsFehler):
            replace(gueltig, **{feld: wert}).pruefen()


def test_ohne_ssh_aus_und_api(einstellungen, llama, hosts):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from missa.tools import werkzeuge_laden
    from tests.conftest import API_KEY

    namen = [w.name for w in werkzeuge_laden(einstellungen)]
    assert "container_anzeigen" not in namen and "fehlersuche" not in namen

    app = create_app(einstellungen, llama_transport=llama.transport)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        assert client.get("/api/container").json() == {"ssh": False, "hosts": {}}
        assert client.get("/api/anmeldungen").json()["ssh"] is False

    app = create_app(
        einstellungen, llama_transport=llama.transport, ssh_verbindung=hosts.verbindung()
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        antwort = client.get("/api/container").json()
        assert antwort["ssh"] is True and sorted(antwort["hosts"]) == ["int01", "rp01", "wb01"]
        anmeldungen = client.get("/api/anmeldungen", params={"tage": 2}).json()
        assert anmeldungen["ssh"] is True and anmeldungen["bekannte_ips"] == []
        werkzeuge = client.get("/health").json()["werkzeuge"]
        assert {"anmeldungen_anzeigen", "ip_als_bekannt_markieren", "alarme_anzeigen"} <= set(
            werkzeuge
        )
