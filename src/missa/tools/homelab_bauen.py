"""Bauen der Homelab-Dienste (Alarme, Server, SSH, Updates, VMs, Caddy, Minecraft, Git …).

Aus ``missa/tools/__init__.py`` ausgelagert; alle Namen sind weiter über ``missa.tools``
importierbar.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from missa.core.config import Einstellungen

if TYPE_CHECKING:
    import httpx

    from missa.core.alarme import Alarme
    from missa.core.rechte import Rechte
    from missa.core.zeitplaner import Zeitplaner
    from missa.integrationen.ssh import SshVerbindung
    from missa.tools.anmeldungen import LoginWaechter
    from missa.tools.caddy import CaddyVerwaltung
    from missa.tools.container import ContainerDienst
    from missa.tools.erreichbarkeit import ErreichbarkeitsWaechter
    from missa.tools.fehlersuche import Fehlersuche
    from missa.tools.firewall import FirewallLog
    from missa.tools.git import GitHubWaechter, GitUeberblick
    from missa.tools.homelab import HomelabInventar
    from missa.tools.kalender import Kalender
    from missa.tools.minecraft import MinecraftVerwaltung
    from missa.tools.prompts import ClaudePrompts
    from missa.tools.server_alarme import ServerWaechter
    from missa.tools.updates import UpdateVerwaltung
    from missa.tools.vms import VmVerwaltung
    from missa.tools.zertifikate import ZertifikatsWaechter

log = logging.getLogger(__name__)


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


def erreichbarkeit_bauen(einstellungen: Einstellungen, alarme: Alarme) -> ErreichbarkeitsWaechter:
    """Prüfung der eigenen Domains von außen – die Ziele stehen in ``erreichbarkeit_pruefungen``.

    ``ERREICHBARKEIT_DOMAINS`` ist nur der Startwert beim allerersten Start;
    ``ERREICHBARKEIT_INTERVALL_MINUTEN`` und ``ERREICHBARKEIT_STUFE`` gelten für neue Prüfungen.
    """
    from missa.core.alarme import AlarmStufe
    from missa.integrationen.erreichbarkeit import ErreichbarkeitsPruefer
    from missa.tools.erreichbarkeit import ErreichbarkeitsWaechter

    return ErreichbarkeitsWaechter(
        ErreichbarkeitsPruefer(),
        alarme,
        einstellungen.datenbank,
        einstellungen.zone,
        stufe=AlarmStufe(einstellungen.erreichbarkeit_stufe),
        intervall_minuten=einstellungen.erreichbarkeit_intervall_minuten,
        startwert=einstellungen.erreichbarkeit_ziele,
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
    einstellungen: Einstellungen,
    alarme: Alarme,
    erreichbarkeit: ErreichbarkeitsWaechter | None = None,
) -> ZertifikatsWaechter | None:
    """Zertifikate, Domain-Ablauf (RDAP) und DNS – ohne Domains und Erwartungen aus.

    Ohne ``ZERTIFIKAT_DOMAINS`` nimmt Missa die Ziele der aktiven Erreichbarkeits-Prüfungen
    (bei jeder Prüfung neu, also auch nach einer Änderung im Chat).
    """
    quelle = erreichbarkeit.zertifikat_ziele if erreichbarkeit is not None else None
    if not (einstellungen.zertifikat_domains or quelle or einstellungen.dns_erwartet):
        return None
    from missa.integrationen.domains import RdapClient, erwartet_lesen
    from missa.tools.zertifikate import ZertifikatsWaechter

    return ZertifikatsWaechter(
        alarme,
        einstellungen.datenbank,
        zertifikat_domains=einstellungen.zertifikat_domains,
        zertifikat_quelle=quelle,
        domain_namen=einstellungen.domain_namen or None,
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
        # Gleich mit 0600 anlegen – nie kurz für alle lesbar.
        fd = os.open(datei, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as aus:
            aus.write(neu + "\n")
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
