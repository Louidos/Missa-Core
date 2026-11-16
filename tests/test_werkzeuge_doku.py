"""Jedes Werkzeug steht in docs/werkzeuge.md – und dort steht keins, das es nicht gibt."""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_werkzeug_abdeckung import werkzeug_namen

DOKU = Path(__file__).resolve().parents[1] / "docs" / "werkzeuge.md"
ZEILE = re.compile(r"^\| `([a-z0-9_]+)` \|", re.MULTILINE)


def test_jedes_werkzeug_steht_in_der_doku():
    dokumentiert = set(ZEILE.findall(DOKU.read_text(encoding="utf-8")))
    fehlt = sorted(werkzeug_namen() - dokumentiert)
    assert not fehlt, f"In docs/werkzeuge.md fehlen: {', '.join(fehlt)}"


def test_doku_nennt_keine_alten_werkzeuge():
    dokumentiert = set(ZEILE.findall(DOKU.read_text(encoding="utf-8")))
    zu_viel = sorted(dokumentiert - werkzeug_namen())
    assert not zu_viel, f"docs/werkzeuge.md nennt unbekannte Werkzeuge: {', '.join(zu_viel)}"
