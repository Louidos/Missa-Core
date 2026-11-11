"""Konfiguration von Missa-Core aus Umgebungsvariablen.

Im Betrieb setzt systemd die Variablen aus ``/etc/missa/missa.env``
(``EnvironmentFile=``). Beim Start von Hand liest :func:`env_datei_laden`
dieselbe Datei; bereits gesetzte Variablen haben Vorrang.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from missa.tools.pausen import PausenRegeln

STANDARD_ENV_DATEI = Path("/etc/missa/missa.env")
WAHR = frozenset({"1", "true", "ja", "yes", "on", "an"})


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
    nc_talk_enabled: bool = False
    nc_talk_base_url: str = ""
    nc_talk_api_url: str = ""
    nc_talk_bot_secret: str = ""
    nc_talk_allowed_users: tuple[str, ...] = ()
    nc_talk_timeout: float = 300.0
    nc_talk_notify_conversation: str = ""
    nc_talk_notify_rate: int = 10
    mail_imap_host: str = ""
    mail_imap_port: int = 993
    mail_smtp_host: str = ""
    mail_smtp_port: int = 465
    mail_user: str = ""
    mail_password: str = ""
    mail_absender: str = ""
    mail_ordner_entwuerfe: str = "Drafts"
    mail_ordner_gesendet: str = "Sent"
    mail_stil_datei: Path | None = None
    nextcloud_adressbuch: str = ""
    kontakte_geburtstag_vorlauf: int = 3
    kontakte_pruefzeit: time = time(8, 0)
    pausen_aktiv: bool = True
    pausen_kalender: str = "Pausen"
    pausen_farbe: str = "türkis"
    pausen_puffer_minuten: int = 10
    pausen_mittag_minuten: int = 30
    pausen_mittag_von: time = time(11, 30)
    pausen_mittag_bis: time = time(14, 0)
    pausen_max_stunden: float = 8.0
    pausen_wegzeit_minuten: int = 30
    pausen_pruefzeit: time = time(19, 0)
    nachtschicht_von: time = time(1, 0)
    nachtschicht_bis: time = time(6, 0)

    @property
    def talk_aktiv(self) -> bool:
        """Talk ist nur an, wenn eingeschaltet und Adresse und Bot-Secret gesetzt sind."""
        return bool(self.nc_talk_enabled and self.nc_talk_base_url and self.nc_talk_bot_secret)

    @property
    def talk_api_url(self) -> str:
        """Adresse, über die Missa-Core Antworten an Nextcloud schickt."""
        return self.nc_talk_api_url or self.nc_talk_base_url

    @property
    def nextcloud_konfiguriert(self) -> bool:
        """``True``, wenn Adresse, Benutzer und App-Passwort für Nextcloud gesetzt sind."""
        return bool(self.nextcloud_url and self.nextcloud_user and self.nextcloud_app_password)

    @property
    def mail_konfiguriert(self) -> bool:
        """``True``, wenn IMAP, SMTP, Benutzer und Passwort für E-Mail gesetzt sind."""
        return bool(
            self.mail_imap_host and self.mail_smtp_host and self.mail_user and self.mail_password
        )

    @property
    def stil_datei(self) -> Path:
        """Bearbeitbare Datei mit Louis' Schreibstil-Profil."""
        return self.mail_stil_datei or self.daten / "schreibstil.md"

    def pausen_regeln(self) -> PausenRegeln:
        """Grenzwerte für die Pausen- und Puffer-Prüfung."""
        from missa.tools.pausen import PausenRegeln

        return PausenRegeln(
            puffer=timedelta(minutes=self.pausen_puffer_minuten),
            mittag_dauer=timedelta(minutes=self.pausen_mittag_minuten),
            mittag_von=self.pausen_mittag_von,
            mittag_bis=self.pausen_mittag_bis,
            max_stunden=self.pausen_max_stunden,
            wegzeit=timedelta(minutes=self.pausen_wegzeit_minuten),
            kalender=self.pausen_kalender,
            farbe=self.pausen_farbe,
        )

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
        try:
            talk_timeout = float(wert("NC_TALK_TIMEOUT", "300"))
        except ValueError as fehler:
            raise KonfigurationsFehler("NC_TALK_TIMEOUT muss eine Zahl (Sekunden) sein") from fehler
        try:
            talk_rate = int(wert("NC_TALK_NOTIFY_RATE", "10"))
        except ValueError as fehler:
            raise KonfigurationsFehler(
                "NC_TALK_NOTIFY_RATE muss eine ganze Zahl (Nachrichten pro Minute) sein"
            ) from fehler

        def zahl(name: str, standard: int) -> int:
            try:
                return int(wert(name, str(standard)))
            except ValueError as fehler:
                raise KonfigurationsFehler(f"{name} muss eine ganze Zahl sein") from fehler

        stil = env.get("MAIL_STIL_DATEI", "").strip()

        def kommazahl(name: str, standard: float) -> float:
            try:
                return float(wert(name, str(standard)).replace(",", "."))
            except ValueError as fehler:
                raise KonfigurationsFehler(f"{name} muss eine Zahl sein") from fehler

        def uhrzeit(name: str, standard: str) -> time:
            text = wert(name, standard)
            try:
                stunde, _, minute = text.partition(":")
                return time(int(stunde), int(minute or 0))
            except ValueError as fehler:
                raise KonfigurationsFehler(f"{name} muss eine Uhrzeit wie 08:00 sein") from fehler

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
            nc_talk_enabled=env.get("NC_TALK_ENABLED", "").strip().casefold() in WAHR,
            nc_talk_base_url=env.get("NC_TALK_BASE_URL", "").strip().rstrip("/"),
            nc_talk_api_url=env.get("NC_TALK_API_URL", "").strip().rstrip("/"),
            nc_talk_bot_secret=env.get("NC_TALK_BOT_SECRET", "").strip(),
            nc_talk_allowed_users=tuple(
                dict.fromkeys(
                    teil.strip()
                    for teil in env.get("NC_TALK_ALLOWED_USERS", "").split(",")
                    if teil.strip()
                )
            ),
            nc_talk_timeout=talk_timeout if talk_timeout > 0 else 300.0,
            nc_talk_notify_conversation=env.get("NC_TALK_NOTIFY_CONVERSATION", "").strip(),
            nc_talk_notify_rate=talk_rate if talk_rate > 0 else 10,
            mail_imap_host=env.get("MAIL_IMAP_HOST", "").strip(),
            mail_imap_port=zahl("MAIL_IMAP_PORT", 993),
            mail_smtp_host=env.get("MAIL_SMTP_HOST", "").strip(),
            mail_smtp_port=zahl("MAIL_SMTP_PORT", 465),
            mail_user=env.get("MAIL_USER", "").strip(),
            mail_password=env.get("MAIL_PASSWORD", "").strip(),
            mail_absender=env.get("MAIL_ABSENDER", "").strip(),
            mail_ordner_entwuerfe=wert("MAIL_ORDNER_ENTWUERFE", cls.mail_ordner_entwuerfe),
            mail_ordner_gesendet=wert("MAIL_ORDNER_GESENDET", cls.mail_ordner_gesendet),
            mail_stil_datei=Path(stil) if stil else None,
            nextcloud_adressbuch=env.get("NEXTCLOUD_ADRESSBUCH", "").strip(),
            kontakte_geburtstag_vorlauf=max(0, zahl("KONTAKTE_GEBURTSTAG_VORLAUF", 3)),
            kontakte_pruefzeit=uhrzeit("KONTAKTE_PRUEFZEIT", "08:00"),
            pausen_aktiv=wert("PAUSEN_AKTIV", "true").casefold() in WAHR,
            pausen_kalender=wert("PAUSEN_KALENDER", cls.pausen_kalender),
            pausen_farbe=wert("PAUSEN_FARBE", cls.pausen_farbe),
            pausen_puffer_minuten=max(0, zahl("PAUSEN_PUFFER_MINUTEN", 10)),
            pausen_mittag_minuten=max(0, zahl("PAUSEN_MITTAG_MINUTEN", 30)),
            pausen_mittag_von=uhrzeit("PAUSEN_MITTAG_VON", "11:30"),
            pausen_mittag_bis=uhrzeit("PAUSEN_MITTAG_BIS", "14:00"),
            pausen_max_stunden=kommazahl("PAUSEN_MAX_STUNDEN", 8.0),
            pausen_wegzeit_minuten=max(0, zahl("PAUSEN_WEGZEIT_MINUTEN", 30)),
            pausen_pruefzeit=uhrzeit("PAUSEN_PRUEFZEIT", "19:00"),
            nachtschicht_von=uhrzeit("NACHTSCHICHT_VON", "01:00"),
            nachtschicht_bis=uhrzeit("NACHTSCHICHT_BIS", "06:00"),
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
        if self.nachtschicht_von == self.nachtschicht_bis:
            raise KonfigurationsFehler(
                "NACHTSCHICHT_VON und NACHTSCHICHT_BIS dürfen nicht gleich sein"
            )
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
