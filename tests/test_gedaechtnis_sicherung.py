"""Gruppe 5: Langzeitgedächtnis, Lernen aus Korrekturen, Vergessen, Backup und Wiederherstellen."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.sicherung import (
    Quellen,
    Sicherung,
    env_ohne_geheimnisse,
    wiederherstellen,
)
from missa.core.sicherung import (
    main as sicherung_main,
)
from missa.core.verlauf import Verlauf
from missa.core.vorgaben import Vorgaben
from missa.core.werkzeuge import Werkzeugkasten, aktuelle_anfrage
from missa.core.zeitangaben import Wiederholung
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.gedaechtnis import Gedaechtnis, gedaechtnis_werkzeuge
from tests.conftest import API_KEY, text, werkzeug_aufruf

ZONE = ZoneInfo("Europe/Zurich")
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def datei(tmp_path) -> Path:
    return tmp_path / "daten" / "missa.db"


@pytest.fixture
def gedaechtnis(datei) -> Gedaechtnis:
    return Gedaechtnis(datei, ZONE)


@pytest.fixture(autouse=True)
def anfrage():
    marke = aktuelle_anfrage.set("anfrage-1")
    yield
    aktuelle_anfrage.reset(marke)


def nachricht_vor_tagen(datei: Path, tage: int, sitzung: str, inhalt: str) -> None:
    Verlauf(datei)
    zeit = (datetime.now(UTC) - timedelta(days=tage)).isoformat(timespec="seconds")
    with sqlite3.connect(datei) as db:
        db.execute(
            "INSERT INTO nachrichten (sitzung, rolle, inhalt, erstellt) VALUES (?, 'user', ?, ?)",
            (sitzung, inhalt, zeit),
        )


# ---------------------------------------------------------------------- Langzeitgedächtnis


def test_nach_neustart_weiss_missa_was_louis_vor_zwei_tagen_gesagt_hat(datei):
    nachricht_vor_tagen(
        datei, 2, "talk-louis", "Ich will den Proxmox-Server auf 64 GB RAM aufrüsten"
    )
    Gedaechtnis(datei, ZONE).hinzufuegen("Louis plant ein RAM-Upgrade für Proxmox", "projekte")

    neu = Gedaechtnis(datei, ZONE)  # Neustart: alles frisch aus der Datei
    kontext = neu.kontext("Was wollte ich nochmal mit dem Proxmox-Server machen?") or ""
    assert "Louis plant ein RAM-Upgrade für Proxmox" in kontext
    assert "## Frühere Gespräche" in kontext
    tag = (datetime.now(ZONE) - timedelta(days=2)).strftime("%d.%m.%Y")
    assert f"{tag} Louis: „Ich will den Proxmox-Server auf 64 GB RAM aufrüsten“" in kontext


def test_fakten_haben_ein_datum(gedaechtnis):
    fakt, _ = gedaechtnis.hinzufuegen("Louis hat sich für Debian entschieden", "entscheidungen")
    assert f"gemerkt am {datetime.now(ZONE):%Y-%m-%d}" in fakt.zeile()


def test_aehnlicher_fakt_ersetzt_statt_doppelt(gedaechtnis):
    gedaechtnis.hinzufuegen("Louis trinkt Kaffee mit Zucker", "vorlieben")
    neu, ist_neu = gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    assert not ist_neu and neu.geaendert
    assert [f.text for f in gedaechtnis.alle()] == ["Louis trinkt Kaffee ohne Zucker"]
    # Verschiedene Dinge bleiben getrennt.
    gedaechtnis.hinzufuegen("Projekt 1 ist fertig", "projekte")
    gedaechtnis.hinzufuegen("Projekt 7 ist fertig", "projekte")
    assert len(gedaechtnis.alle("projekte")) == 2


def test_was_weisst_du_ueber(gedaechtnis):
    werkzeuge = {w.name: w for w in gedaechtnis_werkzeuge(gedaechtnis, Bestaetigungen())}
    werkzeuge["merken"].funktion("Louis' Schwester heißt Anna", "sonstiges")
    assert "Anna" in werkzeuge["erinnern"].funktion("Schwester")


def test_hinweise_im_kontext(gedaechtnis):
    assert "## Vielleicht merkenswert" in (gedaechtnis.kontext("Ich mag keine Oliven") or "")
    assert "## Louis korrigiert dich" in (
        gedaechtnis.kontext("Nein, das war falsch, ich wohne in Bern") or ""
    )
    assert gedaechtnis.kontext("Wie spät ist es?") is None


def test_migration_alter_tabelle(datei):
    datei.parent.mkdir(parents=True)
    with sqlite3.connect(datei) as db:
        db.executescript(
            """
            CREATE TABLE fakten (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                kategorie TEXT NOT NULL CHECK (kategorie IN ('termine', 'vorlieben', 'projekte',
                'sonstiges')), erstellt TEXT NOT NULL);
            INSERT INTO fakten (text, kategorie, erstellt)
                VALUES ('Louis mag Tee', 'vorlieben', '2026-11-01T10:00:00+01:00');
            """
        )
    gedaechtnis = Gedaechtnis(datei, ZONE)
    assert [f.text for f in gedaechtnis.suchen("Tee")] == ["Louis mag Tee"]
    gedaechtnis.hinzufuegen("Louis hat entschieden: Debian", "entscheidungen")
    Gedaechtnis(datei, ZONE)  # zweiter Start: keine erneute Migration


# ---------------------------------------------------------------------- Korrekturen


def test_nach_korrektur_beim_naechsten_mal_richtig(gedaechtnis):
    alt, _ = gedaechtnis.hinzufuegen("Louis' Velo ist rot", "sonstiges")
    werkzeuge = {w.name: w for w in gedaechtnis_werkzeuge(gedaechtnis, Bestaetigungen())}
    antwort = werkzeuge["korrigieren"].funktion(
        richtig="Louis' Velo ist blau", falsch="Louis' Velo ist rot"
    )
    assert "Korrektur gespeichert" in antwort and "Ersetzt" in antwort
    texte = [f.text for f in gedaechtnis.alle()]
    assert "Louis' Velo ist rot" not in texte  # nicht doppelt
    assert gedaechtnis.holen(alt.id).text == "Louis' Velo ist blau"  # type: ignore[union-attr]
    kontext = gedaechtnis.kontext("Welche Farbe hat mein Velo?") or ""
    assert "## Korrekturen von Louis (unbedingt beachten)" in kontext
    assert "Korrektur: Louis' Velo ist blau" in kontext


def test_zweite_korrektur_ersetzt_die_erste(gedaechtnis):
    gedaechtnis.korrigieren("Louis' Velo ist blau", "Louis' Velo ist rot")
    gedaechtnis.korrigieren("Louis' Velo ist blau-weiss", "Louis' Velo ist blau")
    korrekturen = gedaechtnis.alle("korrekturen")
    assert len(korrekturen) == 1 and "blau-weiss" in korrekturen[0].text


def test_korrektur_ueber_den_chat(einstellungen, llama):
    app = create_app(einstellungen, llama_transport=llama.transport)
    kopf = {"Authorization": f"Bearer {API_KEY}", "X-Missa-Session": "s1"}
    llama.antworten = [
        werkzeug_aufruf(
            "korrigieren", {"richtig": "Louis wohnt in Bern", "falsch": "Louis wohnt in Zürich"}
        ),
        text("Danke, hab's mir gemerkt: Bern!"),
        text("Du wohnst in Bern."),
    ]
    with TestClient(app) as client:
        client.post(
            "/v1/chat/completions",
            headers=kopf,
            json={"messages": [{"role": "user", "content": "Das war falsch, ich wohne in Bern"}]},
        )
        assert "## Louis korrigiert dich" in llama.system(0)
        client.post(
            "/v1/chat/completions",
            headers={**kopf, "X-Missa-Session": "s2"},
            json={"messages": [{"role": "user", "content": "Wo wohne ich?"}]},
        )
    assert "Korrektur: Louis wohnt in Bern" in llama.system(-1)


# ---------------------------------------------------------------------- Vergessen


def test_vergiss_x_ist_danach_nicht_mehr_abrufbar(datei, gedaechtnis):
    gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    gedaechtnis.hinzufuegen("Louis mag Velofahren", "vorlieben")
    nachricht_vor_tagen(datei, 1, "s", "Mein Kaffee heute war super")
    bestaetigungen = Bestaetigungen()
    vergessen = {w.name: w for w in gedaechtnis_werkzeuge(gedaechtnis, bestaetigungen)}["vergessen"]

    vorschau = vergessen.funktion(thema="Kaffee")
    assert vorschau.startswith("NOCH NICHT GELÖSCHT")
    assert "1 Fakt(en) und 1 Nachricht(en)" in vorschau
    assert gedaechtnis.suchen("Kaffee")  # noch da
    aktuelle_anfrage.set("anfrage-2")  # Louis: „Ja“
    assert "endgültig gelöscht" in vergessen.funktion(thema="Kaffee", bestaetigt=True)

    assert gedaechtnis.suchen("Kaffee") == []
    assert gedaechtnis.gespraeche_suchen("Kaffee") == []
    assert "Kaffee" not in (gedaechtnis.kontext("Wie trinke ich meinen Kaffee?") or "")
    with sqlite3.connect(datei) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM fakten WHERE text LIKE '%Kaffee%'").fetchone()[0] == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM fakten_fts WHERE fakten_fts MATCH 'kaffee'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM nachrichten WHERE inhalt LIKE '%Kaffee%'").fetchone()[
                0
            ]
            == 0
        )
    assert [f.text for f in gedaechtnis.alle()] == ["Louis mag Velofahren"]


def test_vergessen_ist_zerstoererisch(gedaechtnis):
    from missa.core.werkzeuge import Stufe

    stufen = {w.name: w.stufe for w in gedaechtnis_werkzeuge(gedaechtnis, Bestaetigungen())}
    assert stufen["vergessen"] is Stufe.ZERSTOERERISCH
    assert stufen["korrigieren"] is Stufe.SCHREIBEN
    assert stufen["frueher_gesagt"] is Stufe.LESEN


def test_vergessen_ueber_die_rechte_schicht(gedaechtnis):
    import asyncio

    gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    kasten = Werkzeugkasten(gedaechtnis_werkzeuge(gedaechtnis, Bestaetigungen()))
    argumente = {"thema": "Kaffee"}
    antwort = asyncio.run(kasten.ausfuehren("vergessen", argumente, "a1", nachricht="Vergiss das"))
    assert antwort.startswith("NICHT AUSGEFÜHRT")
    assert gedaechtnis.suchen("Kaffee")
    asyncio.run(kasten.ausfuehren("vergessen", argumente, "a2", nachricht="Ja"))
    assert gedaechtnis.suchen("Kaffee") == []


# ---------------------------------------------------------------------- Backup


@pytest.fixture
def installation(tmp_path) -> dict[str, Path]:
    """Eine laufende Installation mit Gedächtnis, Routine, Regel, Charakter und Konfiguration."""
    daten = tmp_path / "alt" / "data"
    pfade = {
        "daten": daten,
        "db": daten / "missa.db",
        "charakter": tmp_path / "alt" / "etc" / "charakter.md",
        "stil": daten / "schreibstil.md",
        "env": tmp_path / "alt" / "etc" / "missa.env",
        "backup": tmp_path / "backup",
    }
    pfade["charakter"].parent.mkdir(parents=True)
    pfade["charakter"].write_text("Du bist Missa. Frech, aber lieb.", encoding="utf-8")
    pfade["env"].write_text(
        "MISSA_API_KEY=supergeheim\nNEXTCLOUD_URL=https://cloud.example.ch\n"
        "NEXTCLOUD_APP_PASSWORD=abc123\nMAIL_PASSWORD=pw\nNC_TALK_BOT_SECRET=x\n"
        "export TIMEZONE=Europe/Zurich\n",
        encoding="utf-8",
    )
    gedaechtnis = Gedaechtnis(pfade["db"], ZONE)
    gedaechtnis.hinzufuegen("Louis trinkt Kaffee ohne Zucker", "vorlieben")
    gedaechtnis.korrigieren("Louis wohnt in Bern", "Louis wohnt in Zürich")
    planer = Zeitplaner(pfade["db"], ZONE, Zustellung())
    planer.anlegen(
        "Wochenrückblick",
        datetime(2026, 11, 13, 17, 0, tzinfo=ZONE),
        Wiederholung("woechentlich", (4,)),
        art="routine",
        anweisung="Schreib mir einen Wochenrückblick",
    )
    Vorgaben(pfade["db"]).setzen("pausen.puffer_minuten", "20")
    pfade["stil"].write_text("Kurz, per Du.", encoding="utf-8")
    return pfade


def sicherung_fuer(pfade: dict[str, Path], **weiter) -> Sicherung:
    return Sicherung(
        Quellen(pfade["db"], pfade["charakter"], pfade["stil"], pfade["env"]),
        pfade["backup"],
        ZONE,
        **weiter,
    )


def test_backup_enthaelt_alles_ohne_geheimnisse(installation):
    datei = sicherung_fuer(installation).erstellen()
    assert datei.name.startswith("missa-backup-") and datei.name.endswith(".tar.gz")
    assert oct(datei.stat().st_mode)[-3:] == "600"
    with tarfile.open(datei) as archiv:
        namen = set(archiv.getnames())
        env = archiv.extractfile("missa.env.ohne-geheimnisse").read().decode()  # type: ignore[union-attr]
    assert namen == {
        "missa.db",
        "charakter.md",
        "schreibstil.md",
        "missa.env.ohne-geheimnisse",
        "manifest.json",
    }
    for geheim in ("supergeheim", "abc123", "MAIL_PASSWORD=pw", "NC_TALK_BOT_SECRET=x"):
        assert geheim not in env
    assert "NEXTCLOUD_URL=https://cloud.example.ch" in env
    assert "export TIMEZONE=Europe/Zurich" in env


def test_backup_auf_frischer_installation_wiederhergestellt(installation, tmp_path):
    datei = sicherung_fuer(installation).erstellen()
    neu = Quellen(
        tmp_path / "neu" / "data" / "missa.db",
        tmp_path / "neu" / "etc" / "charakter.md",
        tmp_path / "neu" / "data" / "schreibstil.md",
        tmp_path / "neu" / "etc" / "missa.env",
    )
    neu.env_datei.parent.mkdir(parents=True)  # type: ignore[union-attr]
    neu.env_datei.write_text("MISSA_API_KEY=neu\n", encoding="utf-8")  # type: ignore[union-attr]

    ergebnis = wiederherstellen(datei, neu)
    assert str(neu.datenbank) in ergebnis["wiederhergestellt"]

    gedaechtnis = Gedaechtnis(neu.datenbank, ZONE)
    assert [f.text for f in gedaechtnis.suchen("Kaffee")] == ["Louis trinkt Kaffee ohne Zucker"]
    assert "Bern" in (gedaechtnis.kontext("Wo wohne ich?") or "")
    [routine] = Zeitplaner(neu.datenbank, ZONE, Zustellung()).offene("routine")
    assert routine.anweisung == "Schreib mir einen Wochenrückblick"
    assert Vorgaben(neu.datenbank).lesen("pausen.puffer_minuten", "") == "20"
    assert neu.charakter.read_text(encoding="utf-8") == "Du bist Missa. Frech, aber lieb."
    assert neu.schreibstil.read_text(encoding="utf-8") == "Kurz, per Du."  # type: ignore[union-attr]
    # Die echte Konfiguration bleibt, die aus dem Backup liegt daneben.
    assert neu.env_datei.read_text(encoding="utf-8") == "MISSA_API_KEY=neu\n"  # type: ignore[union-attr]
    assert "supergeheim" not in Path(ergebnis["konfiguration"]).read_text(encoding="utf-8")


def test_wiederherstellen_hebt_bisherige_dateien_auf(installation, tmp_path):
    datei = sicherung_fuer(installation).erstellen()
    installation["charakter"].write_text("Neuer Charakter", encoding="utf-8")
    ergebnis = wiederherstellen(
        datei, Quellen(installation["db"], installation["charakter"], None, None)
    )
    aufgehoben = [Path(p) for p in ergebnis["vorher_gesichert"]]
    assert any(
        p.read_text(encoding="utf-8") == "Neuer Charakter"
        for p in aufgehoben
        if p.name.startswith("charakter.md")
    )


def test_kein_backup_wird_abgelehnt(tmp_path):
    fremd = tmp_path / "fremd.tar.gz"
    with tarfile.open(fremd, "w:gz") as archiv:
        leer = tmp_path / "x.txt"
        leer.write_text("x")
        archiv.add(leer, arcname="x.txt")
    with pytest.raises(ValueError, match="kein Missa-Backup"):
        wiederherstellen(fremd, Quellen(tmp_path / "db", tmp_path / "c.md"))


def test_aeltere_backups_werden_aufgeraeumt(installation):
    zeit = [datetime(2026, 11, 1, 3, 30, tzinfo=ZONE)]
    sicherung = sicherung_fuer(installation, uhr=lambda: zeit[0])
    for _ in range(16):
        sicherung.erstellen()
        zeit[0] += timedelta(days=1)
    backups = sicherung.liste()
    assert len(backups) == 14
    assert backups[0].name == "missa-backup-20261103-033000.tar.gz"
    (installation["backup"] / "notizen.txt").write_text("bleibt")
    sicherung.aufraeumen()
    assert (installation["backup"] / "notizen.txt").exists()  # fremde Dateien bleiben


def test_env_ohne_geheimnisse():
    text = env_ohne_geheimnisse(
        "A=1\nPROXMOX_TOKEN_SECRET=x\n# MAIL_PASSWORD=kommentar\nLLAMA_API_KEY_FILE=/k\n"
    )
    assert "A=1" in text and "PROXMOX_TOKEN_SECRET=  #" in text
    assert "# MAIL_PASSWORD=kommentar" in text
    assert "LLAMA_API_KEY_FILE=/k" not in text  # lieber zu viel leeren als zu wenig


def test_taeglich_ueber_den_zeitplaner(einstellungen, llama):
    app = create_app(einstellungen, llama_transport=llama.transport)
    auftrag = app.state.zeitplaner._auftraege["sicherung"]
    assert auftrag.uhrzeit == time(3, 30)
    app.state.sicherung.taeglich()
    assert len(app.state.sicherung.liste()) == 1
    assert app.state.sicherung.ordner == einstellungen.backup_ordner
    with TestClient(app) as client:
        werkzeuge = client.get("/health").json()["werkzeuge"]
    assert {"backup_erstellen", "backups_anzeigen"} <= set(werkzeuge)


def test_standard_ordner_liegt_im_datenordner():
    from missa.core.config import Einstellungen

    e = Einstellungen()
    assert e.backup_ordner == Path("/opt/missa/data/backup")
    assert e.backup_behalten == 14
    e = Einstellungen.aus_umgebung(
        {"MISSA_BACKUP_ORDNER": "/srv/backup", "MISSA_BACKUP_BEHALTEN": "7"}
    )
    assert (e.backup_ordner, e.backup_behalten) == (Path("/srv/backup"), 7)


def _umgebung(pfade: dict[str, Path]) -> dict[str, str]:
    return {
        "MISSA_ENV_DATEI": str(pfade["env"]),
        "MISSA_DATEN": str(pfade["daten"]),
        "MISSA_CHARAKTER_DATEI": str(pfade["charakter"]),
        "MISSA_BACKUP_ORDNER": str(pfade["backup"]),
        "MAIL_STIL_DATEI": str(pfade["stil"]),
    }


def test_kommandozeile(installation, monkeypatch, capsys):
    for name, wert in _umgebung(installation).items():
        monkeypatch.setenv(name, wert)
    assert sicherung_main(["erstellen"]) == 0
    [datei] = Sicherung(
        Quellen(installation["db"], installation["charakter"]), installation["backup"], ZONE
    ).liste()
    assert sicherung_main(["liste"]) == 0
    Gedaechtnis(installation["db"], ZONE).hinzufuegen("Etwas Neues nach dem Backup", "sonstiges")
    assert sicherung_main(["wiederherstellen", datei.name]) == 0
    assert "eingespielt" in capsys.readouterr().out
    texte = [f.text for f in Gedaechtnis(installation["db"], ZONE).alle()]
    assert "Etwas Neues nach dem Backup" not in texte


def test_script_wiederherstellen(installation):
    datei = sicherung_fuer(installation).erstellen()
    Gedaechtnis(installation["db"], ZONE).hinzufuegen("Nach dem Backup", "sonstiges")
    umgebung = {
        **os.environ,
        **_umgebung(installation),
        "MISSA_PYTHON": sys.executable,
        "MISSA_WIEDERHERSTELLEN_OHNE_ROOT": "1",
    }
    lauf = subprocess.run(
        ["bash", str(REPO / "scripts" / "wiederherstellen.sh"), str(datei), "--ja"],
        env=umgebung,
        capture_output=True,
        text=True,
        check=False,
    )
    assert lauf.returncode == 0, lauf.stderr
    assert "Fertig" in lauf.stdout
    assert "Nach dem Backup" not in [f.text for f in Gedaechtnis(installation["db"], ZONE).alle()]
    abbruch = subprocess.run(
        ["bash", str(REPO / "scripts" / "wiederherstellen.sh"), "gibt-es-nicht.tar.gz", "--ja"],
        env=umgebung,
        capture_output=True,
        text=True,
        check=False,
    )
    assert abbruch.returncode == 1
