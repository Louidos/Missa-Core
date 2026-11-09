"""Gemeinsame CalDAV-Verbindung zu Nextcloud (für Kalender und Aufgaben).

Die CalDAV-Basis ist ``NEXTCLOUD_URL + /remote.php/dav``. Kalender und
Aufgabenlisten werden über ihren Anzeigenamen gefunden.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import caldav
import icalendar
from caldav.lib import error as caldav_fehler

from missa.core.werkzeuge import WerkzeugFehler

log = logging.getLogger(__name__)

PRODID = "-//Missa//Missa-Core//DE"
WOCHENTAGE_KURZ = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class NextcloudFehler(WerkzeugFehler):
    """Nextcloud ist nicht erreichbar, lehnt ab oder findet etwas nicht."""


class NextcloudVerbindung:
    """Eine gemeinsame, wiederverwendete CalDAV-Verbindung zu Nextcloud."""

    def __init__(
        self,
        url: str,
        benutzer: str,
        app_passwort: str,
        client_fabrik: Callable[..., Any] = caldav.DAVClient,
    ) -> None:
        self.dav_url = url.rstrip("/") + "/remote.php/dav"
        self._benutzer = benutzer
        self._app_passwort = app_passwort
        self._fabrik = client_fabrik
        self._client: Any = None
        self._sammlungen: dict[str, Any] = {}
        self._sperre = threading.RLock()

    def _verbinden(self) -> Any:
        if self._client is None:
            optionen: dict[str, Any] = {}
            if self.dav_url.startswith("http://"):
                log.warning("Nextcloud wird unverschlüsselt über HTTP angesprochen")
                optionen["require_tls"] = False
            self._client = self._fabrik(
                url=self.dav_url,
                username=self._benutzer,
                password=self._app_passwort,
                **optionen,
            )
        return self._client

    def zuruecksetzen(self) -> None:
        """Verwirft Verbindung und gefundene Kalender (z. B. nach einem Fehler)."""
        with self._sperre:
            self._client = None
            self._sammlungen.clear()

    @contextmanager
    def fehler_abfangen(self, aktion: str) -> Iterator[None]:
        """Übersetzt CalDAV- und Netzwerkfehler in verständliche Meldungen."""
        try:
            yield
        except WerkzeugFehler:
            raise
        except caldav_fehler.AuthorizationError as fehler:
            self.zuruecksetzen()
            raise NextcloudFehler(
                f"{aktion}: Nextcloud lehnt die Anmeldung ab (Benutzer oder App-Passwort prüfen)"
            ) from fehler
        except Exception as fehler:
            self.zuruecksetzen()
            log.exception("%s fehlgeschlagen", aktion)
            raise NextcloudFehler(
                f"{aktion}: Nextcloud ist gerade nicht erreichbar ({fehler})"
            ) from fehler

    def sammlung(self, anzeigename: str) -> Any:
        """Kalender oder Aufgabenliste mit diesem Anzeigenamen (ohne Groß/Klein)."""
        gesucht = anzeigename.strip().casefold()
        with self._sperre:
            if gesucht in self._sammlungen:
                return self._sammlungen[gesucht]
            with self.fehler_abfangen(f"Kalender „{anzeigename}“ suchen"):
                vorhanden = self._verbinden().principal().calendars()
                namen = []
                for kalender in vorhanden:
                    name = kalender.get_display_name() or ""
                    namen.append(name)
                    if name.strip().casefold() == gesucht:
                        self._sammlungen[gesucht] = kalender
                        return kalender
            raise NextcloudFehler(
                f"In Nextcloud gibt es keinen Kalender bzw. keine Liste „{anzeigename}“. "
                f"Vorhanden: {', '.join(n for n in namen if n) or 'keine'}"
            )


# ---------------------------------------------------------------------- Hilfen


def kurz_id(uid: str) -> str:
    """Kurze, stabile ID (8 Zeichen) aus der UID – leichter für das Modell."""
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:8]


def passt_zu_id(uid: str, gesucht: str) -> bool:
    gesucht = gesucht.strip()
    return bool(gesucht) and (uid == gesucht or kurz_id(uid) == gesucht.lower())


def zeitpunkt_lesen(text: str, zone: ZoneInfo, feld: str = "Zeitpunkt") -> date | datetime:
    """Liest ``2026-11-13`` (Datum) oder ``2026-11-13T14:00`` (Datum mit Uhrzeit).

    Uhrzeiten ohne Zeitzone gelten in ``zone``; andere werden umgerechnet.
    """
    roh = str(text).strip()
    try:
        if len(roh) == 10:
            return date.fromisoformat(roh)
        wert = datetime.fromisoformat(roh.replace(" ", "T", 1))
    except ValueError as fehler:
        raise WerkzeugFehler(
            f"{feld} „{text}“ ist kein gültiges Datum. Bitte ISO-Format wie "
            "2026-11-13 oder 2026-11-13T14:00 verwenden."
        ) from fehler
    if wert.tzinfo is None:
        return wert.replace(tzinfo=zone)
    return wert.astimezone(zone)


def in_zone(wert: date | datetime, zone: ZoneInfo) -> date | datetime:
    """Datumswerte bleiben, Uhrzeiten werden in ``zone`` umgerechnet (schwebende gelten dort)."""
    if isinstance(wert, datetime):
        return wert.replace(tzinfo=zone) if wert.tzinfo is None else wert.astimezone(zone)
    return wert


def als_zeitpunkt(wert: date | datetime, zone: ZoneInfo) -> datetime:
    """Für Vergleiche und Sortierung: Datum = Mitternacht in ``zone``."""
    if isinstance(wert, datetime):
        return in_zone(wert, zone)  # type: ignore[return-value]
    return datetime(wert.year, wert.month, wert.day, tzinfo=zone)


def datum_kurz(wert: date) -> str:
    """z. B. ``Fr 13.11.2026``."""
    return f"{WOCHENTAGE_KURZ[wert.weekday()]} {wert:%d.%m.%Y}"


def neuer_kalender() -> icalendar.Calendar:
    kalender = icalendar.Calendar()
    kalender.add("prodid", PRODID)
    kalender.add("version", "2.0")
    return kalender


def neue_uid() -> str:
    return str(uuid.uuid4()).upper()


def jetzt_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def als_ical_text(kalender: icalendar.Calendar) -> str:
    """Ergänzt fehlende VTIMEZONE-Blöcke und liefert den iCalendar-Text."""
    if hasattr(kalender, "add_missing_timezones"):
        kalender.add_missing_timezones()
    return kalender.to_ical().decode("utf-8")


def komponenten(objekt: Any, art: str) -> list[Any]:
    """Alle VEVENT/VTODO-Komponenten eines CalDAV-Objekts."""
    return list(objekt.icalendar_instance.walk(art))


def text_feld(komponente: Any, name: str) -> str:
    wert = komponente.get(name)
    return str(wert).strip() if wert is not None else ""


def ende_berechnen(komponente: Any, start: date | datetime) -> date | datetime:
    """DTEND, sonst DTSTART + DURATION, sonst Standarddauer."""
    if komponente.get("dtend") is not None:
        return komponente["dtend"].dt
    if komponente.get("duration") is not None:
        return start + komponente["duration"].dt
    return start + timedelta(days=1) if not isinstance(start, datetime) else start
