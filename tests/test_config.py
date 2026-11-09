"""Konfiguration aus Umgebungsvariablen."""

from __future__ import annotations

from pathlib import Path

import pytest

from missa.core.config import Einstellungen, KonfigurationsFehler, env_datei_laden


def test_standardwerte():
    e = Einstellungen.aus_umgebung({})
    assert e.host == "0.0.0.0"
    assert e.port == 8000
    assert e.api_key == ""
    assert e.charakter_datei == Path("/etc/missa/charakter.md")
    assert e.daten == Path("/opt/missa/data")
    assert e.datenbank == Path("/opt/missa/data/missa.db")
    assert e.llama_url == "http://127.0.0.1:8080"
    assert e.llama_api_key_file == Path("/etc/missa/llama.key")
    assert e.timezone == "Europe/Zurich"


def test_werte_aus_umgebung():
    e = Einstellungen.aus_umgebung(
        {
            "MISSA_HOST": "127.0.0.1",
            "MISSA_PORT": "9000",
            "MISSA_API_KEY": " geheim ",
            "MISSA_CHARAKTER_DATEI": "/tmp/c.md",
            "MISSA_DATEN": "/tmp/daten",
            "LLAMA_URL": "http://ai01:8080/",
            "LLAMA_API_KEY_FILE": "/tmp/k",
            "LLAMA_TIMEOUT": "30",
            "TIMEZONE": "Europe/Berlin",
        }
    )
    assert (e.host, e.port, e.api_key) == ("127.0.0.1", 9000, "geheim")
    assert e.charakter_datei == Path("/tmp/c.md")
    assert e.datenbank == Path("/tmp/daten/missa.db")
    assert e.llama_url == "http://ai01:8080"
    assert e.llama_timeout == 30.0
    assert e.zone.key == "Europe/Berlin"


def test_leere_werte_nehmen_standard():
    assert Einstellungen.aus_umgebung({"MISSA_PORT": "", "MISSA_HOST": " "}).port == 8000


def test_ungueltiger_port():
    with pytest.raises(KonfigurationsFehler):
        Einstellungen.aus_umgebung({"MISSA_PORT": "acht"})


def test_pruefen_verlangt_api_key():
    with pytest.raises(KonfigurationsFehler, match="MISSA_API_KEY"):
        Einstellungen().pruefen()
    Einstellungen(api_key="x").pruefen()


def test_pruefen_erkennt_falsche_zeitzone():
    with pytest.raises(KonfigurationsFehler, match="Zeitzone"):
        Einstellungen(api_key="x", timezone="Mars/Olympus").pruefen()


def test_env_datei_laden(tmp_path):
    datei = tmp_path / "missa.env"
    datei.write_text(
        "# Kommentar\n\nMISSA_PORT=9001\nexport MISSA_HOST='127.0.0.1'\n"
        'MISSA_API_KEY="a=b"\nSCHON_DA=neu\nkaputt\n',
        encoding="utf-8",
    )
    env = {"SCHON_DA": "alt"}
    assert env_datei_laden(datei, env) is True
    assert env == {
        "SCHON_DA": "alt",
        "MISSA_PORT": "9001",
        "MISSA_HOST": "127.0.0.1",
        "MISSA_API_KEY": "a=b",
    }


def test_env_datei_fehlt(tmp_path):
    assert env_datei_laden(tmp_path / "gibtsnicht", {}) is False
