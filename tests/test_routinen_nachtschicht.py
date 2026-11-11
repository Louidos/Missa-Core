"""Routinen, Nachtschicht und Hintergrund-Aufträge.

Alles läuft mit einer gestellten Uhr, gefälschtem Kanal und gefälschtem Modell –
ein echter Server wird nicht gebraucht.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.hintergrund import Hintergrund, im_fenster, naechster_beginn
from missa.core.vorgaben import Vorgaben, uhrzeit_lesen
from missa.core.werkzeuge import WerkzeugFehler, aktuelle_anfrage, im_hintergrund
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.erinnerungen import ErinnerungsWerkzeuge
from missa.tools.nachtschicht import NachtschichtWerkzeuge, nachtschicht_werkzeuge
from missa.tools.routinen import RoutinenWerkzeuge, routinen_werkzeuge
from tests.conftest import API_KEY, text, werkzeug_aufruf
from tests.test_zeitplaner import FakeKanal, Uhr

ZONE = ZoneInfo("Europe/Zurich")


class FakeMissa:
    """Ersetzt das Modell: merkt sich die Aufträge und antwortet mit festem Text."""

    def __init__(self) -> None:
        self.auftraege: list[str] = []
        self.im_hintergrund: list[bool] = []
        self.fehler: Exception | None = None

    async def __call__(self, auftrag) -> str:
        self.auftraege.append(auftrag.auftrag_fuer_missa())
        self.im_hintergrund.append(im_hintergrund.get())
        if self.fehler is not None:
            raise self.fehler
        return f"Ergebnis für {auftrag.titel}"


@pytest.fixture
def uhr() -> Uhr:
    # Dienstag, 10. November 2026, 19:00
    return Uhr(datetime(2026, 11, 10, 19, 0, tzinfo=ZONE))


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def datei(tmp_path: Path) -> Path:
    return tmp_path / "missa.db"


@pytest.fixture
def planer(datei, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def missa() -> FakeMissa:
    return FakeMissa()


@pytest.fixture
def hg(datei, planer, missa) -> Hintergrund:
    h = Hintergrund(datei, ZONE, planer)
    h.ausfuehrer = missa
    return h


@pytest.fixture
def bestaetigungen() -> Bestaetigungen:
    return Bestaetigungen()


@pytest.fixture
def rw(planer, hg, bestaetigungen) -> RoutinenWerkzeuge:
    return RoutinenWerkzeuge(planer, hg, bestaetigungen)


@pytest.fixture
def nw(hg, bestaetigungen) -> NachtschichtWerkzeuge:
    return NachtschichtWerkzeuge(hg, bestaetigungen)


def zustellen(planer: Zeitplaner) -> int:
    return asyncio.run(planer.faellige_zustellen())


def abarbeiten(hg: Hintergrund) -> int:
    """Führt alle Aufträge aus, die jetzt laufen dürfen."""

    async def ablauf() -> int:
        anzahl = 0
        while (auftrag := hg.naechster()) is not None:
            await hg.ausfuehren(auftrag)
            anzahl += 1
        return anzahl

    return asyncio.run(ablauf())


def tagesauftraege(planer: Zeitplaner) -> list[str]:
    return asyncio.run(planer.auftraege_ausfuehren(warten=True))


def in_anfrage(nummer: int, funktion, *args, **kwargs):
    marke = aktuelle_anfrage.set(f"anfrage-{nummer}")
    try:
        return funktion(*args, **kwargs)
    finally:
        aktuelle_anfrage.reset(marke)


# ---------------------------------------------------------------------- Erinnerungen


def test_rail_kit_erinnerung_kommt_auch_nach_einem_neustart(datei, kanal, uhr):
    erster = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    ergebnis = ErinnerungsWerkzeuge(erster).anlegen("ans Rail-Kit", "um 6:30")
    assert "Mittwoch, 11. November 2026 um 06:30 Uhr" in ergebnis

    # Neustart (z. B. Update) mitten in der Nacht.
    uhr.jetzt = datetime(2026, 11, 11, 3, 0, tzinfo=ZONE)
    zweiter = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    assert zustellen(zweiter) == 0
    uhr.jetzt = datetime(2026, 11, 11, 6, 30, tzinfo=ZONE)
    assert zustellen(zweiter) == 1
    assert kanal.nachrichten == ["⏰ Erinnerung: ans Rail-Kit"]


def test_migration_ergaenzt_spalten_und_behaelt_alte_erinnerungen(datei, kanal, uhr):
    alt = sqlite3.connect(datei)
    alt.executescript(
        """
        CREATE TABLE erinnerungen (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, nachricht TEXT,
            faellig TEXT NOT NULL, uhrzeit TEXT NOT NULL, regel TEXT,
            status TEXT NOT NULL DEFAULT 'offen'
                CHECK (status IN ('offen', 'zugestellt', 'geloescht')),
            erstellt TEXT NOT NULL, zugestellt TEXT, naechster_versuch TEXT,
            versuche INTEGER NOT NULL DEFAULT 0, schluessel TEXT UNIQUE, kanal TEXT,
            herkunft TEXT NOT NULL DEFAULT 'chat'
        );
        INSERT INTO erinnerungen (text, faellig, uhrzeit, erstellt)
        VALUES ('Training', '2026-11-11T05:30:00+00:00', '06:30', '2026-11-10T19:00:00+01:00');
        """
    )
    alt.commit()
    alt.close()
    planer = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    [erinnerung] = planer.offene()
    assert erinnerung.text == "Training"
    assert erinnerung.art == "erinnerung" and not erinnerung.pausiert
    # Zweiter Start: Migration läuft nicht doppelt.
    Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)


# ---------------------------------------------------------------------- Routinen


def test_routine_anlegen_per_text(rw, planer):
    ergebnis = rw.anlegen(
        "Schreib mir einen Wochenrückblick", "jeden Freitag um 17 Uhr", "Wochenrückblick"
    )
    assert "Routine gespeichert #1" in ergebnis
    assert "jeden Freitag um 17:00 Uhr" in ergebnis
    assert "Freitag, 13. November 2026 um 17:00 Uhr" in ergebnis
    [routine] = planer.offene("routine")
    assert routine.anweisung == "Schreib mir einen Wochenrückblick"
    # Routinen sind keine Erinnerungen.
    assert planer.offene() == []
    assert "Wochenrückblick" in rw.anzeigen()


def test_routine_braucht_eine_wiederholung(rw, planer):
    ergebnis = rw.anlegen("Wochenrückblick", "am Freitag um 17 Uhr")
    assert ergebnis.startswith("NOCH NICHT GESPEICHERT")
    assert planer.offene("routine") == []


def test_routine_laeuft_zur_zeit_und_ergebnis_kommt_als_nachricht(
    rw, planer, hg, missa, kanal, uhr
):
    rw.anlegen("Schreib mir einen Wochenrückblick", "jeden Freitag um 17 Uhr", "Wochenrückblick")
    uhr.jetzt = datetime(2026, 11, 13, 16, 59, tzinfo=ZONE)
    assert zustellen(planer) == 0
    uhr.vor(minutes=1)
    assert zustellen(planer) == 1  # an den Hintergrund übergeben
    assert kanal.nachrichten == []  # das Modell arbeitet noch

    assert abarbeiten(hg) == 1
    assert missa.im_hintergrund == [True]
    assert "Routine „Wochenrückblick“ ist fällig" in missa.auftraege[0]
    assert "Anweisung: Schreib mir einen Wochenrückblick" in missa.auftraege[0]
    zustellen(planer)
    assert kanal.nachrichten == ["🔁 Wochenrückblick\n\nErgebnis für Wochenrückblick"]
    # Nächster Lauf eine Woche später.
    [routine] = planer.offene("routine")
    assert routine.faellig == datetime(2026, 11, 20, 17, 0, tzinfo=ZONE)


def test_routine_pausieren_und_fortsetzen(rw, planer, hg, missa, uhr):
    rw.anlegen("Wetter zusammenfassen", "täglich um 7", "Wetter")
    assert "Routine pausiert" in rw.pausieren(1)
    assert "PAUSIERT" in rw.anzeigen()

    uhr.jetzt = datetime(2026, 11, 13, 7, 0, tzinfo=ZONE)  # zwei Läufe verpasst
    assert zustellen(planer) == 0
    assert abarbeiten(hg) == 0

    ergebnis = rw.fortsetzen("#1")
    assert "Routine fortgesetzt" in ergebnis
    # Kein Nachholen der pausierten Läufe: weiter mit dem nächsten regulären.
    assert "Samstag, 14. November 2026 um 07:00 Uhr" in ergebnis
    assert zustellen(planer) == 0


def test_routine_loeschen_nur_nach_ja(rw, planer):
    rw.anlegen("Wochenrückblick", "jeden Freitag um 17 Uhr")
    vorschau = in_anfrage(1, rw.loeschen, 1)
    assert vorschau.startswith("NOCH NICHT GELÖSCHT")
    assert in_anfrage(1, rw.loeschen, 1, bestaetigt=True).startswith("NICHT GELÖSCHT")
    assert len(planer.offene("routine")) == 1
    assert "Routine gelöscht" in in_anfrage(2, rw.loeschen, 1, bestaetigt=True)
    assert planer.offene("routine") == []
    with pytest.raises(WerkzeugFehler):
        rw.pausieren(1)


def test_erinnerungs_werkzeuge_fassen_routinen_nicht_an(rw, planer):
    rw.anlegen("Wochenrückblick", "jeden Freitag um 17 Uhr")
    with pytest.raises(WerkzeugFehler, match="Erinnerung #1"):
        ErinnerungsWerkzeuge(planer).loeschen(1)
    with pytest.raises(WerkzeugFehler, match="Routine #2"):
        planer.anlegen("Training", planer.jetzt() + timedelta(hours=1))
        rw.pausieren(2)


def test_verpasste_routine_laeuft_nach_dem_neustart_einmal(datei, kanal, uhr, missa):
    planer = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    hg = Hintergrund(datei, ZONE, planer)
    hg.ausfuehrer = missa
    RoutinenWerkzeuge(planer, hg).anlegen("Tagesplan", "täglich um 7")
    # ai01 war drei Tage aus.
    uhr.jetzt = datetime(2026, 11, 14, 9, 0, tzinfo=ZONE)
    neu = Zeitplaner(datei, ZONE, Zustellung([kanal]), uhr=uhr)
    hg2 = Hintergrund(datei, ZONE, neu)
    hg2.ausfuehrer = missa
    assert zustellen(neu) == 1
    assert abarbeiten(hg2) == 1
    assert "verspätet" in missa.auftraege[0]
    [routine] = neu.offene("routine")
    assert routine.faellig == datetime(2026, 11, 15, 7, 0, tzinfo=ZONE)


def test_routine_ueber_die_zeitumstellung(rw, planer, hg, uhr):
    uhr.jetzt = datetime(2026, 10, 23, 12, 0, tzinfo=ZONE)  # Sommerzeit
    rw.anlegen("Wochenrückblick", "jeden Freitag um 17 Uhr")
    uhr.jetzt = datetime(2026, 10, 23, 17, 0, tzinfo=ZONE)
    zustellen(planer)
    [routine] = planer.offene("routine")
    # 25. Oktober: Umstellung auf Winterzeit – trotzdem 17:00 an der Wand.
    assert routine.faellig == datetime(2026, 10, 30, 17, 0, tzinfo=ZONE)
    assert routine.faellig.utcoffset() == timedelta(hours=1)


def test_routine_jetzt_ausfuehren(rw, planer, hg, kanal):
    rw.anlegen("Wochenrückblick", "jeden Freitag um 17 Uhr", "Wochenrückblick")
    assert "läuft jetzt im Hintergrund" in rw.jetzt_ausfuehren(1)
    abarbeiten(hg)
    zustellen(planer)
    assert kanal.nachrichten == ["🔁 Wochenrückblick\n\nErgebnis für Wochenrückblick"]
    # Der reguläre Zeitplan bleibt.
    assert planer.offene("routine")[0].faellig == datetime(2026, 11, 13, 17, 0, tzinfo=ZONE)


def test_kein_zweiter_lauf_wenn_der_erste_noch_wartet(rw, planer, hg, uhr):
    rw.anlegen("Tagesplan", "täglich um 19:01")
    uhr.vor(minutes=1)
    zustellen(planer)
    uhr.vor(days=1)
    zustellen(planer)
    assert hg.anzahl_wartend() == 1


def test_fehlgeschlagene_routine_wird_wiederholt_und_dann_gemeldet(
    rw, planer, hg, missa, kanal, uhr
):
    missa.fehler = RuntimeError("llama-server nicht erreichbar")
    rw.anlegen("Tagesplan", "täglich um 19:01", "Tagesplan")
    uhr.vor(minutes=1)
    zustellen(planer)
    assert abarbeiten(hg) == 1
    [auftrag] = hg.liste("sofort")
    assert auftrag.status == "wartet" and auftrag.versuche == 1
    assert abarbeiten(hg) == 0  # wartet erst 5 Minuten
    uhr.vor(minutes=5)
    abarbeiten(hg)
    uhr.vor(minutes=10)
    abarbeiten(hg)
    [auftrag] = hg.liste("sofort")
    assert auftrag.status == "fehler"
    zustellen(planer)
    assert "konnte ich leider nicht ausführen" in kanal.nachrichten[-1]
    assert "llama-server nicht erreichbar" in kanal.nachrichten[-1]


# ---------------------------------------------------------------------- Nachtschicht


def test_nachtaufgabe_laeuft_nur_im_fenster(nw, hg, missa, uhr):
    ergebnis = nw.anlegen("Fasse alle offenen Aufgaben zusammen", "Aufgaben-Übersicht")
    assert "Nachtaufgabe #1" in ergebnis
    assert "01:00–06:00 Uhr" in ergebnis
    assert "Mittwoch, 11. November 2026 um 01:00 Uhr" in ergebnis
    assert abarbeiten(hg) == 0

    uhr.jetzt = datetime(2026, 11, 11, 0, 59, tzinfo=ZONE)
    assert abarbeiten(hg) == 0
    uhr.jetzt = datetime(2026, 11, 11, 1, 0, tzinfo=ZONE)
    assert abarbeiten(hg) == 1
    assert "Nachtschicht" in missa.auftraege[0]
    assert "Aufgabe: Fasse alle offenen Aufgaben zusammen" in missa.auftraege[0]
    assert "#1 „Aufgaben-Übersicht“ – erledigt" in nw.anzeigen()


def test_ergebnis_am_morgen_und_im_naechsten_gespraech(nw, hg, planer, kanal, uhr):
    nw.anlegen("Fasse alle offenen Aufgaben zusammen", "Aufgaben-Übersicht")
    uhr.jetzt = datetime(2026, 11, 11, 2, 0, tzinfo=ZONE)
    abarbeiten(hg)
    zustellen(planer)
    assert kanal.nachrichten == []  # erst am Morgen

    uhr.jetzt = datetime(2026, 11, 11, 6, 0, tzinfo=ZONE)
    assert "nachtschicht-bericht" in tagesauftraege(planer)
    zustellen(planer)
    [bericht] = kanal.nachrichten
    assert bericht.startswith("🌙 Guten Morgen!")
    assert "#1 „Aufgaben-Übersicht“" in bericht
    assert "Ergebnis für Aufgaben-Übersicht" in bericht

    # Im nächsten Gespräch einmal erwähnt, danach nicht mehr.
    uhr.jetzt = datetime(2026, 11, 11, 8, 15, tzinfo=ZONE)
    kontext = hg.kontext("Guten Morgen")
    assert kontext is not None and "Aus deiner Nachtschicht" in kontext
    assert "Ergebnis für Aufgaben-Übersicht" in kontext
    assert hg.kontext("Und sonst?") is None
    # Das ganze Ergebnis bleibt abrufbar.
    assert "Ergebnis für Aufgaben-Übersicht" in nw.ergebnis(1)


def test_morgenbericht_ohne_aufgaben_schickt_nichts(hg, planer, kanal, uhr):
    uhr.jetzt = datetime(2026, 11, 11, 6, 0, tzinfo=ZONE)
    tagesauftraege(planer)
    zustellen(planer)
    assert kanal.nachrichten == []


def test_nach_dem_fenster_fertig_wird_sofort_gemeldet(nw, hg, planer, kanal, uhr):
    nw.anlegen("Lange Aufgabe")
    uhr.jetzt = datetime(2026, 11, 11, 5, 59, tzinfo=ZONE)
    auftrag = hg.naechster()
    assert auftrag is not None
    uhr.jetzt = datetime(2026, 11, 11, 6, 20, tzinfo=ZONE)  # Modell war langsam
    asyncio.run(hg.ausfuehren(auftrag))
    zustellen(planer)
    assert len(kanal.nachrichten) == 1 and "Lange Aufgabe" in kanal.nachrichten[0]


def test_kontext_im_hintergrund_markiert_nichts(nw, hg, uhr):
    nw.anlegen("Lange Aufgabe")
    uhr.jetzt = datetime(2026, 11, 11, 2, 0, tzinfo=ZONE)
    abarbeiten(hg)
    marke = im_hintergrund.set(True)
    assert hg.kontext("x") is None
    im_hintergrund.reset(marke)
    assert hg.kontext("x") is not None


def test_nachtschicht_per_gespraech_einstellen_bleibt_gespeichert(
    nw, hg, datei, planer, missa, uhr
):
    ergebnis = nw.einstellen("2", "5:30")
    assert "02:00–05:30 Uhr" in ergebnis
    # Nach einem Neustart gilt das neue Fenster weiter.
    neu = Hintergrund(datei, ZONE, planer)
    assert neu.fenster() == (time(2, 0), time(5, 30))
    assert planer._auftraege["nachtschicht-bericht"].uhrzeit == time(5, 30)
    neu.ausfuehrer = missa
    neu.einreihen("", "Lange Aufgabe")
    uhr.jetzt = datetime(2026, 11, 11, 1, 30, tzinfo=ZONE)
    assert abarbeiten(neu) == 0
    uhr.jetzt = datetime(2026, 11, 11, 2, 0, tzinfo=ZONE)
    assert abarbeiten(neu) == 1


def test_nachtschicht_einstellen_prueft_die_angaben(nw):
    with pytest.raises(WerkzeugFehler):
        nw.einstellen("2", "2:00")
    with pytest.raises(WerkzeugFehler):
        nw.einstellen("abends", "6")


def test_nachtaufgabe_loeschen_nur_nach_ja(nw, hg):
    nw.anlegen("Lange Aufgabe")
    assert in_anfrage(1, nw.loeschen, 1).startswith("NOCH NICHT GELÖSCHT")
    assert hg.anzahl_wartend() == 1
    assert "gelöscht" in in_anfrage(2, nw.loeschen, 1, bestaetigt=True)
    assert hg.anzahl_wartend() == 0


def test_unterbrochene_nachtaufgabe_laeuft_nach_dem_neustart_erneut(datei, planer, missa, uhr):
    hg = Hintergrund(datei, ZONE, planer)
    auftrag = hg.einreihen("", "Lange Aufgabe")
    hg._setzen(auftrag.id, status="laeuft", versuche=1)  # Strom weg mittendrin

    async def neustart() -> None:
        neu = Hintergrund(datei, ZONE, planer)
        neu.ausfuehrer = missa
        uhr.jetzt = datetime(2026, 11, 11, 1, 0, tzinfo=ZONE)
        neu.starten()
        for _ in range(100):
            if neu.holen(auftrag.id).status == "fertig":  # type: ignore[union-attr]
                break
            await asyncio.sleep(0.01)
        await neu.stoppen()

    asyncio.run(neustart())
    assert hg.holen(auftrag.id).status == "fertig"  # type: ignore[union-attr]


def test_hintergrund_blockiert_den_event_loop_nicht(datei, planer, uhr):
    """Während Missa im Hintergrund rechnet, bleibt der Event-Loop (also der Chat) frei."""
    hg = Hintergrund(datei, ZONE, planer)
    gestartet = asyncio.Event()
    freigabe = asyncio.Event()

    async def langsam(_auftrag) -> str:
        gestartet.set()
        await freigabe.wait()
        return "fertig"

    hg.ausfuehrer = langsam

    async def ablauf() -> str:
        hg.starten()
        hg.einreihen("Routine", "x", art="sofort")
        await asyncio.wait_for(gestartet.wait(), 2)
        # Der Loop reagiert, obwohl der Auftrag läuft – z. B. auf eine Chat-Anfrage.
        await asyncio.sleep(0)
        status = hg.liste("sofort")[0].status
        freigabe.set()
        for _ in range(100):
            if hg.liste("sofort")[0].status == "fertig":
                break
            await asyncio.sleep(0.01)
        await hg.stoppen()
        return status

    assert asyncio.run(ablauf()) == "laeuft"
    assert hg.liste("sofort")[0].status == "fertig"


# ---------------------------------------------------------------------- Zeitfenster


@pytest.mark.parametrize(
    ("jetzt", "erwartet"),
    [
        (datetime(2026, 11, 11, 0, 59, tzinfo=ZONE), False),
        (datetime(2026, 11, 11, 1, 0, tzinfo=ZONE), True),
        (datetime(2026, 11, 11, 5, 59, tzinfo=ZONE), True),
        (datetime(2026, 11, 11, 6, 0, tzinfo=ZONE), False),
        # Winterzeit-Umstellung: 02:30 gibt es zweimal, beide Male im Fenster.
        (datetime(2026, 10, 25, 2, 30, fold=0, tzinfo=ZONE), True),
        (datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=ZONE), True),
        # Sommerzeit-Umstellung: nach 01:59 kommt 03:00.
        (datetime(2027, 3, 28, 1, 59, tzinfo=ZONE), True),
        (datetime(2027, 3, 28, 3, 0, tzinfo=ZONE), True),
    ],
)
def test_im_fenster(jetzt, erwartet):
    assert im_fenster(jetzt, time(1, 0), time(6, 0)) is erwartet


def test_fenster_ueber_mitternacht():
    von, bis = time(23, 0), time(5, 0)
    assert im_fenster(datetime(2026, 11, 11, 23, 30, tzinfo=ZONE), von, bis)
    assert im_fenster(datetime(2026, 11, 12, 4, 0, tzinfo=ZONE), von, bis)
    assert not im_fenster(datetime(2026, 11, 12, 12, 0, tzinfo=ZONE), von, bis)


def test_naechster_beginn_ueber_die_sommerzeit_umstellung():
    # 28. März 2027: 02:30 gibt es nicht – das Fenster beginnt um 03:30 Sommerzeit.
    jetzt = datetime(2027, 3, 27, 20, 0, tzinfo=ZONE)
    beginn = naechster_beginn(jetzt, time(2, 30), time(5, 0))
    assert (beginn.hour, beginn.minute) == (3, 30)
    assert beginn.utcoffset() == timedelta(hours=2)


def test_naechster_beginn_ueber_die_winterzeit_umstellung():
    jetzt = datetime(2026, 10, 24, 20, 0, tzinfo=ZONE)
    beginn = naechster_beginn(jetzt, time(1, 0), time(6, 0))
    assert beginn == datetime(2026, 10, 25, 1, 0, tzinfo=ZONE)
    assert beginn.utcoffset() == timedelta(hours=2)
    # Dauer bis zum Ende des Fensters: eine Stunde länger als sonst.
    ende = datetime(2026, 10, 25, 6, 0, tzinfo=ZONE)
    assert ende.astimezone(UTC) - beginn.astimezone(UTC) == timedelta(hours=6)


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("2", time(2)), ("01:00", time(1)), ("5.30", time(5, 30)), ("um 6 Uhr", time(6))],
)
def test_uhrzeit_lesen(eingabe, erwartet):
    assert uhrzeit_lesen(eingabe) == erwartet


def test_vorgaben_ueberleben_neustart(datei):
    Vorgaben(datei).setzen("x", "1")
    assert Vorgaben(datei).lesen("x", "0") == "1"
    assert Vorgaben(datei).lesen("y", "0") == "0"


# ---------------------------------------------------------------------- Sicherheit


def test_im_hintergrund_gilt_keine_zustimmung(bestaetigungen):
    in_anfrage(1, bestaetigungen.vormerken, "fakt:1")
    marke = im_hintergrund.set(True)
    try:
        assert in_anfrage(2, bestaetigungen.erlaubt, "fakt:1") is False
    finally:
        im_hintergrund.reset(marke)
    assert in_anfrage(2, bestaetigungen.erlaubt, "fakt:1") is True


def test_im_hintergrund_keine_neuen_routinen_oder_nachtaufgaben(rw, nw):
    marke = im_hintergrund.set(True)
    try:
        with pytest.raises(WerkzeugFehler):
            rw.anlegen("x", "täglich um 7")
        with pytest.raises(WerkzeugFehler):
            nw.anlegen("x")
        with pytest.raises(WerkzeugFehler):
            nw.einstellen("2", "5")
    finally:
        im_hintergrund.reset(marke)


def test_werkzeug_namen(planer, hg):
    assert [w.name for w in routinen_werkzeuge(planer, hg)] == [
        "routine_anlegen",
        "routinen_anzeigen",
        "routine_pausieren",
        "routine_fortsetzen",
        "routine_loeschen",
        "routine_jetzt_ausfuehren",
    ]
    assert [w.name for w in nachtschicht_werkzeuge(hg)] == [
        "nachtaufgabe_anlegen",
        "nachtaufgaben_anzeigen",
        "nachtaufgabe_ergebnis",
        "nachtaufgabe_loeschen",
        "nachtschicht_einstellen",
    ]


# ---------------------------------------------------------------------- App


def test_app_routine_von_anfang_bis_ende(einstellungen, llama):
    kanal = FakeKanal()
    app = create_app(einstellungen, llama_transport=llama.transport, zustellung=Zustellung([kanal]))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        gesund = client.get("/health").json()
        for name in ("routine_anlegen", "nachtaufgabe_anlegen", "nachtschicht_einstellen"):
            assert name in gesund["werkzeuge"]

        # Louis legt die Routine im Chat an.
        llama.antworten = [
            werkzeug_aufruf(
                "routine_anlegen",
                {
                    "anweisung": "Wochenrückblick schreiben",
                    "wann": "jeden Freitag um 17 Uhr",
                    "name": "Wochenrückblick",
                },
            ),
            text("Mach ich, jeden Freitag um 17 Uhr! 📅"),
        ]
        client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Jeden Freitag um 17 Uhr Wochenrückblick"}]
            },
        )
        routinen = client.get("/api/routinen").json()
        assert routinen["anzahl"] == 1
        assert routinen["routinen"][0]["anweisung"] == "Wochenrückblick schreiben"
        assert client.get("/api/erinnerungen").json()["anzahl"] == 0

        # Zur Zeit: Missa erledigt den Auftrag im Hintergrund mit dem Modell.
        llama.antworten = [text("Deine Woche war super! 🎉")]
        hg: Hintergrund = app.state.hintergrund
        auftrag = hg.einreihen(
            "Wochenrückblick",
            "Wochenrückblick schreiben",
            art="sofort",
            herkunft="routine",
            zustellen=True,
        )
        hg.wecken()
        for _ in range(200):
            if hg.holen(auftrag.id).status == "fertig":  # type: ignore[union-attr]
                break
            import time as zeit

            zeit.sleep(0.01)
        assert hg.holen(auftrag.id).ergebnis == "Deine Woche war super! 🎉"  # type: ignore[union-attr]
        anfrage = llama.anfragen[-1]["messages"][-1]["content"]
        assert "Interner Auftrag" in anfrage and "Louis ist gerade nicht im Gespräch" in anfrage
        assert "tools" in llama.anfragen[-1]  # mit Werkzeugen

        nacht = client.get("/api/nachtschicht").json()
        assert nacht["fenster"] == {"von": "01:00", "bis": "06:00"}


def test_app_erwaehnt_nachtschicht_im_systemprompt(einstellungen, llama):
    app = create_app(einstellungen, llama_transport=llama.transport, zustellung=Zustellung())
    hg: Hintergrund = app.state.hintergrund
    auftrag = hg.einreihen("Aufgaben-Übersicht", "Fasse zusammen")
    hg._setzen(
        auftrag.id, status="fertig", ergebnis="3 Aufgaben offen.", beendet=hg.jetzt().isoformat()
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        client.post(
            "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Guten Morgen"}]}
        )
        assert "Aus deiner Nachtschicht" in llama.system(-1)
        assert "3 Aufgaben offen." in llama.system(-1)
        client.post(
            "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Danke"}]}
        )
        assert "Aus deiner Nachtschicht" not in llama.system(-1)


def test_api_braucht_anmeldung(client):
    client.headers.pop("Authorization")
    assert client.get("/api/routinen").status_code == 401
    assert client.get("/api/nachtschicht").status_code == 401
