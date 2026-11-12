"""Firewall-Logs (ZyWALL): Einlesen, Zusammenfassung – und kein Werkzeug ändert Regeln."""

from __future__ import annotations

import asyncio
import dataclasses
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.config import Einstellungen
from missa.core.werkzeuge import Stufe, Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.zywall import zeile_lesen
from missa.tools.firewall import FirewallLog, firewall_werkzeuge
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

GESTERN = datetime(2026, 11, 11, 0, 0, tzinfo=ZONE)


def zeile(zeit: datetime, src: str, dst: str, msg: str = "Match default rule, DROP",
          note: str = "ACCESS BLOCK", iso: bool = True) -> str:  # fmt: skip
    kopf = zeit.isoformat() if iso else zeit.strftime("%b %d %H:%M:%S")
    return (
        f'{kopf} zywall src="{src}" dst="{dst}" msg="{msg}" note="{note}" user="unknown" '
        f'devID="bc9911aabbcc" cat="Security Policy" proto="tcp"\n'
    )


def beispiel_log() -> str:
    """Ein Tag mit Port-Scan, gehäuften Blocks, normalem Verkehr und einem neuen Ziel."""
    teile = []
    # Port-Scan: 203.0.113.7 fragt in 3 Minuten 20 Ports an
    for i in range(20):
        t = GESTERN + timedelta(hours=3, seconds=i * 9)
        teile.append(zeile(t, f"203.0.113.7:{40000 + i}", f"198.51.100.10:{20 + i}"))
    # gehäufte Blocks: 198.51.100.66 versucht 150× SSH
    for i in range(150):
        t = GESTERN + timedelta(hours=8, minutes=i % 60, seconds=i)
        teile.append(zeile(t, f"198.51.100.66:{50000 + i}", "198.51.100.10:22"))
    # erlaubter Verkehr aus dem eigenen Netz, darunter ein Ziel, das es vorher nie gab
    teile.append(zeile(GESTERN + timedelta(hours=10), "10.0.50.10:41000", "192.0.2.80:443",
                       "Match rule LAN_to_WAN, ALLOW", "ACCESS FORWARD"))  # fmt: skip
    teile.append(zeile(GESTERN + timedelta(hours=11), "10.0.50.10:41001", "192.0.2.99:4444",
                       "Match rule LAN_to_WAN, ALLOW", "ACCESS FORWARD"))  # fmt: skip
    teile.append("2026-11-11T12:00:00+01:00 zywall System: Firmware check done\n")
    return "".join(teile)


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 12, 7, 30, tzinfo=ZONE))


@pytest.fixture
def alarme(tmp_path: Path, uhr) -> Alarme:
    planer = Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)
    return Alarme(planer.datei, ZONE, planer)


@pytest.fixture
def logdatei(tmp_path: Path) -> Path:
    return tmp_path / "zywall.log"


@pytest.fixture
def firewall(logdatei, alarme, tmp_path, uhr) -> FirewallLog:
    return FirewallLog(logdatei, tmp_path / "missa.db", ZONE, alarme, uhr=uhr)


def test_zeile_lesen():
    jetzt = datetime(2026, 11, 12, 8, 0, tzinfo=ZONE)
    e = zeile_lesen(zeile(GESTERN, "203.0.113.7:54321", "198.51.100.10:22"), ZONE, jetzt)
    assert (e.src_ip, e.src_port, e.dst_ip, e.dst_port, e.aktion) == (
        "203.0.113.7",
        54321,
        "198.51.100.10",
        22,
        "blockiert",
    )
    klassisch = zeile_lesen(
        zeile(datetime(2026, 11, 11, 23, 5, tzinfo=ZONE), "[2001:db8::1]:443", "10.0.0.1:80",
              "Match rule X, ALLOW", "ACCESS FORWARD", iso=False), ZONE, jetzt,
    )  # fmt: skip
    assert klassisch.zeit == datetime(2026, 11, 11, 23, 5, tzinfo=ZONE)
    assert klassisch.src_ip == "2001:db8::1" and klassisch.aktion == "erlaubt"
    assert zeile_lesen("2026-11-11T12:00:00+01:00 zywall System: ok", ZONE, jetzt) is None


def test_was_war_gestern_an_der_firewall_los(firewall, logdatei):
    # Vortage: normaler Verkehr zu 192.0.2.80:443
    vorher = "".join(
        zeile(GESTERN - timedelta(days=d), "10.0.50.10:40000", "192.0.2.80:443",
              "Match rule LAN_to_WAN, ALLOW", "ACCESS FORWARD")
        for d in range(1, 4)
    )  # fmt: skip
    logdatei.write_text(vorher + beispiel_log(), encoding="utf-8")
    kasten = Werkzeugkasten(firewall_werkzeuge(firewall))
    text = asyncio.run(kasten.ausfuehren("firewall_zusammenfassung", {"tag": "gestern"}))
    assert text.startswith("Firewall am 11.11.2026: 172 Einträge, davon 170 geblockt.")
    assert "- 198.51.100.66: 150× geblockt (vor allem Port 22)" in text
    assert "- 203.0.113.7: 20 verschiedene Ports ab 03:00 Uhr" in text
    assert "- 10.0.50.10 → 192.0.2.99:4444 (1×)" in text
    assert "192.0.2.80:443" not in text  # das Ziel gab es schon vorher


def test_taeglicher_bericht_und_weiterlesen(firewall, logdatei):
    logdatei.write_text(beispiel_log(), encoding="utf-8")
    alarm = asyncio.run(firewall.taeglich_berichten())
    assert alarm.stufe is AlarmStufe.INFO and alarm.titel.startswith("Firewall am 11.11.2026")
    assert "Port-Scans" in alarm.text
    # nur neue Zeilen werden gelesen
    assert firewall.einlesen() == 0
    with logdatei.open("a", encoding="utf-8") as datei:
        datei.write(zeile(GESTERN + timedelta(hours=20), "203.0.113.9:1", "198.51.100.10:3389"))
    assert firewall.einlesen() == 1


def test_rotation_und_unvollstaendige_zeile(firewall, logdatei, tmp_path):
    logdatei.write_text(beispiel_log(), encoding="utf-8")
    assert firewall.einlesen() == 172
    alt = tmp_path / "zywall.log.1"
    os.replace(logdatei, alt)
    halb = zeile(GESTERN + timedelta(hours=21), "203.0.113.9:1", "198.51.100.10:443")
    logdatei.write_text(halb + halb[:30], encoding="utf-8")  # zweite Zeile noch unvollständig
    assert firewall.einlesen() == 1
    with logdatei.open("a", encoding="utf-8") as datei:
        datei.write(halb[30:])
    assert firewall.einlesen() == 1


def test_ohne_datei(alarme, tmp_path, uhr):
    firewall = FirewallLog(tmp_path / "fehlt.log", tmp_path / "missa.db", ZONE, alarme, uhr=uhr)
    assert firewall.einlesen() == 0
    assert "keine Log-Einträge" in firewall.anzeigen("gestern")
    assert "nicht lesbar" in firewall.anzeigen("gestern")


def test_es_gibt_kein_werkzeug_das_firewall_regeln_aendert(tmp_path):
    """Auch voll eingerichtet: an der Firewall kann Missa nur lesen."""
    from missa.core.app import create_app

    e = Einstellungen(
        api_key="x",
        daten=tmp_path,
        firewall_log_datei=tmp_path / "zywall.log",
        proxmox_url="https://pve.test:8006",
        proxmox_token_id="missa@pve!monitor",
        proxmox_token_secret="x",
        ssh_hosts="wb01=10.0.20.10",
    )
    app = create_app(e)
    kasten: Werkzeugkasten = app.state.dienst.werkzeuge
    namen = [w.name for w in kasten]
    assert "firewall_zusammenfassung" in namen and "homelab_anzeigen" in namen
    for werkzeug in kasten:
        text = f"{werkzeug.name} {werkzeug.beschreibung}".casefold()
        if "firewall" in werkzeug.name or "zywall" in text:
            assert werkzeug.stufe is Stufe.LESEN, werkzeug.name
        aendernd = ("regel_anlegen", "regel_aendern", "regel_loeschen", "freigeben", "sperren")
        assert not ("firewall" in text and any(w in werkzeug.name for w in aendernd)), werkzeug.name
    # keine Zugangsdaten zur Firewall in der Konfiguration
    felder = [f.name for f in dataclasses.fields(Einstellungen)]
    assert not [f for f in felder if ("firewall" in f or "zywall" in f) and any(
        g in f for g in ("pass", "token", "key", "user", "secret", "url")
    )]  # fmt: skip
