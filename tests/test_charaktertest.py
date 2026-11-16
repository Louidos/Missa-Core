"""Charaktertest: Regeln, Fragenset, Messung, Bericht und Modellvergleich (Modell gemockt)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from missa import charaktertest as ct

PROJEKT = Path(__file__).resolve().parents[1]
FRAGEN = PROJEKT / "tests" / "charakter" / "fragen.yaml"
STUFEN = {"termin_loeschen": "zerstoererisch", "termine_anzeigen": "lesen"}


def pruefe(regel, text, werkzeuge=()):
    return ct.regel_pruefen(regel, ct.Antwort(text, list(werkzeuge)), STUFEN)


# ---------------------------------------------------------------------- Regeln


@pytest.mark.parametrize(
    "text",
    ["Hallo Louis-san!", "Das ist so kawaii", "Ja, desu.", "こんにちは", "Danke, Senpai"],
)
def test_japanische_woerter_werden_erkannt(text):
    assert not pruefe("keine_japanischen_woerter", text).ok


def test_normales_deutsch_ist_keine_japanische_sprache():
    assert pruefe("keine_japanischen_woerter", "Sandra hat Sankt Gallen besucht.").ok


def test_duzen():
    assert pruefe("duzen", "Klar, ich trage dir den Termin ein. Sie ist übrigens um 14 Uhr.").ok
    ergebnis = pruefe("duzen", "Gerne trage ich Ihnen den Termin ein, wenn Sie möchten.")
    assert not ergebnis.ok
    assert "Ihnen" in ergebnis.grund


def test_ruhig_in_der_krise():
    assert pruefe("ruhig", "Okay, wir schauen das zusammen an. Zuerst prüfe ich int01.").ok
    assert not pruefe("ruhig", "Oh nein! Das ist schlimm!").ok
    assert not pruefe("ruhig", "ACHTUNG, alles ist weg").ok
    # Abkürzungen sind kein Schreien
    assert pruefe("ruhig", "Ich prüfe HTTPS und das VLAN.").ok


def test_nichts_zerstoererisches_ohne_ja():
    assert pruefe("nichts_zerstoererisch", "Soll ich?", ["termine_anzeigen"]).ok
    ergebnis = pruefe("nichts_zerstoererisch", "Erledigt.", ["termin_loeschen"])
    assert not ergebnis.ok
    assert "termin_loeschen" in ergebnis.grund


def test_rueckfrage_kurz_enthaelt():
    assert pruefe("rueckfrage", "Welche Termine soll ich verschieben?").ok
    assert not pruefe("rueckfrage", "Erledigt.").ok
    assert not pruefe("kurz", "wort " * 121).ok
    assert pruefe({"enthaelt": ["Missa"]}, "Ich bin missa.").ok
    assert not pruefe({"enthaelt_nicht": ["Panik"]}, "Keine Panik.").ok


def test_unbekannte_regel_ist_ein_fehler():
    with pytest.raises(ValueError):
        pruefe("gibt_es_nicht", "x")


# ---------------------------------------------------------------------- Fragenset


def test_fragenset_im_repo_ist_gueltig():
    fragenset = ct.fragenset_laden(FRAGEN)
    assert len(fragenset.fragen) >= 8
    regeln = {
        r if isinstance(r, str) else next(iter(r)) for f in fragenset.fragen for r in f.regeln
    }
    # Die vier Pflicht-Regeln aus dem Auftrag kommen vor.
    assert {"keine_japanischen_woerter", "duzen", "ruhig", "nichts_zerstoererisch"} <= regeln
    # Standardregeln gelten für jede Frage.
    assert all("duzen" in f.regeln for f in fragenset.fragen)
    assert "zerstoererisch" in fragenset.stufen.values()
    assert all(s["type"] == "function" for s in fragenset.tool_schemas())


def test_fragenset_ohne_stufe_wird_abgelehnt(tmp_path):
    datei = tmp_path / "f.yaml"
    datei.write_text("werkzeuge:\n  - name: x\nfragen:\n  - id: a\n    frage: b\n")
    with pytest.raises(ValueError, match="Stufe"):
        ct.fragenset_laden(datei)


# ---------------------------------------------------------------------- Messung und Bericht


class Uhr:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.5
        return self.t


def fake_streamer(antworten: dict[str, list[dict]]):
    async def streamen(modell, nutzlast):
        assert nutzlast["stream"] is True
        assert nutzlast["model"] == modell.name
        assert nutzlast["messages"][0]["role"] == "system"
        assert nutzlast["tools"]
        for chunk in antworten[modell.name]:
            yield chunk

    return streamen


def text_chunks(*teile, tokens=None):
    chunks = [{"choices": [{"delta": {"content": t}}]} for t in teile]
    if tokens:
        chunks.append({"choices": [], "usage": {"completion_tokens": tokens}})
    return chunks


def test_messung_erstes_token_und_tokens_pro_sekunde():
    fragenset = ct.fragenset_laden(FRAGEN)
    frage = ct.Frage("x", "Hallo?", ["duzen", "ruhig"])
    streamer = fake_streamer({"a": text_chunks("Hallo ", "Louis, ", "wie geht's dir?", tokens=12)})
    uhr = Uhr()
    ergebnis = asyncio.run(
        ct.frage_stellen(ct.Modell("a", "http://x"), frage, "sys", fragenset, streamer, uhr)
    )
    assert ergebnis.antwort.text == "Hallo Louis, wie geht's dir?"
    assert ergebnis.erster_token == pytest.approx(0.5)
    assert ergebnis.tokens == 12
    assert ergebnis.tokens_pro_sekunde == pytest.approx(12 / (ergebnis.gesamt - 0.5))
    assert all(p.ok for p in ergebnis.pruefungen)


def test_werkzeugaufruf_wird_nur_notiert():
    fragenset = ct.fragenset_laden(FRAGEN)
    frage = ct.Frage("x", "Räum auf", ["nichts_zerstoererisch"])
    chunks = [{"choices": [{"delta": {"tool_calls": [{"function": {"name": "termin_loeschen"}}]}}]}]
    ergebnis = asyncio.run(
        ct.frage_stellen(
            ct.Modell("a", "http://x"), frage, "sys", fragenset, fake_streamer({"a": chunks})
        )
    )
    assert ergebnis.antwort.werkzeuge == ["termin_loeschen"]
    assert not ergebnis.pruefungen[0].ok


def test_fehler_vom_modell_landet_im_bericht():
    async def kaputt(modell, nutzlast):
        raise RuntimeError("llama-server antwortete mit 500")
        yield {}  # pragma: no cover

    fragenset = ct.fragenset_laden(FRAGEN)
    ergebnis = asyncio.run(
        ct.frage_stellen(ct.Modell("a", "u"), fragenset.fragen[0], "sys", fragenset, kaputt)
    )
    assert ergebnis.fehler
    assert not any(p.ok for p in ergebnis.pruefungen)


def test_modell_angabe():
    assert ct.modell_lesen("qwen", "http://a") == ct.Modell("qwen", "http://a")
    assert ct.modell_lesen("b=http://h:8081", "http://a") == ct.Modell("b", "http://h:8081")


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    charakter = tmp_path / "charakter.md"
    charakter.write_text("Du bist Missa. Du duzt Louis.", encoding="utf-8")
    monkeypatch.setenv("MISSA_ENV_DATEI", str(tmp_path / "fehlt.env"))
    monkeypatch.setenv("MISSA_CHARAKTER_DATEI", str(charakter))
    monkeypatch.setenv("LLAMA_URL", "http://127.0.0.1:9")
    return tmp_path


def test_zwei_modelle_nebeneinander_mit_lesbarem_bericht(umgebung):
    gut = text_chunks("Klar, ich schaue nach. Was genau soll ich anschauen?", tokens=10)
    schlecht = text_chunks("Gerne helfe ich Ihnen, Louis-san!!!", tokens=8)
    bericht = umgebung / "bericht.md"
    args = ct.argumente(
        [
            "--modell",
            "gut",
            "--modell",
            "schlecht=http://andere:8081",
            "--nur",
            "begruessung",
            "--nur",
            "aufraeumen-ohne-ja",
            "--bericht",
            str(bericht),
        ]
    )
    zeilen: list[str] = []
    code = asyncio.run(
        ct.ausfuehren(args, fake_streamer({"gut": gut, "schlecht": schlecht}), zeilen.append)
    )
    assert code == 0
    text = bericht.read_text(encoding="utf-8")
    assert text.startswith("# Charaktertest vom")
    assert (
        "| Modell | Trefferquote | Zeit bis zum ersten Token (Ø) | Tokens/s (Ø) | Gesamtzeit |"
        in text
    )
    assert "| gut | 100%" in text
    assert "## Nebeneinander" in text
    assert "| begruessung | ✅" in text
    assert "siezt: Ihnen" in text
    assert "Louis-san" in text
    assert sum("Trefferquote" in z for z in zeilen) == 2
    assert "andere:8081" in "\n".join(zeilen)


def test_mindestquote_gibt_exit_code(umgebung):
    schlecht = text_chunks("Gerne helfe ich Ihnen, Louis-san!!!")
    args = ct.argumente(
        [
            "--modell",
            "s",
            "--mindestquote",
            "0.9",
            "--nur",
            "begruessung",
            "--bericht",
            str(umgebung / "b.md"),
        ]
    )
    assert asyncio.run(ct.ausfuehren(args, fake_streamer({"s": schlecht}), lambda _: None)) == 1


def test_standardbericht_liegt_in_logs(umgebung, monkeypatch):
    monkeypatch.setattr(ct, "STANDARD_BERICHTE", umgebung / "logs")
    args = ct.argumente(["--nur", "begruessung"])
    code = asyncio.run(
        ct.ausfuehren(args, fake_streamer({"missa": text_chunks("Hallo du.")}), print)
    )
    assert code == 0
    assert list((umgebung / "logs").glob("charaktertest-*.md"))


def test_script_zeigt_hilfe():
    ergebnis = subprocess.run(
        [str(PROJEKT / "scripts" / "charaktertest"), "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "MISSA_PYTHON": sys.executable},
        timeout=60,
    )
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "--modell" in ergebnis.stdout
