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


# ----------------------------------------------------------------------------- Rechner


def _app(einstellungen, llama, aktiv: bool):
    import dataclasses

    from missa.core.app import create_app
    from missa.core.werkzeuge import Stufe, Werkzeug, Werkzeugkasten
    from tests.conftest import JETZT

    kasten = Werkzeugkasten(
        [Werkzeug("notiz", "Notiert.", {"type": "object"}, lambda: "ok", stufe=Stufe.SCHREIBEN)]
    )
    return create_app(
        dataclasses.replace(einstellungen, geraete_aktiv=aktiv),
        werkzeuge=kasten,
        llama_transport=llama.transport,
        uhr=lambda: JETZT,
    )


def test_ohne_geraet_ist_der_systemprompt_unveraendert(einstellungen, llama):
    ohne = _app(einstellungen, llama, aktiv=False).state.dienst
    mit = _app(einstellungen, llama, aktiv=True).state.dienst
    for sitzung in (None, "laptop-chat", "talk:abc"):
        assert mit.systemprompt("Hallo", None, sitzung) == ohne.systemprompt("Hallo", None, sitzung)
        assert "## Rechner" not in mit.systemprompt("Hallo", None, sitzung)


def test_rechner_abschnitt_nur_in_der_sitzung_des_geraets(einstellungen, llama):
    import asyncio

    from missa.core.geraete import GeraeteTokens
    from tests.geraet_fake import werkzeug

    app = _app(einstellungen, llama, aktiv=True)
    dienst, geraete = app.state.dienst, app.state.geraete

    async def ablauf():
        GeraeteTokens(geraete.tokens.datei).anlegen("laptop")
        geraete.anmelden(
            "laptop",
            protokoll=1,
            geraet_name="laptop",
            sitzungen=["laptop-chat"],
            werkzeuge=[
                werkzeug("rechner_dateien_suchen"),
                werkzeug("rechner_oeffnen", "schreiben"),
            ],
        )
        im_chat = dienst.systemprompt("Öffne X", None, "laptop-chat")
        in_talk = dienst.systemprompt("Öffne X", None, "talk:abc")
        await geraete.schliessen()
        return im_chat, in_talk

    im_chat, in_talk = asyncio.run(ablauf())
    assert "## Rechner" in im_chat and "„laptop“" in im_chat
    assert "Erst suchen" in im_chat and "Rate nie einen Pfad" in im_chat
    assert "rechner_oeffnen" in im_chat
    assert "## Rechner" not in in_talk and "rechner_" not in in_talk
    # Der Abschnitt bleibt kurz (das Modell läuft auf der CPU).
    abschnitt = im_chat.split("## Rechner", 1)[1]
    assert len(abschnitt) < 500
