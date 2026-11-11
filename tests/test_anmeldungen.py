"""Login-Überwachung: SSH- und Nextcloud-Anmeldungen, Fehlversuche, neue IPs, ungewohnte Zeiten."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.anmeldungen import NextcloudLog, ist_privat, nextcloud_lesen, ssh_lesen
from missa.tools.anmeldungen import LoginWaechter, login_werkzeuge
from tests.ssh_fake import FakeHosts
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr


def journal(zeit: datetime, text: str, host: str = "wb01", programm: str = "sshd") -> str:
    return f"{zeit.astimezone(ZONE):%Y-%m-%dT%H:%M:%S%z} {host} {programm}[42]: {text}"


def fehlversuch(zeit: datetime, ip: str = "203.0.113.7", benutzer: str = "root", port=0) -> str:
    return journal(zeit, f"Failed password for {benutzer} from {ip} port {50000 + port} ssh2")


def erfolg(zeit: datetime, ip: str, benutzer: str = "louis") -> str:
    return journal(zeit, f"Accepted publickey for {benutzer} from {ip} port 51234 ssh2: ED25519 x")


class Logs:
    """Hält die Zeilen, die ``ssh-log`` je Host liefert (wie journalctl --since)."""

    def __init__(self) -> None:
        self.zeilen: dict[str, list[str]] = {}

    def neu(self, host: str, *zeilen: str) -> None:
        self.zeilen.setdefault(host, []).extend(zeilen)

    def antwort(self, host: str):
        def lesen(argumente: list[str]) -> str:
            seit = int(next(a for a in argumente if a.startswith("seit=@"))[6:])
            return "\n".join(
                z
                for z in self.zeilen.get(host, [])
                if datetime.strptime(z.split()[0], "%Y-%m-%dT%H:%M:%S%z").timestamp() >= seit
            )

        return lesen


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
def logs() -> Logs:
    return Logs()


@pytest.fixture
def hosts(logs) -> FakeHosts:
    fake = FakeHosts("wb01=10.0.20.10,int01=10.0.30.20")
    fake.antworten[("wb01", "ssh-log")] = logs.antwort("wb01")
    fake.antworten[("int01", "ssh-log")] = logs.antwort("int01")
    return fake


@pytest.fixture
def waechter(hosts, alarme, planer, uhr) -> LoginWaechter:
    return LoginWaechter(hosts.verbindung(), alarme, planer.datei, ZONE, uhr=uhr)


def runde(waechter: LoginWaechter, uhr: Uhr, minuten: int = 5) -> list:
    gemeldet = asyncio.run(waechter.pruefen())
    uhr.vor(minutes=minuten)
    return gemeldet


# ----------------------------------------------------------------------------- Lesen


def test_ssh_zeilen_jeder_versuch_zaehlt_einmal(uhr):
    t = uhr.jetzt
    text = "\n".join(
        [
            erfolg(t, "10.0.30.5"),
            journal(t, "Invalid user admin from 203.0.113.7 port 5555"),
            journal(t, "Failed password for invalid user admin from 203.0.113.7 port 5555 ssh2"),
            journal(t, "Invalid user test from 198.51.100.9 port 1111", programm="sshd-session"),
            journal(
                t,
                "Connection closed by invalid user test 198.51.100.9 port 1111 [preauth]",
                programm="sshd-session",
            ),
            journal(t, "pam_unix(sshd:auth): authentication failure; rhost=203.0.113.7"),
            journal(t, "Server listening on 0.0.0.0 port 22."),
            "-- Boot 1234 --",
        ]
    )
    ereignisse = ssh_lesen("wb01", text)
    assert [(e.erfolg, e.benutzer, e.ip) for e in ereignisse] == [
        (True, "louis", "10.0.30.5"),
        (False, "admin", "203.0.113.7"),
        (False, "test", "198.51.100.9"),
    ]


def test_nextcloud_zeilen():
    text = "\n".join(
        [
            json.dumps(
                {
                    "time": "2026-11-11T13:00:00+00:00",
                    "remoteAddr": "203.0.113.7",
                    "message": "Login failed: 'louis' (Remote IP: '203.0.113.7')",
                }
            ),
            "2026-11-11T14:05:00+0100 int01 Nextcloud[5]: "
            + json.dumps(
                {
                    "time": "2026-11-11T13:05:00+00:00",
                    "remoteAddr": "10.0.30.5",
                    "app": "admin_audit",
                    "message": 'Login successful: "louis"',
                }
            ),
            json.dumps({"time": "2026-11-11T13:06:00+00:00", "message": "irgendwas"}),
        ]
    )
    ereignisse = nextcloud_lesen("int01", text)
    assert [(e.erfolg, e.benutzer, e.ip) for e in ereignisse] == [
        (False, "louis", "203.0.113.7"),
        (True, "louis", "10.0.30.5"),
    ]


def test_ist_privat():
    assert ist_privat("10.0.30.5") and ist_privat("192.168.1.2") and ist_privat("fd00::1")
    assert ist_privat("100.101.102.103")  # Tailscale
    assert not ist_privat("203.0.113.7") and not ist_privat("8.8.8.8") and not ist_privat("xyz")


# ----------------------------------------------------------------------------- Melden


def test_zehn_fehlgeschlagene_ssh_logins_sind_wichtig(waechter, logs, uhr):
    logs.neu("wb01", fehlversuch(uhr.jetzt - timedelta(days=1)))
    assert runde(waechter, uhr) == []  # erster Lauf: still lernen
    logs.neu("wb01", *(fehlversuch(uhr.jetzt, port=i) for i in range(9)))
    assert runde(waechter, uhr, 1) == []  # 9 sind noch keine 10
    logs.neu(
        "wb01", fehlversuch(uhr.jetzt, port=9), fehlversuch(uhr.jetzt, "198.51.100.9", "admin")
    )
    alarme = runde(waechter, uhr, 1)
    assert [(a.stufe, a.titel) for a in alarme] == [
        (AlarmStufe.WICHTIG, "11 fehlgeschlagene SSH-Anmeldungen auf wb01")
    ]
    assert "vor allem von 203.0.113.7 (10×), 198.51.100.9 (1×)" in alarme[0].text
    assert "root, admin" in alarme[0].text
    # weitere Versuche: nicht im Minutentakt
    logs.neu("wb01", *(fehlversuch(uhr.jetzt, port=20 + i) for i in range(5)))
    assert runde(waechter, uhr, 1) == []


def test_neue_ip_von_aussen_wichtig_aus_dem_netz_info(waechter, logs, uhr):
    logs.neu("wb01", erfolg(uhr.jetzt - timedelta(hours=3), "10.0.30.5"))
    assert runde(waechter, uhr) == []  # gelernt
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.30.5"))  # schon gesehen
    assert runde(waechter, uhr) == []
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.40.8"), erfolg(uhr.jetzt, "198.51.100.23"))
    alarme = sorted(runde(waechter, uhr), key=lambda a: a.titel)
    assert [(a.stufe, a.titel) for a in alarme] == [
        (AlarmStufe.INFO, "Neue IP bei SSH auf wb01: louis von 10.0.40.8"),
        (AlarmStufe.WICHTIG, "Neue IP bei SSH auf wb01: louis von 198.51.100.23"),
    ]
    assert "„198.51.100.23 ist bekannt“" in alarme[1].text
    logs.neu("wb01", erfolg(uhr.jetzt, "198.51.100.23"))
    assert runde(waechter, uhr) == []  # einmal reicht


def test_bekannte_ip_meldet_nichts(waechter, logs, uhr):
    runde(waechter, uhr)
    kasten = Werkzeugkasten(login_werkzeuge(waechter))
    antwort = asyncio.run(
        kasten.ausfuehren("ip_als_bekannt_markieren", {"ip": "198.51.100.23", "notiz": "Handy"})
    )
    assert "198.51.100.23 ist jetzt als bekannt markiert" in antwort
    logs.neu("wb01", erfolg(uhr.jetzt, "198.51.100.23"))
    assert runde(waechter, uhr) == []
    liste = asyncio.run(kasten.ausfuehren("bekannte_ips_anzeigen", {}))
    assert "- 198.51.100.23: Handy" in liste
    weg = asyncio.run(kasten.ausfuehren("ip_nicht_mehr_bekannt", {"ip": "198.51.100.23"}))
    assert "nicht mehr als bekannt" in weg
    falsch = asyncio.run(kasten.ausfuehren("ip_als_bekannt_markieren", {"ip": "kein.host"}))
    assert falsch.startswith("Fehler:")


def test_ungewohnte_zeit(waechter, logs, uhr):
    runde(waechter, uhr)
    uhr.jetzt = datetime(2026, 11, 12, 3, 12, tzinfo=ZONE)
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.30.5"))
    alarme = runde(waechter, uhr)
    # neue IP aus dem eigenen Netz, aber um 3 Uhr nachts → wichtig
    assert [a.stufe for a in alarme] == [AlarmStufe.WICHTIG]
    assert "zu einer ungewohnten Zeit" in alarme[0].text
    uhr.jetzt = datetime(2026, 11, 13, 3, 30, tzinfo=ZONE)
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.30.5"))
    alarme = runde(waechter, uhr)
    assert [(a.stufe, a.titel) for a in alarme] == [
        (AlarmStufe.INFO, "SSH-Anmeldung zu ungewohnter Zeit: louis auf wb01")
    ]
    # Missa selbst (automatischer Benutzer) zählt nicht als ungewohnt
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.30.5", benutzer="missa"))
    assert runde(waechter, uhr) == []


def test_gelernter_tagesverlauf(waechter, logs, uhr):
    # 30 Anmeldungen am Nachmittag in den letzten Tagen
    alt = [
        erfolg(uhr.jetzt - timedelta(days=tag, hours=stunde), "10.0.30.5")
        for tag in range(1, 7)
        for stunde in range(5)
    ]
    logs.neu("wb01", *alt)
    runde(waechter, uhr)
    uhr.jetzt = datetime(2026, 11, 11, 22, 40, tzinfo=ZONE)  # außerhalb der Nacht, aber selten
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.30.5"))
    alarme = runde(waechter, uhr)
    assert [a.titel for a in alarme] == ["SSH-Anmeldung zu ungewohnter Zeit: louis auf wb01"]
    uhr.jetzt = datetime(2026, 11, 12, 12, 0, tzinfo=ZONE)  # übliche Zeit
    logs.neu("wb01", erfolg(uhr.jetzt, "10.0.30.5"))
    assert runde(waechter, uhr) == []


def test_bekannte_ips_aus_konfiguration_und_einstellungen(hosts, alarme, planer, logs, uhr):
    waechter = LoginWaechter(
        hosts.verbindung(),
        alarme,
        planer.datei,
        ZONE,
        bekannte_ips=["198.51.100.23", "unsinn"],
        uhr=uhr,
    )
    assert [b["ip"] for b in waechter.bekannte()] == ["198.51.100.23"]
    kasten = Werkzeugkasten(login_werkzeuge(waechter))
    antwort = asyncio.run(
        kasten.ausfuehren("login_einstellungen", {"fehlversuche_grenze": 3, "fenster_minuten": 5})
    )
    assert "ab 3 fehlgeschlagenen Anmeldungen in 5 Minuten" in antwort
    runde(waechter, uhr)
    logs.neu("wb01", *(fehlversuch(uhr.jetzt, port=i) for i in range(3)))
    assert len(runde(waechter, uhr, 1)) == 1


def test_nextcloud_fehlversuche(hosts, alarme, planer, uhr):
    zeilen: list[str] = []
    hosts.antworten[("int01", "nextcloud-log")] = lambda _a: "\n".join(zeilen)
    waechter = LoginWaechter(
        hosts.verbindung(),
        alarme,
        planer.datei,
        ZONE,
        hosts=["wb01"],
        nextcloud_log=NextcloudLog("int01", "datei"),
        uhr=uhr,
    )
    runde(waechter, uhr)
    for i in range(10):
        zeit = (uhr.jetzt + timedelta(seconds=i)).isoformat()
        zeilen.append(
            json.dumps(
                {"time": zeit, "message": f"Login failed: 'louis' (Remote IP: '203.0.113.{i}')"}
            )
        )
    alarme_jetzt = runde(waechter, uhr, 1)
    assert [a.titel for a in alarme_jetzt] == ["10 fehlgeschlagene Nextcloud-Anmeldungen auf int01"]


def test_zusammenfassung_und_nicht_erreichbarer_host(waechter, hosts, logs, uhr):
    logs.neu("wb01", erfolg(uhr.jetzt - timedelta(minutes=30), "10.0.30.5"))
    logs.neu("wb01", fehlversuch(uhr.jetzt - timedelta(minutes=20)))
    hosts.offline.add("int01")
    runde(waechter, uhr)
    kasten = Werkzeugkasten(login_werkzeuge(waechter))
    text = asyncio.run(kasten.ausfuehren("anmeldungen_anzeigen", {}))
    assert "1 erfolgreich, 1 fehlgeschlagen" in text
    assert "SSH wb01: louis von 10.0.30.5" in text
    assert "Fehlversuche vor allem von: 203.0.113.7 (1×)" in text
    assert "Nicht lesbar: ssh:int01: Keine SSH-Verbindung zu int01" in text
