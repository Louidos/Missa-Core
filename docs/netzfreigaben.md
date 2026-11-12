# Netzfreigaben

Welche Verbindungen missa-core braucht. Missa (und Claude Code) ändert **keine**
Firewall-Regeln: Diese Liste ist die Vorlage für Louis, der die Regeln selbst in der
ZyWALL anlegt. Konkrete Adressen stehen in `/etc/missa/missa.env`, nicht im Code.

Heute gilt: ai01 (Missa, eigenes VLAN) erreicht weder Proxmox noch die Firewall; das darf
nur wb01. Jede Gruppe des Server-Updates trägt hier ein, was sie zusätzlich braucht.

| Quelle | Ziel | Port | Wozu | Seit | Nötig, wenn |
|--------|------|------|------|------|-------------|
| ai01 | pve-id01 (Proxmox-API, `PROXMOX_URL`) | TCP 8006 | Server-Alarme: CPU, RAM, Disk und Status pro VM lesen (Token nur PVEAuditor) | Server-Alarme | `PROXMOX_URL` gesetzt |
| ai01 | Host des eigenen Monitoring-Tools (`MONITORING_URL`) | Port aus der URL (TCP) | Server-Alarme: Messwerte als JSON lesen | Server-Alarme | `MONITORING_URL` gesetzt |
| ai01 | Host des Homepage-Dashboards (`HOMEPAGE_URL`) | Port aus der URL (TCP, meist 3000) | Server-Alarme: Status der Dienste lesen | Server-Alarme | `HOMEPAGE_URL` gesetzt |
| ai01 | DNS-Resolver von ai01 | UDP/TCP 53 | Erreichbarkeit: öffentliche Namen auflösen | Server-Alarme | `ERREICHBARKEIT_DOMAINS` gesetzt |
| ai01 | öffentliche Adresse von cloud., git. und ki.louis-degiorgi.ch | TCP 443 | Erreichbarkeit: HTTPS-Antwort und Zertifikat prüfen | Server-Alarme | `ERREICHBARKEIT_DOMAINS` gesetzt |
| ai01 | jeder Host aus `SSH_HOSTS` (z. B. wb01, int01, rp01 10.0.70.10) | TCP 22 | Container, Logs, Anmeldungen: SSH als `missa`, nur die Befehle aus `missa-befehl` | Container und Logs | `SSH_HOSTS` gesetzt |
| ai01 | pve-id01 | TCP 22 | nur, wenn du Missa ausdrücklich auch auf dem Proxmox-Host einrichtest (`--proxmox-ja`) | Container und Logs | sonst nicht nötig |
| jeder Host aus `SSH_HOSTS` | Paketquellen (deb.debian.org, security.debian.org bzw. Ubuntu-Spiegel) | TCP 80/443 | Updates: `apt-get update` und `apt-get -y upgrade` laufen auf dem Host selbst | Updates | Updates werden genutzt (meist schon erlaubt) |
| ai01 | pve-id01 (`PROXMOX_URL`) | TCP 8006 | Eigene VMs: anlegen, einrichten, starten, stoppen, löschen – Token nur für den Pool missa | Proxmox-VMs | `PROXMOX_VM_TOKEN_*` gesetzt (gleiche Freigabe wie oben) |
| ai01 | neue VMs in den VLANs aus `VM_NETZE` | TCP 22 | Ansible richtet die VM ein (SSH als `VM_BENUTZER`) | Proxmox-VMs | VMs werden genutzt |
| neue VMs | Paketquellen und piston-meta/piston-data.mojang.com | TCP 80/443 | Updates, Java und das Minecraft-Server-Jar | Proxmox-VMs | VMs werden genutzt |
| Spieler (LAN bzw. außen) | Minecraft-VM | TCP 25565 | Minecraft spielen (von außen nur mit Portweiterleitung in der ZyWALL) | Proxmox-VMs | Minecraft läuft |
| ZyWALL | ai01 | UDP 514 | Firewall-Logs per Syslog an rsyslog auf ai01 (Missa liest die Datei) | Homelab-Wissen und Firewall | `FIREWALL_LOG_DATEI` gesetzt |
| ai01 | data.iana.org | TCP 443 | Domains: Liste der RDAP-Server (IANA) | Zertifikate und Domains | `RDAP_AKTIV=true` und Domains gesetzt |
| ai01 | RDAP-Server der Endung (für `.ch`: rdap.nic.ch) | TCP 443 | Domains: Ablaufdatum abfragen – die einzige Abfrage nach außen dieser Prüfung | Zertifikate und Domains | `RDAP_AKTIV=true` und Domains gesetzt |

Hinweise:

- **Erreichbarkeit:** Damit ai01 die eigene öffentliche Adresse erreicht, muss die ZyWALL
  das von innen erlauben (NAT-Loopback bzw. Policy-Route von ai01s VLAN auf die WAN-Adresse).
  Löst ai01 die Domains per Split-DNS intern auf (direkt auf rp01, 10.0.70.10), prüft Missa
  nur Caddy und nicht den Weg von außen. Beides steht so auch in
  [server-alarme.md](server-alarme.md).
- Alles andere bleibt, wie es ist: Nextcloud Talk und Nextcloud (CalDAV/CardDAV) siehe
  [nextcloud-talk.md](nextcloud-talk.md) und [installation.md](installation.md).
