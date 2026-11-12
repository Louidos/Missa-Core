"""Werkzeuge, die Missa benutzen kann (Kalender, Aufgaben, Gedächtnis …)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.gedaechtnis import Gedaechtnis, gedaechtnis_werkzeuge

if TYPE_CHECKING:
    import httpx

    from missa.channels.nextcloud_talk import TalkClient
    from missa.core.alarme import Alarme
    from missa.core.hintergrund import Hintergrund
    from missa.core.rechte import Aktionsprotokoll, Rechte
    from missa.core.zeitplaner import Zeitplaner
    from missa.integrationen.ssh import SshVerbindung
    from missa.tools.anmeldungen import LoginWaechter
    from missa.tools.caddy import CaddyVerwaltung
    from missa.tools.container import ContainerDienst
    from missa.tools.dateien import Dateien
    from missa.tools.erreichbarkeit import ErreichbarkeitsWaechter
    from missa.tools.fehlersuche import Fehlersuche
    from missa.tools.firewall import FirewallLog
    from missa.tools.git import GitHubWaechter, GitUeberblick
    from missa.tools.homelab import HomelabInventar
    from missa.tools.kalender import Kalender
    from missa.tools.kontakte import Adressbuch, Kontaktpflege
    from missa.tools.mail import MailDienst
    from missa.tools.minecraft import MinecraftVerwaltung
    from missa.tools.nextcloud import NextcloudVerbindung
    from missa.tools.notizen import Notizen
    from missa.tools.pausen import PausenWaechter
    from missa.tools.prompts import ClaudePrompts
    from missa.tools.server_alarme import ServerWaechter
    from missa.tools.talk import TalkMelder
    from missa.tools.updates import UpdateVerwaltung
    from missa.tools.vms import VmVerwaltung
    from missa.tools.zertifikate import ZertifikatsWaechter

log = logging.getLogger(__name__)


@dataclass
class NextcloudDienste:
    """Gemeinsame Nextcloud-Objekte für Werkzeuge und Hintergrund-Aufträge."""

    verbindung: NextcloudVerbindung
    kalender: Kalender | None = None
    adressbuch: Adressbuch | None = None
    notizen: Notizen | None = None
    dateien: Dateien | None = None


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
    from missa.tools.dateien import Dateien
    from missa.tools.notizen import Notizen

    notizen = Notizen(
        einstellungen.nextcloud_url,
        einstellungen.nextcloud_user,
        einstellungen.nextcloud_app_password,
        einstellungen.zone,
        einstellungen.nextcloud_notizen_kategorie,
    )
    dateien = Dateien(
        einstellungen.nextcloud_url,
        einstellungen.nextcloud_user,
        einstellungen.nextcloud_app_password,
        einstellungen.zone,
        einstellungen.nextcloud_dateien_ordner or "Missa",
    )
    return NextcloudDienste(verbindung, kalender, adressbuch, notizen, dateien)


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
    alarme: Alarme | None = None,
    server: ServerWaechter | None = None,
    erreichbarkeit: ErreichbarkeitsWaechter | None = None,
    container: ContainerDienst | None = None,
    fehlersuche: Fehlersuche | None = None,
    anmeldungen: LoginWaechter | None = None,
    updates: UpdateVerwaltung | None = None,
    zertifikate: ZertifikatsWaechter | None = None,
    homelab: HomelabInventar | None = None,
    firewall: FirewallLog | None = None,
    vms: VmVerwaltung | None = None,
    caddy: CaddyVerwaltung | None = None,
    minecraft: MinecraftVerwaltung | None = None,
    git: GitUeberblick | None = None,
    github: GitHubWaechter | None = None,
    prompts: ClaudePrompts | None = None,
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

    if server is not None:
        from missa.tools.server_alarme import server_werkzeuge

        for werkzeug in server_werkzeuge(server):
            kasten.hinzufuegen(werkzeug)

    if erreichbarkeit is not None:
        from missa.tools.erreichbarkeit import erreichbarkeit_werkzeuge

        for werkzeug in erreichbarkeit_werkzeuge(erreichbarkeit):
            kasten.hinzufuegen(werkzeug)

    if container is not None:
        from missa.tools.container import container_werkzeuge

        for werkzeug in container_werkzeuge(container):
            kasten.hinzufuegen(werkzeug)

    if fehlersuche is not None:
        from missa.tools.fehlersuche import fehlersuche_werkzeuge

        for werkzeug in fehlersuche_werkzeuge(fehlersuche):
            kasten.hinzufuegen(werkzeug)

    if anmeldungen is not None:
        from missa.tools.anmeldungen import login_werkzeuge

        for werkzeug in login_werkzeuge(anmeldungen):
            kasten.hinzufuegen(werkzeug)

    if updates is not None:
        from missa.tools.updates import update_werkzeuge

        for werkzeug in update_werkzeuge(updates):
            kasten.hinzufuegen(werkzeug)

    if zertifikate is not None:
        from missa.tools.zertifikate import zertifikat_werkzeuge

        for werkzeug in zertifikat_werkzeuge(zertifikate):
            kasten.hinzufuegen(werkzeug)

    if homelab is not None:
        from missa.tools.homelab import homelab_werkzeuge

        for werkzeug in homelab_werkzeuge(homelab):
            kasten.hinzufuegen(werkzeug)

    if firewall is not None:
        from missa.tools.firewall import firewall_werkzeuge

        for werkzeug in firewall_werkzeuge(firewall):
            kasten.hinzufuegen(werkzeug)

    if vms is not None:
        from missa.tools.vms import vm_werkzeuge

        for werkzeug in vm_werkzeuge(vms):
            kasten.hinzufuegen(werkzeug)

    if caddy is not None:
        from missa.tools.caddy import caddy_werkzeuge

        for werkzeug in caddy_werkzeuge(caddy):
            kasten.hinzufuegen(werkzeug)

    if minecraft is not None:
        from missa.tools.minecraft import minecraft_werkzeuge

        for werkzeug in minecraft_werkzeuge(minecraft):
            kasten.hinzufuegen(werkzeug)

    if git is not None:
        from missa.tools.git import git_werkzeuge

        for werkzeug in git_werkzeuge(git):
            kasten.hinzufuegen(werkzeug)

    if github is not None:
        from missa.tools.git import github_werkzeuge

        for werkzeug in github_werkzeuge(github):
            kasten.hinzufuegen(werkzeug)

    if prompts is not None:
        from missa.tools.prompts import prompt_werkzeuge

        for werkzeug in prompt_werkzeuge(prompts):
            kasten.hinzufuegen(werkzeug)

    meldende = (
        vms,
        server,
        erreichbarkeit,
        container,
        anmeldungen,
        updates,
        zertifikate,
        homelab,
        firewall,
        git,
        github,
    )
    if alarme is not None and any(d is not None for d in meldende):
        from missa.core.alarme import alarm_werkzeuge

        for werkzeug in alarm_werkzeuge(alarme):
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
        if nextcloud.notizen is not None:
            from missa.tools.notizen import notizen_werkzeuge

            for werkzeug in notizen_werkzeuge(nextcloud.notizen):
                kasten.hinzufuegen(werkzeug)
        if nextcloud.dateien is not None:
            from missa.tools.dateien import dateien_werkzeuge

            for werkzeug in dateien_werkzeuge(nextcloud.dateien):
                kasten.hinzufuegen(werkzeug)
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
    from missa.core.vorgaben import Vorgaben
    from missa.tools.pausen import PausenWaechter

    waechter = PausenWaechter(
        kalender,
        einstellungen.pausen_regeln(),
        einstellungen.zone,
        zeitplaner,
        vorgaben=Vorgaben(einstellungen.datenbank),
    )
    kalender.nach_pruefungen.append(waechter.nach_termin)
    kalender.vor_pruefungen.append(waechter.vor_termin)
    return waechter


def alarme_bauen(einstellungen: Einstellungen, zeitplaner: Zeitplaner) -> Alarme:
    """Alarm-Stufen mit Ruhezeit und Ruhefenster (Standardwerte aus der Konfiguration)."""
    from missa.core.alarme import Alarme

    return Alarme(
        einstellungen.datenbank,
        einstellungen.zone,
        zeitplaner,
        ruhe_von=einstellungen.alarm_ruhezeit_von,
        ruhe_bis=einstellungen.alarm_ruhezeit_bis,
        ruhefenster_minuten=einstellungen.alarm_ruhefenster_minuten,
    )


def server_waechter_bauen(
    einstellungen: Einstellungen,
    alarme: Alarme,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ServerWaechter | None:
    """Server-Alarme mit allen konfigurierten Quellen – ohne Quelle ``None`` (aus)."""
    from missa.integrationen.monitoring import HomepageQuelle, JsonQuelle
    from missa.integrationen.proxmox import ProxmoxLeser
    from missa.tools.server_alarme import ServerWaechter

    quellen = []
    if einstellungen.monitoring_url:
        quellen.append(
            JsonQuelle(
                einstellungen.monitoring_url, einstellungen.monitoring_token, transport=transport
            )
        )
    if einstellungen.homepage_url:
        quellen.append(HomepageQuelle(einstellungen.homepage_url, transport=transport))
    if einstellungen.proxmox_konfiguriert:
        quellen.append(
            ProxmoxLeser(
                einstellungen.proxmox_url,
                einstellungen.proxmox_token_id,
                einstellungen.proxmox_token_secret,
                tls_pruefen=einstellungen.proxmox_tls_pruefen,
                ca_datei=einstellungen.proxmox_ca_datei,
                transport=transport,
            )
        )
    elif einstellungen.proxmox_url:
        log.warning(
            "Proxmox ist unvollständig konfiguriert (PROXMOX_URL, PROXMOX_TOKEN_ID und "
            "PROXMOX_TOKEN_SECRET nötig) – Proxmox-Werte sind aus"
        )
    if not quellen:
        return None
    return ServerWaechter(
        quellen,
        alarme,
        einstellungen.datenbank,
        einstellungen.zone,
        dauer_minuten=einstellungen.alarm_dauer_minuten,
        auffaellig_aktiv=einstellungen.auffaellig_aktiv,
        auffaellig_min_abstand=einstellungen.auffaellig_min_abstand,
    )


def erreichbarkeit_bauen(
    einstellungen: Einstellungen, alarme: Alarme
) -> ErreichbarkeitsWaechter | None:
    """Prüfung der eigenen Domains von außen – ohne ``ERREICHBARKEIT_DOMAINS`` aus."""
    if not einstellungen.erreichbarkeit_domains:
        return None
    from missa.core.alarme import AlarmStufe
    from missa.integrationen.erreichbarkeit import ErreichbarkeitsPruefer
    from missa.tools.erreichbarkeit import ErreichbarkeitsWaechter

    return ErreichbarkeitsWaechter(
        ErreichbarkeitsPruefer(einstellungen.erreichbarkeit_domains),
        alarme,
        einstellungen.datenbank,
        einstellungen.zone,
        stufe=AlarmStufe(einstellungen.erreichbarkeit_stufe),
    )


def ssh_verbindung_bauen(einstellungen: Einstellungen) -> SshVerbindung | None:
    """SSH zu den Hosts aus ``SSH_HOSTS`` (als ``missa``, nur erlaubte Befehle) – sonst aus."""
    if not einstellungen.ssh_konfiguriert:
        return None
    from missa.integrationen.ssh import SshVerbindung, hosts_lesen

    return SshVerbindung(
        hosts_lesen(einstellungen.ssh_hosts),
        benutzer=einstellungen.ssh_benutzer,
        schluessel=einstellungen.ssh_schluessel,
        known_hosts=einstellungen.ssh_known_hosts,
        timeout=einstellungen.ssh_timeout,
    )


def container_dienst_bauen(
    einstellungen: Einstellungen, ssh: SshVerbindung, alarme: Alarme
) -> ContainerDienst:
    """Container-Übersicht, Logs, Neustart nach Ja und Hänger-Erkennung."""
    from missa.tools.container import ContainerDienst

    return ContainerDienst(ssh, alarme, ignorieren=einstellungen.container_ignorieren)


def fehlersuche_bauen(
    einstellungen: Einstellungen,
    ssh: SshVerbindung,
    container: ContainerDienst | None,
    server: ServerWaechter | None = None,
    erreichbarkeit: ErreichbarkeitsWaechter | None = None,
) -> Fehlersuche:
    """Fehlersuche in Logs (Docker, journal, Nextcloud) mit Monitoring und Erreichbarkeit."""
    from missa.integrationen.anmeldungen import NextcloudLog
    from missa.tools.fehlersuche import Fehlersuche, dienste_lesen

    return Fehlersuche(
        ssh,
        container,
        dienste=dienste_lesen(einstellungen.fehlersuche_dienste),
        nextcloud_log=NextcloudLog.aus_text(
            einstellungen.nextcloud_log_host, einstellungen.nextcloud_log_quelle
        ),
        server=server,
        erreichbarkeit=erreichbarkeit,
        uhr=lambda: datetime.now(einstellungen.zone),
    )


def login_waechter_bauen(
    einstellungen: Einstellungen, ssh: SshVerbindung, alarme: Alarme
) -> LoginWaechter:
    """Login-Überwachung für SSH (alle bzw. ``LOGIN_HOSTS``) und Nextcloud."""
    from missa.integrationen.anmeldungen import NextcloudLog
    from missa.tools.anmeldungen import LoginWaechter

    return LoginWaechter(
        ssh,
        alarme,
        einstellungen.datenbank,
        einstellungen.zone,
        hosts=einstellungen.login_hosts,
        nextcloud_log=NextcloudLog.aus_text(
            einstellungen.nextcloud_log_host, einstellungen.nextcloud_log_quelle
        ),
        fehlversuche_grenze=einstellungen.login_fehlversuche_grenze,
        fenster_minuten=einstellungen.login_fehlversuche_fenster_minuten,
        automatisch=einstellungen.login_automatisch_benutzer,
        bekannte_ips=einstellungen.login_bekannte_ips,
        nacht_von=einstellungen.login_nacht_von,
        nacht_bis=einstellungen.login_nacht_bis,
    )


def update_verwaltung_bauen(
    einstellungen: Einstellungen,
    ssh: SshVerbindung,
    alarme: Alarme,
    kalender: Kalender | None = None,
    rechte: Rechte | None = None,
) -> UpdateVerwaltung:
    """Updates pro VM, Wartungsfenster im Kalender, Einspielen und Neustart nach Ja."""
    from missa.tools.updates import UpdateVerwaltung, wochentage_lesen

    return UpdateVerwaltung(
        ssh,
        alarme,
        einstellungen.datenbank,
        einstellungen.zone,
        hosts=einstellungen.update_hosts,
        eigener_host=einstellungen.updates_eigener_host,
        kalender=kalender,
        wartung_tage=wochentage_lesen(einstellungen.wartung_tage),
        wartung_von=einstellungen.wartung_von,
        wartung_bis=einstellungen.wartung_bis,
        wartung_dauer_minuten=einstellungen.wartung_dauer_minuten,
        rechte=rechte,
    )


def zertifikats_waechter_bauen(
    einstellungen: Einstellungen, alarme: Alarme
) -> ZertifikatsWaechter | None:
    """Zertifikate, Domain-Ablauf (RDAP) und DNS – ohne Domains und Erwartungen aus."""
    if not (einstellungen.zertifikat_domain_liste or einstellungen.dns_erwartet):
        return None
    from missa.integrationen.domains import RdapClient, erwartet_lesen
    from missa.tools.zertifikate import ZertifikatsWaechter

    return ZertifikatsWaechter(
        alarme,
        einstellungen.datenbank,
        zertifikat_domains=einstellungen.zertifikat_domain_liste,
        domain_namen=einstellungen.domain_namen_liste,
        dns_erwartet=erwartet_lesen(einstellungen.dns_erwartet),
        warn_tage=einstellungen.zertifikat_warnung_tage,
        rdap=RdapClient(einstellungen.rdap_bootstrap_url) if einstellungen.rdap_aktiv else None,
    )


def proxmox_leser_bauen(
    einstellungen: Einstellungen, transport: httpx.AsyncBaseTransport | None = None
) -> Any:
    """Nur lesender Proxmox-Zugang (Token mit PVEAuditor) – ohne Konfiguration ``None``."""
    if not einstellungen.proxmox_konfiguriert:
        return None
    from missa.integrationen.proxmox import ProxmoxLeser

    return ProxmoxLeser(
        einstellungen.proxmox_url,
        einstellungen.proxmox_token_id,
        einstellungen.proxmox_token_secret,
        tls_pruefen=einstellungen.proxmox_tls_pruefen,
        ca_datei=einstellungen.proxmox_ca_datei,
        transport=transport,
    )


def homelab_bauen(
    einstellungen: Einstellungen,
    alarme: Alarme,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HomelabInventar | None:
    """Homelab-Wissen aus Proxmox (nur lesend) – ohne Proxmox aus."""
    leser = proxmox_leser_bauen(einstellungen, transport)
    if leser is None:
        return None
    from missa.tools.homelab import HomelabInventar

    return HomelabInventar(leser, alarme, einstellungen.datenbank, einstellungen.zone)


def firewall_bauen(einstellungen: Einstellungen, alarme: Alarme) -> FirewallLog | None:
    """Firewall-Logs aus der rsyslog-Datei (nur lesend) – ohne FIREWALL_LOG_DATEI aus."""
    if einstellungen.firewall_log_datei is None:
        return None
    from missa.tools.firewall import FirewallLog

    return FirewallLog(
        einstellungen.firewall_log_datei,
        einstellungen.datenbank,
        einstellungen.zone,
        alarme,
        scan_ports=einstellungen.firewall_scan_ports,
        block_grenze=einstellungen.firewall_block_grenze,
    )


def vm_verwaltung_bauen(
    einstellungen: Einstellungen,
    alarme: Alarme,
    rechte: Rechte | None = None,
    homelab: HomelabInventar | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VmVerwaltung | None:
    """Eigene VMs im Pool missa anlegen und einrichten – ohne VM-Token aus."""
    if not einstellungen.vms_konfiguriert:
        return None
    import shutil

    from missa.integrationen.proxmox_vms import ProxmoxVerwalter, bereich_lesen
    from missa.tools.vms import AnsibleLaeufer, Grenzen, VmVerwaltung, netze_lesen

    proxmox = ProxmoxVerwalter(
        einstellungen.proxmox_url,
        einstellungen.proxmox_vm_token_id,
        einstellungen.proxmox_vm_token_secret,
        node=einstellungen.vm_node,
        pool=einstellungen.vm_pool,
        id_bereich=bereich_lesen(einstellungen.vm_id_bereich),
        tls_pruefen=einstellungen.proxmox_tls_pruefen,
        ca_datei=einstellungen.proxmox_ca_datei,
        transport=transport,
    )
    befehl = (
        einstellungen.ansible_playbook
        or shutil.which("ansible-playbook")
        or "/opt/missa/venv/bin/ansible-playbook"
    )
    oeffentlich = ""
    try:
        oeffentlich = (
            einstellungen.ssh_schluessel.with_suffix(".pub").read_text(encoding="utf-8").strip()
        )
    except OSError:
        log.warning("VMs: öffentlicher Schlüssel %s.pub fehlt", einstellungen.ssh_schluessel)
    return VmVerwaltung(
        proxmox,
        alarme,
        einstellungen.datenbank,
        einstellungen.zone,
        playbook_ordner=einstellungen.playbooks,
        template=einstellungen.vm_template_id,
        storage=einstellungen.vm_storage,
        netze=netze_lesen(einstellungen.vm_netze),
        grenzen=Grenzen(
            einstellungen.vm_max_cpu,
            einstellungen.vm_max_ram_gb,
            einstellungen.vm_max_disk_gb,
            einstellungen.vm_pool_max_cpu,
            einstellungen.vm_pool_max_ram_gb,
            einstellungen.vm_pool_max_disk_gb,
        ),
        bridge=einstellungen.vm_bridge,
        disk=einstellungen.vm_disk,
        ci_benutzer=einstellungen.vm_benutzer,
        ssh_schluessel_pub=oeffentlich,
        nameserver=einstellungen.vm_nameserver,
        verwaltung_ip=einstellungen.vm_verwaltung_ip,
        rcon_passwort=lambda: rcon_passwort(einstellungen),
        ansible=AnsibleLaeufer(befehl, einstellungen.ssh_schluessel),
        rechte=rechte,
        homelab=homelab,
    )


def rcon_passwort(einstellungen: Einstellungen, erzeugen: bool = True) -> str:
    """Aus MINECRAFT_RCON_PASSWORT, sonst aus ``MISSA_DATEN/minecraft-rcon.txt``.

    Fehlt beides, erzeugt Missa (beim Einrichten einer Minecraft-VM) einmal ein Passwort und
    legt es nur für missa lesbar ab.
    """
    if einstellungen.minecraft_rcon_passwort:
        return einstellungen.minecraft_rcon_passwort
    datei = einstellungen.daten / "minecraft-rcon.txt"
    try:
        return datei.read_text(encoding="utf-8").strip()
    except OSError:
        if not erzeugen:
            return ""
        import secrets

        neu = secrets.token_urlsafe(18)
        datei.parent.mkdir(parents=True, exist_ok=True)
        datei.write_text(neu + "\n", encoding="utf-8")
        datei.chmod(0o600)
        return neu


def caddy_verwaltung_bauen(
    einstellungen: Einstellungen, ssh: SshVerbindung | None
) -> CaddyVerwaltung | None:
    """Subdomains in Caddy auf rp01 – ohne CADDY_HOST, CADDY_DOMAIN oder SSH aus."""
    if not einstellungen.caddy_konfiguriert or ssh is None:
        return None
    from missa.integrationen.domains import dns_abfragen
    from missa.tools.caddy import CaddyVerwaltung

    return CaddyVerwaltung(
        ssh, einstellungen.caddy_host, einstellungen.caddy_domain, dns=dns_abfragen
    )


def minecraft_bauen(einstellungen: Einstellungen) -> MinecraftVerwaltung | None:
    """Minecraft per RCON – ohne MINECRAFT_RCON_HOST oder ohne Passwort aus."""
    if not einstellungen.minecraft_konfiguriert:
        return None
    passwort = rcon_passwort(einstellungen, erzeugen=False)
    if not passwort:
        log.warning(
            "MINECRAFT_RCON_HOST ist gesetzt, aber kein RCON-Passwort "
            "(MINECRAFT_RCON_PASSWORT oder minecraft-rcon.txt) – Minecraft-Werkzeuge sind aus"
        )
        return None
    from missa.integrationen.rcon import RconClient
    from missa.tools.minecraft import MinecraftVerwaltung

    return MinecraftVerwaltung(
        RconClient(einstellungen.minecraft_rcon_host, einstellungen.minecraft_rcon_port, passwort)
    )


def git_bauen(
    einstellungen: Einstellungen,
    alarme: Alarme | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GitUeberblick | None:
    """Eigener Git-Server (Gitea/Forgejo) – ohne GIT_SERVER_URL oder Token aus."""
    if not einstellungen.git_server_konfiguriert:
        return None
    from missa.integrationen.gitserver import GitServer
    from missa.tools.git import GitUeberblick

    server = GitServer(
        einstellungen.git_server_url,
        einstellungen.git_server_token,
        ca_datei=einstellungen.git_server_ca_datei,
        transport=transport,
    )
    return GitUeberblick(
        server,
        einstellungen.zone,
        alarme,
        besitzer=einstellungen.git_server_besitzer,
        alte_tage=einstellungen.git_alte_branches_tage,
    )


def github_bauen(
    einstellungen: Einstellungen,
    alarme: Alarme | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GitHubWaechter | None:
    """GitHub nur lesend – ohne GITHUB_TOKEN aus."""
    if not einstellungen.github_konfiguriert:
        return None
    from missa.integrationen.github import GitHubLeser
    from missa.tools.git import GitHubWaechter

    leser = GitHubLeser(
        einstellungen.github_token,
        einstellungen.github_konto,
        api_url=einstellungen.github_api_url,
        transport=transport,
    )
    return GitHubWaechter(
        leser, einstellungen.datenbank, einstellungen.zone, alarme, repos=einstellungen.github_repos
    )


def prompts_bauen(einstellungen: Einstellungen, git: GitUeberblick | None) -> ClaudePrompts | None:
    """Claude-Code-Prompts nach der Vorlage – ohne gültige Vorlage aus (mit Warnung)."""
    from missa.tools.prompts import ClaudePrompts, PromptVorlage

    try:
        vorlage = PromptVorlage.laden(einstellungen.prompt_vorlage_datei)
    except ValueError as fehler:
        log.warning("Claude-Code-Prompts sind aus: %s", fehler)
        return None
    return ClaudePrompts(vorlage, einstellungen.datenbank, einstellungen.zone, git)
