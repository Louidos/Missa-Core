"""Systemprompt."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from missa.core.prompt import ERSATZ_CHARAKTER, charakter_lesen, systemprompt_bauen, zeit_text

ZONE = ZoneInfo("Europe/Zurich")


def test_zeit_text_deutsch():
    jetzt = datetime(2026, 3, 1, 8, 5, tzinfo=ZONE)
    text = zeit_text(jetzt)
    assert text.startswith("Jetzt ist Sonntag, 1. März 2026, 08:05 Uhr (Zeitzone Europe/Zurich).")
    assert "ISO-Format: 2026-03-01T08:05" in text
    assert "- morgen: Montag, 2. März 2026 = 2026-03-02" in text
    assert "- übermorgen: Dienstag, 3. März 2026 = 2026-03-03" in text
    assert "- Sonntag, 8. März 2026 = 2026-03-08" in text


def test_charakter_lesen(tmp_path):
    datei = tmp_path / "c.md"
    assert charakter_lesen(datei) == ERSATZ_CHARAKTER
    datei.write_text("   \n", encoding="utf-8")
    assert charakter_lesen(datei) == ERSATZ_CHARAKTER
    datei.write_text("Ich bin Missa.\n", encoding="utf-8")
    assert charakter_lesen(datei) == "Ich bin Missa."


def test_reihenfolge_der_bloecke():
    jetzt = datetime(2026, 11, 9, 12, 0, tzinfo=ZONE)
    prompt = systemprompt_bauen(
        "CHARAKTER", jetzt, "- t: Test", zusatz=["## Gedächtnis\nX", ""], client_system="APP"
    )
    reihenfolge = ["CHARAKTER", "## Aktuelle Zeit", "## Werkzeuge", "## Gedächtnis", "APP"]
    positionen = [prompt.index(teil) for teil in reihenfolge]
    assert positionen == sorted(positionen)
    assert prompt.startswith("CHARAKTER")
