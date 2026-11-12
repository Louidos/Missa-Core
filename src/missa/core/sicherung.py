"""Missa sichern und wiederherstellen.

Ein Backup ist **eine** Datei ``missa-backup-JJJJMMTT-HHMMSS.tar.gz`` mit:

- ``missa.db`` – Gedächtnis, Korrekturen, Gesprächsverlauf, Regeln und Einstellungen aus dem
  Gespräch (``vorgaben``), Erinnerungen, Routinen, Nachtschicht, Kontaktpflege, Protokolle
  (konsistente Kopie über die SQLite-Backup-API, auch während der Dienst läuft)
- ``charakter.md`` – Missas Charakterprofil
- ``schreibstil.md`` – Louis' Schreibstil-Profil (falls vorhanden)
- ``missa.env.ohne-geheimnisse`` – die Konfiguration, **alle Passwörter, Schlüssel und
  Tokens geleert**
- ``manifest.json`` – Version, Zeitpunkt, Inhalt

Täglich legt der Zeitplaner ein Backup in ``MISSA_BACKUP_ORDNER`` an und behält die
neuesten ``MISSA_BACKUP_BEHALTEN`` (Standard 14). Wiederherstellen geht nur auf ai01:
``sudo scripts/wiederherstellen.sh <datei>`` (bzw. ``python -m missa.core.sicherung``).
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa import __version__
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler

log = logging.getLogger(__name__)

PRAEFIX = "missa-backup-"
ENDUNG = ".tar.gz"
#: Einstellungen, deren Wert nie in ein Backup kommt.
GEHEIM = re.compile(r"(PASSWORD|PASSWORT|SECRET|TOKEN|API_KEY|_KEY$|_KEY_|KENNWORT)", re.I)


def env_ohne_geheimnisse(text: str) -> str:
    """Konfiguration mit geleerten Werten für alles, was nach Geheimnis aussieht."""
    zeilen = []
    for zeile in text.splitlines():
        roh = zeile.strip()
        name = roh.removeprefix("export ").partition("=")[0].strip()
        if roh and not roh.startswith("#") and "=" in roh and GEHEIM.search(name):
            zeilen.append(f"{name}=  # beim Backup geleert – bitte neu eintragen")
        else:
            zeilen.append(zeile)
    return "\n".join(zeilen) + "\n"


@dataclass(frozen=True)
class Quellen:
    """Wo die zu sichernden Dateien liegen."""

    datenbank: Path
    charakter: Path
    schreibstil: Path | None = None
    env_datei: Path | None = None


class Sicherung:
    """Legt Backups an, räumt alte auf und listet sie."""

    def __init__(
        self,
        quellen: Quellen,
        ordner: Path,
        zone: ZoneInfo,
        behalten: int = 14,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.quellen = quellen
        self.ordner = ordner
        self.zone = zone
        self.behalten = max(1, behalten)
        self._uhr = uhr or (lambda: datetime.now(zone))

    def liste(self) -> list[Path]:
        if not self.ordner.is_dir():
            return []
        return sorted(self.ordner.glob(f"{PRAEFIX}*{ENDUNG}"))

    def erstellen(self) -> Path:
        """Schreibt ein neues Backup und räumt danach alte auf."""
        try:
            self.ordner.mkdir(parents=True, exist_ok=True)
        except OSError as fehler:
            raise WerkzeugFehler(
                f"Backup-Ordner {self.ordner} lässt sich nicht anlegen ({fehler})"
            ) from fehler
        jetzt = self._uhr().astimezone(self.zone)
        ziel = self.ordner / f"{PRAEFIX}{jetzt:%Y%m%d-%H%M%S}{ENDUNG}"
        inhalt: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            arbeit = Path(tmp)
            if self.quellen.datenbank.is_file():
                db_kopie = arbeit / "missa.db"
                quelle = sqlite3.connect(self.quellen.datenbank, timeout=30)
                try:
                    kopie = sqlite3.connect(db_kopie)
                    try:
                        quelle.backup(kopie)
                    finally:
                        kopie.close()
                finally:
                    quelle.close()
                inhalt.append("missa.db")
            dateien = [("charakter.md", self.quellen.charakter)]
            if self.quellen.schreibstil is not None:
                dateien.append(("schreibstil.md", self.quellen.schreibstil))
            for name, pfad in dateien:
                if pfad.is_file():
                    shutil.copyfile(pfad, arbeit / name)
                    inhalt.append(name)
            if self.quellen.env_datei is not None and self.quellen.env_datei.is_file():
                try:
                    text = self.quellen.env_datei.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text:
                    (arbeit / "missa.env.ohne-geheimnisse").write_text(
                        env_ohne_geheimnisse(text), encoding="utf-8"
                    )
                    inhalt.append("missa.env.ohne-geheimnisse")
            manifest = {
                "version": __version__,
                "erstellt": jetzt.isoformat(timespec="seconds"),
                "inhalt": inhalt,
            }
            (arbeit / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            teil = ziel.with_suffix(".teil")
            with tarfile.open(teil, "w:gz") as archiv:
                for name in [*inhalt, "manifest.json"]:
                    archiv.add(arbeit / name, arcname=name)
            os.chmod(teil, 0o600)
            teil.replace(ziel)
        log.info("Backup angelegt: %s (%s)", ziel, ", ".join(inhalt))
        self.aufraeumen()
        return ziel

    def aufraeumen(self) -> list[Path]:
        """Löscht alte Backups – nur Dateien mit dem Backup-Namensmuster in diesem Ordner."""
        alte = self.liste()[: -self.behalten]
        for pfad in alte:
            pfad.unlink(missing_ok=True)
            log.info("Altes Backup gelöscht: %s", pfad.name)
        return alte

    def taeglich(self) -> None:
        """Täglicher Auftrag des Zeitplaners; Fehler werden geloggt, nie geworfen."""
        try:
            self.erstellen()
        except Exception:
            log.exception("Tägliches Backup fehlgeschlagen")

    # -------------------------------------------------------------- Werkzeuge

    def erstellen_werkzeug(self) -> str:
        pfad = self.erstellen()
        return (
            f"Backup angelegt: {pfad} ({_groesse(pfad.stat().st_size)}). Es sind jetzt "
            f"{len(self.liste())} Backups da (es bleiben höchstens {self.behalten})."
        )

    def anzeigen(self) -> str:
        backups = self.liste()
        if not backups:
            return f"Im Ordner {self.ordner} gibt es noch kein Backup."
        zeilen = [f"- {p.name} ({_groesse(p.stat().st_size)})" for p in reversed(backups)]
        return (
            f"Backups in {self.ordner} (neueste zuerst, höchstens {self.behalten}):\n"
            + "\n".join(zeilen)
            + "\nWiederherstellen geht nur auf ai01: sudo scripts/wiederherstellen.sh <datei>"
        )


def _groesse(anzahl: int) -> str:
    return f"{anzahl / 1024:.0f} KB" if anzahl < 1024 * 1024 else f"{anzahl / 1024 / 1024:.1f} MB"


def sicherungs_werkzeuge(sicherung: Sicherung) -> list[Werkzeug]:
    return [
        Werkzeug(
            "backup_erstellen",
            "Legt sofort ein Backup von Missa an (Gedächtnis, Charakter, Regeln, Routinen, "
            "Konfiguration ohne Geheimnisse). Täglich passiert das ohnehin automatisch.",
            {"type": "object", "properties": {}},
            sicherung.erstellen_werkzeug,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "backups_anzeigen",
            "Listet die vorhandenen Backups von Missa.",
            {"type": "object", "properties": {}},
            sicherung.anzeigen,
            stufe=Stufe.LESEN,
        ),
    ]


# ---------------------------------------------------------------------- Wiederherstellen


def _vorher_sichern(pfad: Path, zeit: str) -> Path | None:
    if not pfad.exists():
        return None
    ziel = pfad.with_name(f"{pfad.name}.vor-wiederherstellung-{zeit}")
    shutil.move(pfad, ziel)
    return ziel


def wiederherstellen(datei: Path, ziel: Quellen) -> dict[str, Any]:
    """Spielt ein Backup ein. Der Dienst muss dabei gestoppt sein.

    Bestehende Dateien werden vorher umbenannt (``….vor-wiederherstellung-…``), die
    Konfiguration kommt als ``missa.env.aus-backup`` daneben – die echte ``missa.env``
    (mit den Geheimnissen) wird nie überschrieben.
    """
    if not datei.is_file():
        raise FileNotFoundError(f"Backup {datei} gibt es nicht")
    zeit = datetime.now().strftime("%Y%m%d-%H%M%S")
    ergebnis: dict[str, Any] = {"wiederhergestellt": [], "vorher_gesichert": []}
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(datei, "r:gz") as archiv:
        archiv.extractall(tmp, filter="data")
        arbeit = Path(tmp)
        manifest_datei = arbeit / "manifest.json"
        if not manifest_datei.is_file():
            raise ValueError(f"{datei.name} ist kein Missa-Backup (manifest.json fehlt)")
        manifest = json.loads(manifest_datei.read_text(encoding="utf-8"))
        ergebnis["manifest"] = manifest
        if (arbeit / "missa.db").is_file():
            # Prüfen, bevor etwas ersetzt wird.
            pruefung = sqlite3.connect(arbeit / "missa.db")
            try:
                if pruefung.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Die Datenbank im Backup ist beschädigt")
            finally:
                pruefung.close()
            ziel.datenbank.parent.mkdir(parents=True, exist_ok=True)
            for rest in ("-wal", "-shm"):
                Path(f"{ziel.datenbank}{rest}").unlink(missing_ok=True)
            if alt := _vorher_sichern(ziel.datenbank, zeit):
                ergebnis["vorher_gesichert"].append(str(alt))
            shutil.copyfile(arbeit / "missa.db", ziel.datenbank)
            ergebnis["wiederhergestellt"].append(str(ziel.datenbank))
        paare = [("charakter.md", ziel.charakter), ("schreibstil.md", ziel.schreibstil)]
        for name, pfad in paare:
            if pfad is None or not (arbeit / name).is_file():
                continue
            pfad.parent.mkdir(parents=True, exist_ok=True)
            if alt := _vorher_sichern(pfad, zeit):
                ergebnis["vorher_gesichert"].append(str(alt))
            shutil.copyfile(arbeit / name, pfad)
            ergebnis["wiederhergestellt"].append(str(pfad))
        if (arbeit / "missa.env.ohne-geheimnisse").is_file() and ziel.env_datei is not None:
            env_ziel = ziel.env_datei.with_name(ziel.env_datei.name + ".aus-backup")
            env_ziel.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(arbeit / "missa.env.ohne-geheimnisse", env_ziel)
            ergebnis["konfiguration"] = str(env_ziel)
    return ergebnis


# ---------------------------------------------------------------------- Kommandozeile


def _quellen_aus_umgebung() -> tuple[Quellen, Path, int, ZoneInfo]:
    from missa.core.config import STANDARD_ENV_DATEI, Einstellungen, env_datei_laden

    env_datei = Path(os.environ.get("MISSA_ENV_DATEI", str(STANDARD_ENV_DATEI)))
    env_datei_laden(env_datei)
    e = Einstellungen.aus_umgebung()
    return (
        Quellen(e.datenbank, e.charakter_datei, e.stil_datei, env_datei),
        e.backup_ordner,
        e.backup_behalten,
        e.zone,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m missa.core.sicherung")
    befehle = parser.add_subparsers(dest="befehl", required=True)
    befehle.add_parser("erstellen", help="jetzt ein Backup anlegen")
    befehle.add_parser("liste", help="vorhandene Backups zeigen")
    zurueck = befehle.add_parser("wiederherstellen", help="ein Backup einspielen")
    zurueck.add_argument("datei", type=Path)
    argumente = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    quellen, ordner, behalten, zone = _quellen_aus_umgebung()
    if argumente.befehl == "erstellen":
        print(Sicherung(quellen, ordner, zone, behalten).erstellen())
        return 0
    if argumente.befehl == "liste":
        print(Sicherung(quellen, ordner, zone, behalten).anzeigen())
        return 0
    datei = argumente.datei
    if not datei.is_file() and (ordner / datei.name).is_file():
        datei = ordner / datei.name
    try:
        ergebnis = wiederherstellen(datei, quellen)
    except (OSError, ValueError, tarfile.TarError, sqlite3.Error) as fehler:
        print(f"Wiederherstellen fehlgeschlagen: {fehler}", file=sys.stderr)
        return 1
    ausgabe = io.StringIO()
    print(f"Backup vom {ergebnis['manifest'].get('erstellt')} eingespielt:", file=ausgabe)
    for pfad in ergebnis["wiederhergestellt"]:
        print(f"  ✓ {pfad}", file=ausgabe)
    for pfad in ergebnis["vorher_gesichert"]:
        print(f"  (bisherige Datei aufgehoben als {pfad})", file=ausgabe)
    if "konfiguration" in ergebnis:
        print(
            f"  Konfiguration ohne Geheimnisse: {ergebnis['konfiguration']} – mit "
            "/etc/missa/missa.env vergleichen, Passwörter und Schlüssel neu eintragen.",
            file=ausgabe,
        )
    print(ausgabe.getvalue(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
