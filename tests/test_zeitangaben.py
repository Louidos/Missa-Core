"""Zeitangaben aus natürlicher Sprache: Uhrzeit, relativ, Tag, wiederkehrend, Zeitzone."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from missa.core.zeitangaben import Rueckfrage, Unverstanden, Wiederholung, wandzeit, zeit_verstehen

ZONE = ZoneInfo("Europe/Zurich")
#: Dienstag, 10. November 2026, 19:00 Uhr
ABEND = datetime(2026, 11, 10, 19, 0, tzinfo=ZONE)
#: Dienstag, 10. November 2026, 07:00 Uhr
FRUEH = datetime(2026, 11, 10, 7, 0, tzinfo=ZONE)


def lokal(jahr, monat, tag, stunde, minute=0) -> datetime:
    return datetime(jahr, monat, tag, stunde, minute, tzinfo=ZONE)


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("um 6:30 Uhr", lokal(2026, 11, 11, 6, 30)),
        ("um 6.30 Uhr", lokal(2026, 11, 11, 6, 30)),
        ("um 21 Uhr", lokal(2026, 11, 10, 21)),
        ("um 20:15", lokal(2026, 11, 10, 20, 15)),
        ("morgen um 7", lokal(2026, 11, 11, 7)),
        ("morgen früh", lokal(2026, 11, 11, 7)),
        ("übermorgen um 9:45", lokal(2026, 11, 12, 9, 45)),
        ("am Freitag um 14 Uhr", lokal(2026, 11, 13, 14)),
        ("nächsten Dienstag um 9", lokal(2026, 11, 17, 9)),
        ("am 24.12. um 18 Uhr", lokal(2026, 12, 24, 18)),
        ("am 3. Dezember um 10", lokal(2026, 12, 3, 10)),
        ("am 1.1. um 0:05", lokal(2027, 1, 1, 0, 5)),
        ("in 3 Tagen um 9", lokal(2026, 11, 13, 9)),
        ("heute Abend um 8", lokal(2026, 11, 10, 20)),
        ("viertel nach 9 heute abend", lokal(2026, 11, 10, 21, 15)),
        ("2026-11-13T14:00", lokal(2026, 11, 13, 14)),
    ],
)
def test_feste_zeitpunkte(text, erwartet):
    angabe = zeit_verstehen(text, ABEND)
    assert angabe.zeitpunkt == erwartet
    assert angabe.wiederholung is None


@pytest.mark.parametrize(
    ("text", "abstand"),
    [
        ("in 20 Minuten", timedelta(minutes=20)),
        ("in einer halben Stunde", timedelta(minutes=30)),
        ("in einer Viertelstunde", timedelta(minutes=15)),
        ("in 2 Stunden und 15 Minuten", timedelta(hours=2, minutes=15)),
        ("in einer Stunde", timedelta(hours=1)),
        ("in 1,5 Stunden", timedelta(minutes=90)),
        ("in 90 Sekunden", timedelta(seconds=90)),
    ],
)
def test_relative_angaben(text, abstand):
    assert zeit_verstehen(text, ABEND).zeitpunkt == ABEND + abstand


def test_halb_heisst_eine_stunde_vorher():
    assert zeit_verstehen("morgen um halb 8", ABEND).zeitpunkt == lokal(2026, 11, 11, 7, 30)


def test_uhrzeit_ohne_tag_spaeter_heute():
    assert zeit_verstehen("um 6:30", lokal(2026, 11, 10, 5, 0)).zeitpunkt == lokal(
        2026, 11, 10, 6, 30
    )


def test_vorbei_und_abends_auch_vorbei_heisst_morgen():
    # 19:00: 6:30 und 18:30 sind beide vorbei → morgen 6:30
    assert zeit_verstehen("um 6:30", ABEND).zeitpunkt == lokal(2026, 11, 11, 6, 30)


def test_um_6_wenn_es_schon_7_ist_fragt_nach():
    with pytest.raises(Rueckfrage) as frage:
        zeit_verstehen("um 6", FRUEH)
    assert "18:00" in frage.value.frage
    assert "morgen um 06:00" in frage.value.frage
    assert frage.value.optionen == [lokal(2026, 11, 10, 18), lokal(2026, 11, 11, 6)]


def test_null_vorne_ist_eindeutig():
    assert zeit_verstehen("um 06:00", FRUEH).zeitpunkt == lokal(2026, 11, 11, 6)


def test_nachts_mehrdeutig_bei_frueher_stunde():
    with pytest.raises(Rueckfrage, match="15:00"):
        zeit_verstehen("morgen um 3", ABEND)


def test_tag_ohne_uhrzeit_fragt_nach_der_uhrzeit():
    with pytest.raises(Rueckfrage, match="Um wie viel Uhr"):
        zeit_verstehen("morgen", ABEND)


def test_heute_vorbei_ist_ein_fehler():
    with pytest.raises(Unverstanden, match="vorbei"):
        zeit_verstehen("heute um 7", ABEND)


def test_gleicher_wochentag_vorbei_fragt():
    with pytest.raises(Rueckfrage, match="nächsten Dienstag"):
        zeit_verstehen("am Dienstag um 8", ABEND)


def test_unverstaendlich():
    with pytest.raises(Unverstanden):
        zeit_verstehen("irgendwann mal", ABEND)


def test_iso_in_der_vergangenheit():
    with pytest.raises(Unverstanden, match="Vergangenheit"):
        zeit_verstehen("2026-11-01T10:00", ABEND)


# ---------------------------------------------------------------------- wiederkehrend


@pytest.mark.parametrize(
    ("text", "regel", "erster"),
    [
        ("jeden Montag um 7", "woechentlich:0", lokal(2026, 11, 16, 7)),
        ("täglich um 6:30", "taeglich", lokal(2026, 11, 11, 6, 30)),
        ("jeden Tag um 21 Uhr", "taeglich", lokal(2026, 11, 10, 21)),
        ("werktags um 7", "werktags", lokal(2026, 11, 11, 7)),
        ("jeden Montag und Donnerstag um 18 Uhr", "woechentlich:0,3", lokal(2026, 11, 12, 18)),
        ("dienstags um 7", "woechentlich:1", lokal(2026, 11, 17, 7)),
        ("jeden Monat am 15. um 9", "monatlich:15", lokal(2026, 11, 15, 9)),
        ("jeden Morgen", "taeglich", lokal(2026, 11, 11, 7)),
        ("jedes Wochenende um 10", "woechentlich:5,6", lokal(2026, 11, 14, 10)),
    ],
)
def test_wiederkehrend(text, regel, erster):
    angabe = zeit_verstehen(text, ABEND)
    assert angabe.wiederholung is not None
    assert angabe.wiederholung.speichern() == regel
    assert angabe.zeitpunkt == erster


def test_wiederholung_speichern_und_lesen():
    for regel in ("taeglich", "werktags", "woechentlich:0,3", "monatlich:31"):
        assert Wiederholung.lesen(regel).speichern() == regel
    assert Wiederholung.lesen(None) is None


def test_monatlich_am_31_im_kurzen_monat():
    regel = Wiederholung("monatlich", monatstag=31)
    naechster = regel.naechster(lokal(2026, 11, 1, 0), time(9), ZONE)
    assert naechster == lokal(2026, 11, 30, 9)


def test_werktags_ueberspringt_wochenende():
    regel = Wiederholung("werktags")
    freitag = lokal(2026, 11, 13, 7)
    assert regel.naechster(freitag, time(7), ZONE) == lokal(2026, 11, 16, 7)


# ---------------------------------------------------------------------- Sommer-/Winterzeit


def test_wiederkehrend_ueber_die_winterzeit_umstellung():
    """25.10.2026: Sommerzeit endet. 7:00 bleibt 7:00 an der Wand, UTC verschiebt sich."""
    regel = Wiederholung("taeglich")
    samstag = lokal(2026, 10, 24, 7)
    sonntag = regel.naechster(samstag, time(7), ZONE)
    montag = regel.naechster(sonntag, time(7), ZONE)
    assert (samstag.hour, sonntag.hour, montag.hour) == (7, 7, 7)
    assert samstag.astimezone(UTC).hour == 5  # CEST = UTC+2
    assert sonntag.astimezone(UTC).hour == 6  # CET = UTC+1
    assert sonntag.astimezone(UTC) - samstag.astimezone(UTC) == timedelta(hours=25)


def test_wiederkehrend_ueber_die_sommerzeit_umstellung():
    """28.03.2027: Sommerzeit beginnt."""
    regel = Wiederholung("woechentlich", (6,))  # sonntags
    vorher = lokal(2027, 3, 21, 7)
    nachher = regel.naechster(vorher, time(7), ZONE)
    assert nachher.date() == date(2027, 3, 28)
    assert nachher.hour == 7
    assert nachher.astimezone(UTC).hour == 5
    assert nachher.astimezone(UTC) - vorher.astimezone(UTC) == timedelta(days=7, hours=-1)


def test_uhrzeit_die_es_nicht_gibt_wird_geschoben():
    wert = wandzeit(date(2027, 3, 28), time(2, 30), ZONE)
    assert (wert.hour, wert.minute) == (3, 30)
    assert wert.utcoffset() == timedelta(hours=2)


def test_doppelte_uhrzeit_gilt_in_der_sommerzeit():
    wert = wandzeit(date(2026, 10, 25), time(2, 30), ZONE)
    assert wert.utcoffset() == timedelta(hours=2)
