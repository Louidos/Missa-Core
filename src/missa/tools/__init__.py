"""Werkzeuge, die Missa benutzen kann (Kalender, Aufgaben, Gedächtnis …)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.gedaechtnis import Gedaechtnis, gedaechtnis_werkzeuge

if TYPE_CHECKING:
    from missa.channels.nextcloud_talk import TalkClient
    from missa.core.hintergrund import Hintergrund
    from missa.core.rechte import Aktionsprotokoll, Rechte
    from missa.core.zeitplaner import Zeitplaner
    from missa.tools.kalender import Kalender
    from missa.tools.kontakte import Adressbuch, Kontaktpflege
    from missa.tools.mail import MailDienst
    from missa.tools.nextcloud import NextcloudVerbindung
    from missa.tools.pausen import PausenWaechter
    from missa.tools.talk import TalkMelder

log = logging.getLogger(__name__)


@dataclass
class NextcloudDienste:
    """Gemeinsame Nextcloud-Objekte für Werkzeuge und Hintergrund-Aufträge."""

    verbindung: NextcloudVerbindung
    kalender: Kalender | None = None
    adressbuch: Adressbuch | None = None


def nextcloud_dienste_bauen(einstellungen: Einstellungen) -> NextcloudDienste:
    """Verbindung, Standardkalender (mit Protokoll und Farbsystem) und Adressbuch."""
    from missa.tools.farben import Farbsystem
    from missa.tools.kalender import Kalender
    from missa.tools.kalender_protokoll import KalenderProtokoll
    from missa.tools.kontakte import Adressbuch
    from missa.tools.nextcloud import NextcloudVerbindung

    verbindung = NextcloudVerbindung(
        einstellungen.nextcloud_url,
        einstellungen.nextcloud_user,
        einstellungen.nextcloud_app_password,
    )
    kalender = None
    if einstellungen.nextcloud_kalender:
        kalender = Kalender(
            verbindung,
            einstellungen.nextcloud_kalender,
            einstellungen.zone,
            Bestaetigungen(),
            KalenderProtokoll(einstellungen.datenbank, einstellungen.zone),
            Farbsystem(einstellungen.datenbank),
        )
    adressbuch = Adressbuch(
        einstellungen.nextcloud_url,
        einstellungen.nextcloud_user,
        einstellungen.nextcloud_app_password,
        einstellungen.nextcloud_adressbuch,
    )
    return NextcloudDienste(verbindung, kalender, adressbuch)


def werkzeuge_laden(
    einstellungen: Einstellungen,
    gedaechtnis: Gedaechtnis | None = None,
    talk_client: TalkClient | None = None,
    *,
    zeitplaner: Zeitplaner | None = None,
    talk_melder: TalkMelder | None = None,
    mail_dienst: MailDienst | None = None,
    nextcloud: NextcloudDienste | None = None,
    kontaktpflege: Kontaktpflege | None = None,
    pausen: PausenWaechter | None = None,
    hintergrund: Hintergrund | None = None,
    rechte: Rechte | None = None,
    protokoll: Aktionsprotokoll | None = None,
) -> Werkzeugkasten:
    """Stellt alle Werkzeuge zusammen, die mit dieser Konfiguration nutzbar sind.

    Die Gedächtnis-Werkzeuge kommen dazu, wenn ``gedaechtnis`` übergeben wird,
    die Erinnerungs-Werkzeuge mit ``zeitplaner``, Routinen und Nachtschicht mit
    ``zeitplaner`` und ``hintergrund``, das Talk-Werkzeug, wenn
    Nextcloud Talk aktiv ist (``talk_client`` oder ein fertiger ``talk_melder``).
    Nextcloud-Werkzeuge gibt es nur, wenn Adresse, Benutzer und App-Passwort
    gesetzt sind – sonst würde Missa Werkzeuge anbieten, die nie funktionieren.
    """
    kasten = Werkzeugkasten(rechte=rechte)
    bestaetigungen = Bestaetigungen()

    if protokoll is not None:
        from missa.core.rechte import protokoll_werkzeuge

        for werkzeug in protokoll_werkzeuge(protokoll, kasten.rechte, einstellungen.zone):
            kasten.hinzufuegen(werkzeug)

    if gedaechtnis is not None:
        for werkzeug in gedaechtnis_werkzeuge(gedaechtnis, bestaetigungen):
            kasten.hinzufuegen(werkzeug)

    if zeitplaner is not None:
        from missa.tools.erinnerungen import erinnerungs_werkzeuge

        for werkzeug in erinnerungs_werkzeuge(zeitplaner, bestaetigungen):
            kasten.hinzufuegen(werkzeug)

    if zeitplaner is not None and hintergrund is not None:
        from missa.tools.nachtschicht import nachtschicht_werkzeuge
        from missa.tools.routinen import routinen_werkzeuge

        for werkzeug in routinen_werkzeuge(zeitplaner, hintergrund, bestaetigungen):
            kasten.hinzufuegen(werkzeug)
        for werkzeug in nachtschicht_werkzeuge(hintergrund, bestaetigungen):
            kasten.hinzufuegen(werkzeug)

    if pausen is not None:
        from missa.tools.pausen import pausen_werkzeuge

        for werkzeug in pausen_werkzeuge(pausen):
            kasten.hinzufuegen(werkzeug)

    if kontaktpflege is not None:
        from missa.tools.kontakte import kontakt_werkzeuge

        for werkzeug in kontakt_werkzeuge(kontaktpflege):
            kasten.hinzufuegen(werkzeug)

    if mail_dienst is not None:
        from missa.tools.mail import mail_werkzeuge

        for werkzeug in mail_werkzeuge(mail_dienst):
            kasten.hinzufuegen(werkzeug)

    if einstellungen.nextcloud_konfiguriert:
        from missa.tools.aufgaben import aufgaben_werkzeuge
        from missa.tools.kalender import kalender_werkzeuge

        if nextcloud is None:
            nextcloud = nextcloud_dienste_bauen(einstellungen)
        verbindung = nextcloud.verbindung
        if nextcloud.kalender is not None:
            for werkzeug in kalender_werkzeuge(
                verbindung,
                einstellungen.nextcloud_kalender,
                einstellungen.zone,
                bestaetigungen,
                kalender=nextcloud.kalender,
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


def kontaktpflege_bauen(
    einstellungen: Einstellungen, zeitplaner: Zeitplaner, nextcloud: NextcloudDienste
) -> Kontaktpflege:
    """Geburtstage und Kontaktpflege (braucht Nextcloud-Kontakte und den Zeitplaner)."""
    from missa.tools.kontakte import Kontaktpflege

    return Kontaktpflege(
        einstellungen.datenbank,
        einstellungen.zone,
        zeitplaner,
        adressbuch=nextcloud.adressbuch,
        kalender=nextcloud.kalender,
        vorlauf_tage=einstellungen.kontakte_geburtstag_vorlauf,
        pruefzeit=einstellungen.kontakte_pruefzeit,
    )


def pausen_waechter_bauen(
    einstellungen: Einstellungen, zeitplaner: Zeitplaner, kalender: Kalender
) -> PausenWaechter:
    """Achtet auf Pausen und Puffer im Kalender (Haken und Abend-Prüfung)."""
    from missa.tools.pausen import PausenWaechter

    waechter = PausenWaechter(
        kalender, einstellungen.pausen_regeln(), einstellungen.zone, zeitplaner
    )
    kalender.nach_pruefungen.append(waechter.nach_termin)
    return waechter
