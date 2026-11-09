"""Werkzeuge, die Missa benutzen kann (Kalender, Aufgaben, Gedächtnis …)."""

from __future__ import annotations

import logging

from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten
from missa.tools.bestaetigung import Bestaetigungen

log = logging.getLogger(__name__)


def werkzeuge_laden(einstellungen: Einstellungen) -> Werkzeugkasten:
    """Stellt alle Werkzeuge zusammen, die mit dieser Konfiguration nutzbar sind.

    Nextcloud-Werkzeuge gibt es nur, wenn Adresse, Benutzer und App-Passwort
    gesetzt sind – sonst würde Missa Werkzeuge anbieten, die nie funktionieren.
    """
    kasten = Werkzeugkasten()
    bestaetigungen = Bestaetigungen()

    if einstellungen.nextcloud_konfiguriert:
        from missa.tools.aufgaben import aufgaben_werkzeuge
        from missa.tools.kalender import kalender_werkzeuge
        from missa.tools.nextcloud import NextcloudVerbindung

        verbindung = NextcloudVerbindung(
            einstellungen.nextcloud_url,
            einstellungen.nextcloud_user,
            einstellungen.nextcloud_app_password,
        )
        if einstellungen.nextcloud_kalender:
            for werkzeug in kalender_werkzeuge(
                verbindung, einstellungen.nextcloud_kalender, einstellungen.zone, bestaetigungen
            ):
                kasten.hinzufuegen(werkzeug)
        else:
            log.warning("NEXTCLOUD_KALENDER ist leer – Kalender-Werkzeuge sind aus")
        if einstellungen.nextcloud_aufgaben_liste:
            for werkzeug in aufgaben_werkzeuge(
                verbindung,
                einstellungen.nextcloud_aufgaben_liste,
                einstellungen.zone,
                bestaetigungen,
            ):
                kasten.hinzufuegen(werkzeug)
        else:
            log.warning("NEXTCLOUD_AUFGABEN_LISTE ist leer – Aufgaben-Werkzeuge sind aus")
    elif einstellungen.nextcloud_url or einstellungen.nextcloud_user:
        log.warning(
            "Nextcloud ist unvollständig konfiguriert (NEXTCLOUD_URL, NEXTCLOUD_USER und "
            "NEXTCLOUD_APP_PASSWORD nötig) – Nextcloud-Werkzeuge sind aus"
        )
    return kasten
