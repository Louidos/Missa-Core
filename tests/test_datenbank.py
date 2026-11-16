"""Gemeinsame Hilfsfunktion für UPDATE (missa.core.datenbank.zeile_aendern)."""

from __future__ import annotations

import sqlite3

import pytest

from missa.core.datenbank import zeile_aendern


@pytest.fixture
def db() -> sqlite3.Connection:
    verbindung = sqlite3.connect(":memory:")
    verbindung.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b INTEGER, v INTEGER)")
    verbindung.execute("INSERT INTO t (id, a, b, v) VALUES (1, 'x', 1, 0)")
    return verbindung


def test_aendert_nur_erlaubte_spalten(db):
    assert zeile_aendern(db, "t", {"a": "neu", "b": 2}, wo="id", wert=1, erlaubt={"a", "b"}) == 1
    assert db.execute("SELECT a, b FROM t").fetchone() == ("neu", 2)


def test_zusatz_aus_dem_code(db):
    zeile_aendern(db, "t", {"a": "y"}, wo="id", wert=1, erlaubt={"a"}, zusatz="v = v + 1")
    assert db.execute("SELECT a, v FROM t").fetchone() == ("y", 1)


@pytest.mark.parametrize(
    "felder",
    [
        {"v": 5},  # nicht in der Liste
        {"a = 'x', b": 1},  # eingeschleuster Ausdruck
        {"a; DROP TABLE t; --": 1},
    ],
)
def test_unbekannte_oder_kaputte_spalten_werden_abgelehnt(db, felder):
    with pytest.raises(ValueError):
        zeile_aendern(db, "t", felder, wo="id", wert=1, erlaubt={"a", "b"})
    assert db.execute("SELECT a, b, v FROM t").fetchone() == ("x", 1, 0)


def test_werte_sind_immer_parameter(db):
    zeile_aendern(db, "t", {"a": "x'; DROP TABLE t; --"}, wo="id", wert=1, erlaubt={"a"})
    assert db.execute("SELECT a FROM t").fetchone() == ("x'; DROP TABLE t; --",)


def test_ungueltiger_tabellen_oder_bedingungsname(db):
    with pytest.raises(ValueError):
        zeile_aendern(db, "t; --", {"a": "y"}, wo="id", wert=1, erlaubt={"a"})
    with pytest.raises(ValueError):
        zeile_aendern(db, "t", {"a": "y"}, wo="id OR 1=1", wert=1, erlaubt={"a"})
    with pytest.raises(ValueError):
        zeile_aendern(db, "t", {}, wo="id", wert=1, erlaubt={"a"})
