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

Hinweise:

- **Erreichbarkeit:** Damit ai01 die eigene öffentliche Adresse erreicht, muss die ZyWALL
  das von innen erlauben (NAT-Loopback bzw. Policy-Route von ai01s VLAN auf die WAN-Adresse).
  Löst ai01 die Domains per Split-DNS intern auf (direkt auf rp01, 10.0.70.10), prüft Missa
  nur Caddy und nicht den Weg von außen. Beides steht so auch in
  [server-alarme.md](server-alarme.md).
- Alles andere bleibt, wie es ist: Nextcloud Talk und Nextcloud (CalDAV/CardDAV) siehe
  [nextcloud-talk.md](nextcloud-talk.md) und [installation.md](installation.md).
