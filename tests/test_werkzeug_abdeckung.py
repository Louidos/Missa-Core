"""Jedes Werkzeug hat mindestens einen Test, der es beim Namen nennt."""

from __future__ import annotations

import re
from pathlib import Path

PROJEKT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"Werkzeug\(\s*\"([a-z0-9_]+)\"")


def werkzeug_namen() -> set[str]:
    namen: set[str] = set()
    for datei in (PROJEKT / "src" / "missa").rglob("*.py"):
        namen |= set(NAME.findall(datei.read_text(encoding="utf-8")))
    return namen


def test_werkzeuge_werden_gefunden():
    namen = werkzeug_namen()
    assert len(namen) > 100
    assert {"aktionen_anzeigen", "termin_loeschen", "vm_starten"} <= namen


def test_jedes_werkzeug_hat_einen_test():
    eigene = Path(__file__).name
    tests = "\n".join(
        d.read_text(encoding="utf-8")
        for d in (PROJEKT / "tests").glob("test_*.py")
        if d.name != eigene
    )
    ohne = sorted(n for n in werkzeug_namen() if not re.search(rf"[\"']{n}[\"']", tests))
    assert not ohne, f"Werkzeuge ohne Test: {', '.join(ohne)}"
