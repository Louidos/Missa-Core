# Container, Fehlersuche in Logs und Login-Überwachung

Missa schaut per SSH auf deine Hosts (wb01, int01, rp01 …): Sie zeigt den Status der
Docker-Container, erklärt Fehler aus den Logs verständlich und meldet ungewöhnliche
Anmeldungen. Gelesen wird immer nur. Das Einzige, was etwas verändert, ist der Neustart
eines Containers, und der läuft nur nach deinem ausdrücklichen Ja.

Ohne `SSH_HOSTS` ist alles aus und stört den Start nicht. Die Einstellungen stehen im Block
„Container, Logs und Anmeldungen“ in [config/missa.env.example](../config/missa.env.example).

## Was du sagen kannst

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Welche Container laufen auf wb01?“ | Liste mit Status, CPU, RAM; hängende sind markiert | `container_anzeigen` (lesen) |
| „Zeig die Logs von nextcloud“ / „… nur die Fehler“ | letzte Zeilen, lange Logs in Teilen | `container_logs` (lesen) |
| „Starte nextcloud auf int01 neu“ | sagt, was passiert, und wartet auf dein Ja | `container_neustarten` (zerstoererisch) |
| „Warum geht Nextcloud nicht?“ | liest Container-, System- und Nextcloud-Logs und erklärt die wahrscheinliche Ursache | `fehlersuche` (lesen) |
| „Was steht im System-Log von rp01?“ / „… nur Fehler der letzten 3 Stunden“ | journalctl, in Teilen | `journal_lesen` (lesen) |
| „Wer hat sich heute angemeldet?“ | SSH- und Nextcloud-Anmeldungen, Fehlversuche nach IP | `anmeldungen_anzeigen` (lesen) |
| „10.0.30.5 ist mein Laptop“ / „… ist bekannt“ | IP als bekannt markieren, dafür gibt es keine Hinweise mehr | `ip_als_bekannt_markieren` (schreiben) |
| „Welche IPs sind bekannt?“ / „10.0.30.5 ist nicht mehr bekannt“ | Liste bzw. Markierung entfernen | `bekannte_ips_anzeigen`, `ip_nicht_mehr_bekannt` |
| „Melde dich erst ab 20 Fehlversuchen in 10 Minuten“ | gespeichert, gilt sofort | `login_einstellungen` (schreiben) |

Jeder Aufruf steht im Aktionsprotokoll („Was hast du heute gemacht?“). Ein Neustart
erscheint dort zweimal: einmal als Rückfrage und einmal ausgeführt mit `bestätigt`. Im
Hintergrund (Routinen, Nachtschicht) startet Missa nie etwas neu.

## Fehlersuche

„Warum geht Nextcloud nicht?“ läuft so ab:

1. **Quellen finden:** Container, deren Name den Dienst enthält (auf allen Hosts oder dem
   genannten), sonst systemd-Units mit dem Namen (`*nextcloud*`). Genauer geht es mit
   `FEHLERSUCHE_DIENSTE`, z. B. `nextcloud:int01:nextcloud-app,nextcloud-db;caddy:rp01:caddy.service`.
   Bei Nextcloud kommt das Nextcloud-Log dazu, wenn es eingerichtet ist.
2. **Lesen:** die letzten 400 Zeilen jeder Quelle und dazu die Fehler aus dem System-Log
   der letzten Stunde (`journalctl -p err`).
3. **Filtern:** nur Zeilen, die nach einem Problem aussehen. Gleiche Meldungen werden
   zusammengefasst („(12×)“).
4. **Erkennen:** Bekannte Ursachen erkennt der Code selbst, z. B. volle Festplatte, zu wenig
   Arbeitsspeicher, Datenbank nicht erreichbar, Wartungsmodus, Redis, abgelaufenes
   Zertifikat, DNS, belegter Port, Rechteproblem, `trusted_domains`, Backend hinter Caddy
   nicht erreichbar. Dazu kommen der Zustand der Container (abgestürzt, ungesund …), volle
   Disks aus dem Monitoring und die Erreichbarkeit von außen.
5. **Erklären:** Missa bekommt die Belege und erklärt sie dir in ein paar einfachen Sätzen,
   mit dem nächsten Schritt. Was nicht eindeutig ist, sagt sie ehrlich.

Lange Logs kommen in Teilen (`teil=1` sind die neuesten Zeilen, höchstens ca. 6000 Zeichen
pro Teil). Für eine sehr gründliche Analyse bietet Missa an, sie heute Nacht in der
Nachtschicht zu machen (`nachtaufgabe_anlegen`, siehe
[routinen-nachtschicht.md](routinen-nachtschicht.md)).

## Hängende Container

Alle `CONTAINER_INTERVALL_MINUTEN` (5) liest Missa `docker ps` auf jedem Host und meldet
(`wichtig`):

- **Neustart-Schleife** (Status `restarting`)
- **ungesund**: Der Healthcheck schlägt fehl (`unhealthy`)
- **hängt beim Start**: über 10 Minuten in `health: starting`
- **abgestürzt**: Exit-Code ungleich 0, gemeldet nur, wenn der Container vorher lief
- **tot** (`dead`)

Läuft der Container wieder normal, kommt ein `info`. Dieselbe Meldung kommt nicht im
5-Minuten-Takt (Ruhefenster, siehe [server-alarme.md](server-alarme.md)). Einmal-Jobs
lassen sich mit `CONTAINER_IGNORIEREN` ausnehmen. Ist ein Host 15 Minuten lang per SSH
nicht erreichbar, kommt ein `info`.

## Login-Überwachung

Alle `LOGIN_INTERVALL_MINUTEN` (5) liest Missa die neuen Anmeldungen:

- **SSH** auf allen Hosts aus `SSH_HOSTS` (oder nur `LOGIN_HOSTS`): journal von `sshd`
  bzw. `sshd-session`. Jeder Fehlversuch zählt einmal, auch bei Servern ohne Passwort-Login.
- **Nextcloud** (wenn `NEXTCLOUD_LOG_HOST` und `NEXTCLOUD_LOG_QUELLE` gesetzt sind): Zeilen
  „Login failed“. Erfolgreiche Anmeldungen schreibt Nextcloud nur mit der App „Auditing /
  Logging“ (`admin_audit`) ins Log. Ohne sie sieht Missa bei Nextcloud nur Fehlversuche.

Missa meldet:

| Was | Stufe |
|-----|-------|
| mindestens 10 Fehlversuche in 15 Minuten auf einem Host (`LOGIN_FEHLVERSUCHE_*`, im Gespräch änderbar) | `wichtig` |
| erfolgreiche Anmeldung von einer IP, die es noch nie gab, von außen | `wichtig` |
| … aus dem eigenen Netz (10.x, 172.16–31.x, 192.168.x, 100.64.x, fc00::/7) | `info` (zu ungewohnter Zeit: `wichtig`) |
| erfolgreiche Anmeldung zu einer ungewohnten Zeit | `info` |

„Ungewohnt“ lernt Missa aus den letzten 30 Tagen: eine Stunde, in die weniger als 3 % aller
Anmeldungen fallen. Solange es weniger als 20 Anmeldungen gibt, gilt 00:00–05:00 als
ungewohnt (`LOGIN_NACHT_*`). Automatische Benutzer (`LOGIN_AUTOMATISCH_BENUTZER`, Standard
`missa`) lösen keine Hinweise auf ungewohnte Zeiten aus, eine neue IP für sie aber schon.

**Bekannte IPs** lösen keine Hinweise aus. Markieren geht im Gespräch oder fest mit
`LOGIN_BEKANNTE_IPS` (z. B. die IP von ai01 und deines Laptops). Beim allerersten Lauf lernt
Missa still die letzten 7 Tage, damit nicht jede bisherige IP als „neu“ gemeldet wird.

Gespeichert wird in `MISSA_DATEN/missa.db` (Tabellen `anmeldungen`, 90 Tage aufbewahrt,
`login_ips`, `login_stand`). Über die API: `GET /api/anmeldungen`, siehe [api.md](api.md).

## Einrichten

### 1. Schlüssel auf ai01

Auf ai01 muss der SSH-Client installiert sein (`sudo apt-get install openssh-client`, meist
schon da).

```bash
sudo install -d -m 0750 -o missa -g missa /etc/missa/ssh
```

```bash
sudo -u missa ssh-keygen -t ed25519 -N "" -C missa@ai01 -f /etc/missa/ssh/id_ed25519
```

```bash
sudo cat /etc/missa/ssh/id_ed25519.pub
```

### 2. Benutzer `missa` auf jedem Host

Das Script [scripts/einrichten/missa-benutzer.sh](../scripts/einrichten/missa-benutzer.sh)
auf den Host kopieren und dort als root ausführen, erst zum Anschauen, dann wirklich:

```bash
sudo bash missa-benutzer.sh --dry-run --schluessel "ssh-ed25519 AAAA… missa@ai01"
```

```bash
sudo bash missa-benutzer.sh --schluessel "ssh-ed25519 AAAA… missa@ai01"
```

Auf rp01 zusätzlich `--caddy`, damit Missa Subdomains vorbereiten kann (siehe
[caddy-minecraft.md](caddy-minecraft.md)). Auf dem Nextcloud-Host zusätzlich `--nextcloud-log /pfad/zur/nextcloud.log`. Damit kann
`missa` genau diese eine Datei lesen. Am saubersten ist eine eigene Logdatei außerhalb des
Datenordners (`'logfile' => '/var/log/nextcloud/nextcloud.log'` in der `config.php`).
Alternativ loggt Nextcloud per syslog ins journal (`'log_type' => 'syslog'`), dann
`NEXTCLOUD_LOG_QUELLE=journal`. Wird die Logdatei rotiert, das Script danach noch einmal
ausführen (oder `setfacl -m u:missa:r` in die logrotate-Regel aufnehmen).

Was das Script einrichtet (siehe `--nur-sudoers` und `--nur-befehl`):

- Benutzer `missa` ohne Passwort; anmelden geht nur mit Missas Schlüssel.
- In `authorized_keys` ist der Schlüssel mit `restrict,command="/usr/local/lib/missa/missa-befehl"`
  gebunden: keine Shell, kein Terminal, keine Weiterleitungen. `missa-befehl` kennt nur
  `pruefen`, `docker-ps`, `docker-stats`, `docker-logs`, `docker-restart`, `journal`, `ssh-log`
  und `nextcloud-log` und prüft jede Angabe.
- `/etc/sudoers.d/missa` (mit `visudo -c` geprüft) erlaubt per sudo **nur ein Programm**,
  `/usr/local/lib/missa/missa-docker` (root, `0755`). Es prüft jede Angabe und kennt nur:
  - `docker ps --all --no-trunc --format {{json .}}`
  - `docker stats --no-stream --no-trunc --format {{json .}}` (nur lesen, für CPU und RAM)
  - `docker logs --tail <1–5000> --timestamps <name>`
  - `docker restart <name>`

  Dazu `/usr/local/lib/missa/missa-update` für die Update-Verwaltung: nur `apt-get update`,
  `apt-get -y upgrade` und `reboot` (siehe [updates-zertifikate.md](updates-zertifikate.md)).

  Nichts sonst: kein `docker exec`, kein `docker run`, kein `docker rm`, keine Shell, kein
  `journalctl` per sudo. Die sudoers-Zeile hat keine Platzhalter und funktioniert darum mit
  dem klassischen sudo genauso wie mit sudo-rs (Standard ab Ubuntu 25.10), das Platzhalter
  in Argumenten nicht erlaubt.
- Logs (journalctl) liest `missa` über die Gruppe **systemd-journal**, ohne sudo. Das ist
  sicherer als `sudo journalctl`: Dort könnte der Pager eine Root-Shell öffnen, und
  `--vacuum` könnte Logs löschen.

Rückgängig: `sudo bash missa-benutzer.sh --entfernen`.

**Proxmox-Host (pve-id01):** Das Script bricht dort ab. Einrichten nur, wenn du das
ausdrücklich willst, mit `--proxmox-ja`. Für die VM-Werte reicht das nur lesende
Proxmox-Token aus [server-alarme.md](server-alarme.md). Auf dem Host selbst braucht Missa
normalerweise nichts.

### 3. Host-Schlüssel prüfen und auf ai01 eintragen

Missa verbindet sich nur mit Hosts, deren Schlüssel in `SSH_KNOWN_HOSTS` steht
(`StrictHostKeyChecking=yes`). Pro Host, den Fingerabdruck mit dem auf dem Host vergleichen
(`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`):

```bash
ssh-keyscan -t ed25519 <adresse> | sudo -u missa tee -a /etc/missa/ssh/known_hosts
```

```bash
sudo -u missa ssh-keygen -lf /etc/missa/ssh/known_hosts
```

Dann in `/etc/missa/missa.env` z. B. `SSH_HOSTS=wb01=<IP>,int01=<IP>,rp01=10.0.70.10`,
Missa neu starten und testen:

```bash
sudo -u missa ssh -i /etc/missa/ssh/id_ed25519 -o UserKnownHostsFile=/etc/missa/ssh/known_hosts missa@10.0.70.10 pruefen
```

Antwortet der Host mit `missa-befehl ok <hostname>`, passt alles. Die nötigen
Netzfreigaben (ai01 → Host, TCP 22) stehen in [netzfreigaben.md](netzfreigaben.md).
