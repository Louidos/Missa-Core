"""Welche Werkzeuge mit welcher Konfiguration geladen werden."""

from __future__ import annotations

import logging

from missa.core.config import Einstellungen
from missa.tools import werkzeuge_laden

NEXTCLOUD = {
    "nextcloud_url": "https://cloud.example.ch",
    "nextcloud_user": "louis",
    "nextcloud_app_password": "pw",
}


def namen(einstellungen: Einstellungen) -> list[str]:
    return [w.name for w in werkzeuge_laden(einstellungen)]


def test_ohne_nextcloud_keine_nextcloud_werkzeuge():
    assert not any(n.startswith("termin") for n in namen(Einstellungen()))


def test_kalender_werkzeuge_mit_vollstaendiger_konfiguration():
    geladen = namen(Einstellungen(**NEXTCLOUD, nextcloud_kalender="Persönlich"))
    assert {"termine_anzeigen", "termin_eintragen", "termin_loeschen"} <= set(geladen)


def test_ohne_kalendername_keine_kalender_werkzeuge(caplog):
    with caplog.at_level(logging.WARNING):
        geladen = namen(Einstellungen(**NEXTCLOUD))
    assert "termine_anzeigen" not in geladen
    assert "NEXTCLOUD_KALENDER" in caplog.text


def test_unvollstaendige_konfiguration_warnt(caplog):
    with caplog.at_level(logging.WARNING):
        geladen = namen(Einstellungen(nextcloud_url="https://x", nextcloud_kalender="K"))
    assert "termine_anzeigen" not in geladen
    assert "unvollständig" in caplog.text


def test_nextcloud_variablen_aus_umgebung():
    e = Einstellungen.aus_umgebung(
        {
            "NEXTCLOUD_URL": "https://cloud.example.ch/",
            "NEXTCLOUD_USER": "louis",
            "NEXTCLOUD_APP_PASSWORD": "abc-def",
            "NEXTCLOUD_KALENDER": "Persönlich",
        }
    )
    assert e.nextcloud_url == "https://cloud.example.ch"
    assert e.nextcloud_konfiguriert
    assert e.nextcloud_kalender == "Persönlich"


def test_aufgaben_werkzeuge_mit_liste():
    geladen = namen(
        Einstellungen(**NEXTCLOUD, nextcloud_kalender="K", nextcloud_aufgaben_liste="Aufgaben")
    )
    assert {
        "aufgaben_anzeigen",
        "aufgabe_erstellen",
        "aufgabe_erledigt",
        "aufgabe_loeschen",
    } <= set(geladen)


def test_ohne_listenname_keine_aufgaben_werkzeuge(caplog):
    with caplog.at_level(logging.WARNING):
        geladen = namen(Einstellungen(**NEXTCLOUD, nextcloud_kalender="K"))
    assert "aufgaben_anzeigen" not in geladen
    assert "NEXTCLOUD_AUFGABEN_LISTE" in caplog.text


def test_aufgaben_liste_aus_umgebung():
    assert (
        Einstellungen.aus_umgebung(
            {"NEXTCLOUD_AUFGABEN_LISTE": " Aufgaben "}
        ).nextcloud_aufgaben_liste
        == "Aufgaben"
    )


def test_gedaechtnis_werkzeuge_wenn_uebergeben(tmp_path):
    from missa.tools.gedaechtnis import Gedaechtnis

    e = Einstellungen(daten=tmp_path)
    assert "merken" not in namen(e)
    geladen = [w.name for w in werkzeuge_laden(e, Gedaechtnis(e.datenbank, e.zone))]
    assert {"merken", "erinnern", "vergessen"} <= set(geladen)


def test_werkzeuge_eines_geraets_kommen_nie_aus_werkzeuge_laden():
    """Rechner-Werkzeuge meldet nur ein angeschlossenes Gerät an, nie die eigene Ladeliste."""
    geladen = namen(Einstellungen(**NEXTCLOUD, nextcloud_kalender="K", geraete_aktiv=True))
    assert not [n for n in geladen if n.startswith("rechner_")]


def test_geraete_gruppe_erst_nach_anmeldung(tmp_path):
    import asyncio

    from missa.core.geraete import MINDESTSTUFEN, Geraete, GeraeteTokens, GeraeteWerkzeuge
    from missa.core.werkzeuge import Werkzeugkasten

    async def ablauf():
        kasten = Werkzeugkasten(werkzeuge_laden(Einstellungen()))
        vorher = len(kasten)
        tokens = GeraeteTokens(tmp_path / "missa.db")
        tokens.anlegen("laptop")
        geraete = Geraete(tokens, Einstellungen().zone)
        GeraeteWerkzeuge(geraete, kasten)
        assert len(kasten) == vorher
        geraete.anmelden(
            "laptop",
            protokoll=1,
            geraet_name="laptop",
            werkzeuge=[
                {"name": n, "beschreibung": n, "parameter": {"type": "object"}, "stufe": s}
                for n, s in MINDESTSTUFEN.items()
            ],
        )
        assert len(kasten) == vorher + len(MINDESTSTUFEN)
        assert set(kasten.gruppe("geraet:laptop")) == set(MINDESTSTUFEN)
        await geraete.schliessen()

    asyncio.run(ablauf())
