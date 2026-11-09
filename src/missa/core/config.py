"""Konfiguration von Missa-Core aus Umgebungsvariablen.

Im Betrieb setzt systemd die Variablen aus ``/etc/missa/missa.env``
(``EnvironmentFile=``). Beim Start von Hand liest :func:`env_datei_laden`
dieselbe Datei; bereits gesetzte Variablen haben Vorrang.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STANDARD_ENV_DATEI = Path("/etc/missa/missa.env")


class KonfigurationsFehler(ValueError):
    """Die Konfiguration ist unvollständig oder ungültig."""


@dataclass(frozen=True)
class Einstellungen:
    """Alle Einstellungen von Missa-Core."""

    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str = ""
    charakter_datei: Path = Path("/etc/missa/charakter.md")
    daten: Path = Path("/opt/missa/data")
    llama_url: str = "http://127.0.0.1:8080"
    llama_api_key_file: Path = Path("/etc/missa/llama.key")
    llama_timeout: float = 600.0
    timezone: str = "Europe/Zurich"
    nextcloud_url: str = ""
    nextcloud_user: str = ""
    nextcloud_app_password: str = ""
    nextcloud_kalender: str = ""
    nextcloud_aufgaben_liste: str = ""

    @property
    def nextcloud_konfiguriert(self) -> bool:
        """``True``, wenn Adresse, Benutzer und App-Passwort für Nextcloud gesetzt sind."""
        return bool(self.nextcloud_url and self.nextcloud_user and self.nextcloud_app_password)

    @property
    def datenbank(self) -> Path:
        """SQLite-Datei für Verlauf und Gedächtnis."""
        return self.daten / "missa.db"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def aus_umgebung(cls, env: Mapping[str, str] | None = None) -> Einstellungen:
        """Liest die Einstellungen aus den Umgebungsvariablen (oder ``env``)."""
        env = os.environ if env is None else env

        def wert(name: str, standard: str) -> str:
            text = env.get(name, "").strip()
            return text or standard

        try:
            port = int(wert("MISSA_PORT", "8000"))
        except ValueError as fehler:
            raise KonfigurationsFehler("MISSA_PORT muss eine Zahl sein") from fehler
        try:
            llama_timeout = float(wert("LLAMA_TIMEOUT", "600"))
        except ValueError as fehler:
            raise KonfigurationsFehler("LLAMA_TIMEOUT muss eine Zahl (Sekunden) sein") from fehler

        return cls(
            host=wert("MISSA_HOST", cls.host),
            port=port,
            api_key=env.get("MISSA_API_KEY", "").strip(),
            charakter_datei=Path(wert("MISSA_CHARAKTER_DATEI", str(cls.charakter_datei))),
            daten=Path(wert("MISSA_DATEN", str(cls.daten))),
            llama_url=wert("LLAMA_URL", cls.llama_url).rstrip("/"),
            llama_api_key_file=Path(wert("LLAMA_API_KEY_FILE", str(cls.llama_api_key_file))),
            llama_timeout=llama_timeout,
            timezone=wert("TIMEZONE", cls.timezone),
            nextcloud_url=env.get("NEXTCLOUD_URL", "").strip().rstrip("/"),
            nextcloud_user=env.get("NEXTCLOUD_USER", "").strip(),
            nextcloud_app_password=env.get("NEXTCLOUD_APP_PASSWORD", "").strip(),
            nextcloud_kalender=env.get("NEXTCLOUD_KALENDER", "").strip(),
            nextcloud_aufgaben_liste=env.get("NEXTCLOUD_AUFGABEN_LISTE", "").strip(),
        )

    def pruefen(self) -> None:
        """Prüft, ob der Dienst mit diesen Einstellungen starten darf."""
        if not self.api_key:
            raise KonfigurationsFehler(
                "MISSA_API_KEY ist nicht gesetzt – ohne Schlüssel startet Missa-Core nicht"
            )
        if not 0 < self.port < 65536:
            raise KonfigurationsFehler("MISSA_PORT muss zwischen 1 und 65535 liegen")
        if self.llama_timeout <= 0:
            raise KonfigurationsFehler("LLAMA_TIMEOUT muss größer als 0 sein")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as fehler:
            raise KonfigurationsFehler(f"Unbekannte Zeitzone: {self.timezone}") from fehler


def env_datei_laden(pfad: Path, env: MutableMapping[str, str] | None = None) -> bool:
    """Lädt ``NAME=WERT``-Zeilen aus ``pfad`` in die Umgebung.

    Bereits gesetzte Variablen werden nicht überschrieben. Leerzeilen und
    Kommentare (``#``) werden übersprungen, umschließende Anführungszeichen
    entfernt. Gibt ``True`` zurück, wenn die Datei gelesen wurde.
    """
    env = os.environ if env is None else env
    try:
        zeilen = pfad.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for zeile in zeilen:
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        if zeile.startswith("export "):
            zeile = zeile.removeprefix("export ").strip()
        name, _, wert = zeile.partition("=")
        name, wert = name.strip(), wert.strip()
        if len(wert) >= 2 and wert[0] == wert[-1] and wert[0] in "\"'":
            wert = wert[1:-1]
        if name and name not in env:
            env[name] = wert
    return True
