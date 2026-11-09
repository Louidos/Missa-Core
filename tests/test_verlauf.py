"""Gesprächsverlauf in SQLite."""

from __future__ import annotations

from missa.core.verlauf import Verlauf


def test_speichern_und_laden(tmp_path):
    verlauf = Verlauf(tmp_path / "unter" / "missa.db")
    verlauf.austausch_speichern("s1", "Frage", "Antwort")
    verlauf.speichern("s2", "user", "Andere Sitzung")
    assert verlauf.laden("s1") == [
        {"role": "user", "content": "Frage"},
        {"role": "assistant", "content": "Antwort"},
    ]
    assert verlauf.laden("leer") == []


def test_laden_begrenzt_und_in_reihenfolge(tmp_path):
    verlauf = Verlauf(tmp_path / "missa.db")
    for nummer in range(5):
        verlauf.speichern("s", "user", str(nummer))
    assert [n["content"] for n in verlauf.laden("s", 3)] == ["2", "3", "4"]


def test_bleibt_nach_neuem_oeffnen_erhalten(tmp_path):
    Verlauf(tmp_path / "missa.db").speichern("s", "user", "bleibt")
    assert Verlauf(tmp_path / "missa.db").laden("s")[0]["content"] == "bleibt"
