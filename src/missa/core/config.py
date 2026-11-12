"""Konfiguration von Missa-Core aus Umgebungsvariablen.

Im Betrieb setzt systemd die Variablen aus ``/etc/missa/missa.env``
(``EnvironmentFile=``). Beim Start von Hand liest :func:`env_datei_laden`
dieselbe Datei; bereits gesetzte Variablen haben Vorrang.
"""

from __future__ import annotations

import contextlib
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
    bestaetigung_minuten: int = 10
    monitoring_url: str = ""
    monitoring_token: str = ""
    homepage_url: str = ""
    proxmox_url: str = ""
    proxmox_token_id: str = ""
    proxmox_token_secret: str = ""
    proxmox_tls_pruefen: bool = True
    proxmox_ca_datei: Path | None = None
    monitoring_intervall_sekunden: int = 60
    alarm_dauer_minuten: float = 5.0
    alarm_ruhefenster_minuten: int = 60
    alarm_ruhezeit_von: time | None = time(22, 0)
    alarm_ruhezeit_bis: time | None = time(7, 0)
    auffaellig_aktiv: bool = True
    auffaellig_min_abstand: float = 15.0
    erreichbarkeit_domains: tuple[str, ...] = ()
    erreichbarkeit_intervall_minuten: int = 5
    erreichbarkeit_stufe: str = "wichtig"
    ssh_hosts: str = ""
    ssh_benutzer: str = "missa"
    ssh_schluessel: Path = Path("/etc/missa/ssh/id_ed25519")
    ssh_known_hosts: Path = Path("/etc/missa/ssh/known_hosts")
    ssh_timeout: float = 20.0
    container_intervall_minuten: int = 5
    container_ignorieren: tuple[str, ...] = ()
    login_hosts: tuple[str, ...] = ()
    login_intervall_minuten: int = 5
    login_fehlversuche_grenze: int = 10
    login_fehlversuche_fenster_minuten: int = 15
    login_automatisch_benutzer: tuple[str, ...] = ("missa",)
    login_bekannte_ips: tuple[str, ...] = ()
    login_nacht_von: time = time(0, 0)
    login_nacht_bis: time = time(5, 0)
    nextcloud_log_host: str = ""
    nextcloud_log_quelle: str = ""
    fehlersuche_dienste: str = ""
    update_hosts: tuple[str, ...] = ()
    updates_pruefzeit: time = time(6, 40)
    updates_eigener_host: str = "ai01"
    wartung_tage: str = "Sa,So"
    wartung_von: time = time(10, 0)
    wartung_bis: time = time(17, 0)
    wartung_dauer_minuten: int = 60
    zertifikat_domains: tuple[str, ...] = ()
    domain_namen: tuple[str, ...] = ()
    dns_erwartet: str = ""
    zertifikat_warnung_tage: tuple[int, ...] = (21, 7)
    zertifikate_pruefzeit: time = time(7, 10)
    rdap_aktiv: bool = True
    rdap_bootstrap_url: str = "https://data.iana.org/rdap/dns.json"
    homelab_intervall_minuten: int = 60
    firewall_log_datei: Path | None = None
    firewall_bericht_zeit: time = time(7, 30)
    firewall_scan_ports: int = 15
    firewall_block_grenze: int = 100
    proxmox_vm_token_id: str = ""
    proxmox_vm_token_secret: str = ""
    vm_node: str = ""
    vm_template_id: int = 0
    vm_storage: str = "local-lvm"
    vm_pool: str = "missa"
    vm_id_bereich: str = "200-249"
    vm_bridge: str = "vmbr0"
    vm_disk: str = "scsi0"
    vm_netze: str = ""
    vm_max_cpu: int = 4
    vm_max_ram_gb: int = 8
    vm_max_disk_gb: int = 64
    vm_pool_max_cpu: int = 8
    vm_pool_max_ram_gb: int = 16
    vm_pool_max_disk_gb: int = 200
    vm_benutzer: str = "missa"
    vm_nameserver: str = ""
    vm_verwaltung_ip: str = ""
    ansible_playbook: str = ""
    playbook_ordner: Path | None = None
    minecraft_rcon_passwort: str = ""
    caddy_host: str = ""
    caddy_domain: str = ""
    minecraft_rcon_host: str = ""
    minecraft_rcon_port: int = 25575
    git_server_url: str = ""
    git_server_token: str = ""
    git_server_besitzer: str = ""
    git_server_ca_datei: Path | None = None
    git_alte_branches_tage: int = 14
    github_token: str = ""
    github_konto: str = ""
    github_repos: tuple[str, ...] = ()
    github_api_url: str = "https://api.github.com"
    github_intervall_minuten: int = 30
    prompt_vorlage: Path | None = None

    @property
    def notfall_datei(self) -> Path:
        """Solange diese Datei existiert, ist der Notfall-Stopp aktiv."""
        return self.daten / "notfall-stopp"

    @property
    def geheimnisse(self) -> tuple[str, ...]:
        """Zugangsdaten, die nie ins Aktionsprotokoll dürfen (werden geschwärzt)."""
        werte = [
            self.api_key,
            self.nextcloud_app_password,
            self.nc_talk_bot_secret,
            self.mail_password,
            self.monitoring_token,
            self.proxmox_token_secret,
            self.proxmox_vm_token_secret,
            self.minecraft_rcon_passwort,
            self.git_server_token,
            self.github_token,
        ]
        with contextlib.suppress(OSError):
            werte.append(self.llama_api_key_file.read_text(encoding="utf-8").strip())
        return tuple(w for w in werte if w)

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
    def proxmox_konfiguriert(self) -> bool:
        """``True``, wenn Adresse, Token-ID und Token-Secret für Proxmox gesetzt sind."""
        return bool(self.proxmox_url and self.proxmox_token_id and self.proxmox_token_secret)

    @property
    def server_monitoring_konfiguriert(self) -> bool:
        """``True``, wenn mindestens eine Monitoring-Quelle eingerichtet ist."""
        return bool(self.monitoring_url or self.homepage_url or self.proxmox_konfiguriert)

    @property
    def ssh_konfiguriert(self) -> bool:
        """``True``, wenn mindestens ein Host für SSH eingetragen ist (``SSH_HOSTS``)."""
        return bool(self.ssh_hosts.strip())

    @property
    def zertifikat_domain_liste(self) -> tuple[str, ...]:
        """Domains für die Zertifikatsprüfung (Standard: die der Erreichbarkeit)."""
        return self.zertifikat_domains or self.erreichbarkeit_domains

    @property
    def domain_namen_liste(self) -> tuple[str, ...]:
        """Registrierte Domains für RDAP (Standard: aus den Zertifikats-Domains abgeleitet)."""
        if self.domain_namen:
            return self.domain_namen
        from missa.integrationen.domains import registrierte_domain

        return tuple(dict.fromkeys(registrierte_domain(d) for d in self.zertifikat_domain_liste))

    @property
    def git_server_konfiguriert(self) -> bool:
        """``True``, wenn Adresse und Token des eigenen Git-Servers gesetzt sind."""
        return bool(self.git_server_url and self.git_server_token)

    @property
    def github_konfiguriert(self) -> bool:
        """``True``, wenn ein (nur lesendes) GitHub-Token gesetzt ist."""
        return bool(self.github_token)

    @property
    def caddy_konfiguriert(self) -> bool:
        """``True``, wenn Missa Subdomains in Caddy vorbereiten darf (Host und Domain)."""
        return bool(self.caddy_host and self.caddy_domain)

    @property
    def minecraft_konfiguriert(self) -> bool:
        """``True``, wenn ein Minecraft-Server per RCON eingetragen ist."""
        return bool(self.minecraft_rcon_host)

    @property
    def vms_konfiguriert(self) -> bool:
        """``True``, wenn Missa eigene VMs anlegen darf (VM-Token, Host, Template und Netze)."""
        return bool(
            self.proxmox_url
            and self.proxmox_vm_token_id
            and self.proxmox_vm_token_secret
            and self.vm_node
            and self.vm_template_id
            and self.vm_netze
        )

    @property
    def playbooks(self) -> Path:
        """Ordner mit den Playbooks (Standard: playbooks/ im Code von Missa-Core)."""
        if self.playbook_ordner is not None:
            return self.playbook_ordner
        for kandidat in (Path("/opt/missa/app/playbooks"), Path(__file__).parents[3] / "playbooks"):
            if kandidat.is_dir():
                return kandidat
        return Path("/opt/missa/app/playbooks")

    @property
    def prompt_vorlage_datei(self) -> Path:
        """Vorlage für Claude-Code-Prompts (Standard: config/prompts/claude-code.md)."""
        if self.prompt_vorlage is not None:
            return self.prompt_vorlage
        for kandidat in (
            Path("/opt/missa/app/config/prompts/claude-code.md"),
            Path(__file__).parents[3] / "config" / "prompts" / "claude-code.md",
        ):
            if kandidat.is_file():
                return kandidat
        return Path("/opt/missa/app/config/prompts/claude-code.md")

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

        def uhrzeit_oder_aus(name: str, standard: str) -> time | None:
            if wert(name, standard).casefold() in ("aus", "nein", "keine", "-"):
                return None
            return uhrzeit(name, standard)

        def liste(name: str) -> tuple[str, ...]:
            return tuple(
                dict.fromkeys(
                    teil.strip().casefold()
                    for teil in env.get(name, "").replace(";", ",").split(",")
                    if teil.strip()
                )
            )

        ca_datei = env.get("PROXMOX_CA_DATEI", "").strip()
        fw_datei = env.get("FIREWALL_LOG_DATEI", "").strip()
        pb_ordner = env.get("PLAYBOOK_ORDNER", "").strip()
        git_ca = env.get("GIT_SERVER_CA_DATEI", "").strip()
        vorlage = env.get("PROMPT_VORLAGE", "").strip()
        try:
            warn_tage = tuple(
                sorted({int(t) for t in liste("ZERTIFIKAT_WARNUNG_TAGE")}, reverse=True)
            ) or (21, 7)
        except ValueError as fehler:
            raise KonfigurationsFehler(
                "ZERTIFIKAT_WARNUNG_TAGE muss Zahlen enthalten, z. B. 21,7"
            ) from fehler

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
            bestaetigung_minuten=max(1, zahl("MISSA_BESTAETIGUNG_MINUTEN", 10)),
            monitoring_url=env.get("MONITORING_URL", "").strip(),
            monitoring_token=env.get("MONITORING_TOKEN", "").strip(),
            homepage_url=env.get("HOMEPAGE_URL", "").strip().rstrip("/"),
            proxmox_url=env.get("PROXMOX_URL", "").strip().rstrip("/"),
            proxmox_token_id=env.get("PROXMOX_TOKEN_ID", "").strip(),
            proxmox_token_secret=env.get("PROXMOX_TOKEN_SECRET", "").strip(),
            proxmox_tls_pruefen=wert("PROXMOX_TLS_PRUEFEN", "true").casefold() in WAHR,
            proxmox_ca_datei=Path(ca_datei) if ca_datei else None,
            monitoring_intervall_sekunden=max(10, zahl("MONITORING_INTERVALL_SEKUNDEN", 60)),
            alarm_dauer_minuten=max(0.0, kommazahl("ALARM_DAUER_MINUTEN", 5.0)),
            alarm_ruhefenster_minuten=max(1, zahl("ALARM_RUHEFENSTER_MINUTEN", 60)),
            alarm_ruhezeit_von=uhrzeit_oder_aus("ALARM_RUHEZEIT_VON", "22:00"),
            alarm_ruhezeit_bis=uhrzeit_oder_aus("ALARM_RUHEZEIT_BIS", "07:00"),
            auffaellig_aktiv=wert("AUFFAELLIG_AKTIV", "true").casefold() in WAHR,
            auffaellig_min_abstand=max(1.0, kommazahl("AUFFAELLIG_MIN_ABSTAND", 15.0)),
            erreichbarkeit_domains=liste("ERREICHBARKEIT_DOMAINS"),
            erreichbarkeit_intervall_minuten=max(1, zahl("ERREICHBARKEIT_INTERVALL_MINUTEN", 5)),
            erreichbarkeit_stufe=wert("ERREICHBARKEIT_STUFE", "wichtig").casefold(),
            ssh_hosts=env.get("SSH_HOSTS", "").strip(),
            ssh_benutzer=wert("SSH_BENUTZER", cls.ssh_benutzer),
            ssh_schluessel=Path(wert("SSH_SCHLUESSEL", str(cls.ssh_schluessel))),
            ssh_known_hosts=Path(wert("SSH_KNOWN_HOSTS", str(cls.ssh_known_hosts))),
            ssh_timeout=max(1.0, kommazahl("SSH_TIMEOUT", 20.0)),
            container_intervall_minuten=max(1, zahl("CONTAINER_INTERVALL_MINUTEN", 5)),
            container_ignorieren=liste("CONTAINER_IGNORIEREN"),
            login_hosts=liste("LOGIN_HOSTS"),
            login_intervall_minuten=max(1, zahl("LOGIN_INTERVALL_MINUTEN", 5)),
            login_fehlversuche_grenze=max(1, zahl("LOGIN_FEHLVERSUCHE_GRENZE", 10)),
            login_fehlversuche_fenster_minuten=max(
                1, zahl("LOGIN_FEHLVERSUCHE_FENSTER_MINUTEN", 15)
            ),
            login_automatisch_benutzer=liste("LOGIN_AUTOMATISCH_BENUTZER") or ("missa",),
            login_bekannte_ips=liste("LOGIN_BEKANNTE_IPS"),
            login_nacht_von=uhrzeit("LOGIN_NACHT_VON", "00:00"),
            login_nacht_bis=uhrzeit("LOGIN_NACHT_BIS", "05:00"),
            nextcloud_log_host=env.get("NEXTCLOUD_LOG_HOST", "").strip().casefold(),
            nextcloud_log_quelle=env.get("NEXTCLOUD_LOG_QUELLE", "").strip(),
            fehlersuche_dienste=env.get("FEHLERSUCHE_DIENSTE", "").strip(),
            update_hosts=liste("UPDATE_HOSTS"),
            updates_pruefzeit=uhrzeit("UPDATES_PRUEFZEIT", "06:40"),
            updates_eigener_host=wert("UPDATES_EIGENER_HOST", "ai01").casefold(),
            wartung_tage=wert("WARTUNG_TAGE", "Sa,So"),
            wartung_von=uhrzeit("WARTUNG_VON", "10:00"),
            wartung_bis=uhrzeit("WARTUNG_BIS", "17:00"),
            wartung_dauer_minuten=max(15, zahl("WARTUNG_DAUER_MINUTEN", 60)),
            zertifikat_domains=liste("ZERTIFIKAT_DOMAINS"),
            domain_namen=liste("DOMAIN_NAMEN"),
            dns_erwartet=env.get("DNS_ERWARTET", "").strip(),
            zertifikat_warnung_tage=warn_tage,
            zertifikate_pruefzeit=uhrzeit("ZERTIFIKATE_PRUEFZEIT", "07:10"),
            rdap_aktiv=wert("RDAP_AKTIV", "true").casefold() in WAHR,
            rdap_bootstrap_url=wert("RDAP_BOOTSTRAP_URL", cls.rdap_bootstrap_url),
            homelab_intervall_minuten=max(5, zahl("HOMELAB_INTERVALL_MINUTEN", 60)),
            firewall_log_datei=Path(fw_datei) if fw_datei else None,
            firewall_bericht_zeit=uhrzeit("FIREWALL_BERICHT_ZEIT", "07:30"),
            firewall_scan_ports=max(3, zahl("FIREWALL_SCAN_PORTS", 15)),
            firewall_block_grenze=max(1, zahl("FIREWALL_BLOCK_GRENZE", 100)),
            proxmox_vm_token_id=env.get("PROXMOX_VM_TOKEN_ID", "").strip(),
            proxmox_vm_token_secret=env.get("PROXMOX_VM_TOKEN_SECRET", "").strip(),
            vm_node=env.get("VM_NODE", "").strip(),
            vm_template_id=max(0, zahl("VM_TEMPLATE_ID", 0)),
            vm_storage=wert("VM_STORAGE", cls.vm_storage),
            vm_pool=wert("VM_POOL", cls.vm_pool),
            vm_id_bereich=wert("VM_ID_BEREICH", cls.vm_id_bereich),
            vm_bridge=wert("VM_BRIDGE", cls.vm_bridge),
            vm_disk=wert("VM_DISK", cls.vm_disk),
            vm_netze=env.get("VM_NETZE", "").strip(),
            vm_max_cpu=max(1, zahl("VM_MAX_CPU", 4)),
            vm_max_ram_gb=max(1, zahl("VM_MAX_RAM_GB", 8)),
            vm_max_disk_gb=max(1, zahl("VM_MAX_DISK_GB", 64)),
            vm_pool_max_cpu=max(1, zahl("VM_POOL_MAX_CPU", 8)),
            vm_pool_max_ram_gb=max(1, zahl("VM_POOL_MAX_RAM_GB", 16)),
            vm_pool_max_disk_gb=max(1, zahl("VM_POOL_MAX_DISK_GB", 200)),
            vm_benutzer=wert("VM_BENUTZER", cls.vm_benutzer),
            vm_nameserver=env.get("VM_NAMESERVER", "").strip(),
            vm_verwaltung_ip=env.get("VM_VERWALTUNG_IP", "").strip(),
            ansible_playbook=env.get("ANSIBLE_PLAYBOOK", "").strip(),
            playbook_ordner=Path(pb_ordner) if pb_ordner else None,
            minecraft_rcon_passwort=env.get("MINECRAFT_RCON_PASSWORT", "").strip(),
            caddy_host=env.get("CADDY_HOST", "").strip().casefold(),
            caddy_domain=env.get("CADDY_DOMAIN", "").strip().casefold().strip("."),
            minecraft_rcon_host=env.get("MINECRAFT_RCON_HOST", "").strip(),
            minecraft_rcon_port=zahl("MINECRAFT_RCON_PORT", 25575),
            git_server_url=env.get("GIT_SERVER_URL", "").strip().rstrip("/"),
            git_server_token=env.get("GIT_SERVER_TOKEN", "").strip(),
            git_server_besitzer=env.get("GIT_SERVER_BESITZER", "").strip(),
            git_server_ca_datei=Path(git_ca) if git_ca else None,
            git_alte_branches_tage=max(1, zahl("GIT_ALTE_BRANCHES_TAGE", 14)),
            github_token=env.get("GITHUB_TOKEN", "").strip(),
            github_konto=env.get("GITHUB_KONTO", "").strip(),
            github_repos=liste("GITHUB_REPOS"),
            github_api_url=wert("GITHUB_API_URL", cls.github_api_url).rstrip("/"),
            github_intervall_minuten=max(5, zahl("GITHUB_INTERVALL_MINUTEN", 30)),
            prompt_vorlage=Path(vorlage) if vorlage else None,
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
        if self.wartung_von >= self.wartung_bis:
            raise KonfigurationsFehler("WARTUNG_VON muss vor WARTUNG_BIS liegen")
        if self.erreichbarkeit_stufe not in ("info", "wichtig", "notfall"):
            raise KonfigurationsFehler("ERREICHBARKEIT_STUFE muss info, wichtig oder notfall sein")
        try:
            from missa.integrationen.anmeldungen import NextcloudLog
            from missa.integrationen.ssh import hosts_lesen
            from missa.tools.fehlersuche import dienste_lesen

            hosts_lesen(self.ssh_hosts)
            dienste_lesen(self.fehlersuche_dienste)
            NextcloudLog.aus_text(self.nextcloud_log_host, self.nextcloud_log_quelle)
            from missa.integrationen.domains import erwartet_lesen
            from missa.tools.updates import wochentage_lesen

            wochentage_lesen(self.wartung_tage)
            erwartet_lesen(self.dns_erwartet)
            from missa.integrationen.proxmox_vms import bereich_lesen
            from missa.tools.vms import netze_lesen

            bereich_lesen(self.vm_id_bereich)
            netze_lesen(self.vm_netze)
            if self.caddy_host and self.caddy_host not in hosts_lesen(self.ssh_hosts):
                raise ValueError(
                    f"CADDY_HOST={self.caddy_host} fehlt in SSH_HOSTS – Missa erreicht "
                    "Caddy nur über SSH als Benutzer missa"
                )
            if not 0 < self.minecraft_rcon_port < 65536:
                raise ValueError("MINECRAFT_RCON_PORT muss zwischen 1 und 65535 liegen")
            if self.git_server_url and not self.git_server_url.startswith(("https://", "http://")):
                raise ValueError("GIT_SERVER_URL muss mit https:// beginnen")
            from missa.integrationen.github import repo_pruefen

            for repo in self.github_repos:
                try:
                    repo_pruefen(repo)
                except Exception as fehler:
                    raise ValueError(f"GITHUB_REPOS: {fehler}") from fehler
        except ValueError as fehler:
            raise KonfigurationsFehler(str(fehler)) from fehler
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
