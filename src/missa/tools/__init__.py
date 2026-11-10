"""Werkzeuge, die Missa benutzen kann (Kalender, Aufgaben, Gedächtnis …)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.gedaechtnis import Gedaechtnis, gedaechtnis_werkzeuge

if TYPE_CHECKING:
    from missa.channels.nextcloud_talk import TalkClient
    from missa.core.zeitplaner import Zeitplaner
    from missa.tools.mail import MailDienst
    from missa.tools.talk import TalkMelder

log = logging.getLogger(__name__)


def werkzeuge_laden(
    einstellungen: Einstellungen,
    gedaechtnis: Gedaechtnis | None = None,
    talk_client: TalkClient | None = None,
    *,
    zeitplaner: Zeitplaner | None = None,
    talk_melder: TalkMelder | None = None,
    mail_dienst: MailDienst | None = None,
) -> Werkzeugkasten:
    """Stellt alle Werkzeuge zusammen, die mit dieser Konfiguration nutzbar sind.

    Die Gedächtnis-Werkzeuge kommen dazu, wenn ``gedaechtnis`` übergeben wird,
    die Erinnerungs-Werkzeuge mit ``zeitplaner``, das Talk-Werkzeug, wenn
    Nextcloud Talk aktiv ist (``talk_client`` oder ein fertiger ``talk_melder``).
    Nextcloud-Werkzeuge gibt es nur, wenn Adresse, Benutzer und App-Passwort
    gesetzt sind – sonst würde Missa Werkzeuge anbieten, die nie funktionieren.
    """
    kasten = Werkzeugkasten()
    bestaetigungen = Bestaetigungen()

    if gedaechtnis is not None:
        for werkzeug in gedaechtnis_werkzeuge(gedaechtnis, bestaetigungen):
            kasten.hinzufuegen(werkzeug)

    if zeitplaner is not None:
        from missa.tools.erinnerungen import erinnerungs_werkzeuge

        for werkzeug in erinnerungs_werkzeuge(zeitplaner):
            kasten.hinzufuegen(werkzeug)

    if mail_dienst is not None:
        from missa.tools.mail import mail_werkzeuge

        for werkzeug in mail_werkzeuge(mail_dienst):
            kasten.hinzufuegen(werkzeug)

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
            from missa.tools.farben import Farbsystem
            from missa.tools.kalender_protokoll import KalenderProtokoll

            for werkzeug in kalender_werkzeuge(
                verbindung,
                einstellungen.nextcloud_kalender,
                einstellungen.zone,
                bestaetigungen,
                KalenderProtokoll(einstellungen.datenbank, einstellungen.zone),
                Farbsystem(einstellungen.datenbank),
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

    if talk_melder is None and talk_client is not None:
        talk_melder = talk_melder_bauen(einstellungen, talk_client)
    if talk_melder is not None:
        from missa.tools.talk import talk_werkzeuge

        for werkzeug in talk_werkzeuge(talk_melder):
            kasten.hinzufuegen(werkzeug)
        if not einstellungen.nc_talk_notify_conversation:
            log.info(
                "NC_TALK_NOTIFY_CONVERSATION ist leer – Missa schreibt nicht von sich aus in Talk"
            )
    return kasten


def talk_melder_bauen(einstellungen: Einstellungen, talk_client: TalkClient) -> TalkMelder:
    """Melder für Nachrichten, die Missa von sich aus in Talk schreibt."""
    from missa.tools.talk import Ratenbegrenzer, TalkMelder

    return TalkMelder(
        talk_client,
        einstellungen.nc_talk_notify_conversation,
        Ratenbegrenzer(einstellungen.nc_talk_notify_rate),
    )


def mail_dienst_bauen(einstellungen: Einstellungen, gedaechtnis: Gedaechtnis | None) -> MailDienst:
    """E-Mail-Entwürfe mit Senden-Sperre für das eigene Mailkonto (IMAP/SMTP)."""
    from missa.tools.mail import MailDienst, gedaechtnis_adressen
    from missa.tools.mail_konto import MailKonto
    from missa.tools.schreibstil import Schreibstil

    konto = MailKonto(
        einstellungen.mail_imap_host,
        einstellungen.mail_imap_port,
        einstellungen.mail_smtp_host,
        einstellungen.mail_smtp_port,
        einstellungen.mail_user,
        einstellungen.mail_password,
        einstellungen.mail_absender,
        einstellungen.mail_ordner_entwuerfe,
        einstellungen.mail_ordner_gesendet,
    )
    return MailDienst(
        konto,
        einstellungen.datenbank,
        Schreibstil(einstellungen.stil_datei),
        einstellungen.zone,
        adressquellen=[gedaechtnis_adressen(gedaechtnis)] if gedaechtnis is not None else [],
    )
