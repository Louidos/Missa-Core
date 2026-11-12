# Homelab-Wissen und Firewall-Logs

Missa kennt den aktuellen Stand deines Homelabs von selbst und fasst die Logs der ZyWALL
zusammen. Beides ist **nur lesend**: Missa hat keine Zugangsdaten zur Firewall, und es gibt
kein Werkzeug, das Firewall-Regeln ändert.

## Homelab-Wissen (Proxmox)

Braucht das nur lesende Proxmox-Token aus [server-alarme.md](server-alarme.md)
(`PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET`).

Alle `HOMELAB_INTERVALL_MINUTEN` (Standard 60) liest Missa alle VMs und Container mit
Name, VMID, Host, Status, CPU, RAM, Disk, Pool, VLAN-Tag (aus `net0`, `net1` …) und IP:

1. **Guest-Agent**, wo vorhanden (`agent/network-get-interfaces`). Das Token braucht dafür
   das Recht, den Agenten zu lesen: In Proxmox VE 9 ist das `VM.GuestAgent.Audit`. In
   Proxmox VE 8 gibt es dafür nur `VM.Monitor`, und das ist mehr als lesen, darum vergibt
   es das Einrichtungs-Script nicht.
2. sonst die **feste IP aus der Konfiguration** (Cloud-Init `ipconfig0` bzw. `ip=` bei
   Containern),
3. bei laufenden **Containern** die Schnittstellen (`lxc/<id>/interfaces`).

Der Stand steht in der Tabelle `inventar` und fließt in Missas Wissen ein. Kommt eine VM in
deiner Frage vor („Welche IP hat rp01?“), steht ihr aktueller Eintrag im Systemprompt. Bei
allgemeinen Fragen („Welche VMs gibt es?“, „Welche IPs …“) steht dort die ganze Liste.
Dazu gibt es das Werkzeug `homelab_anzeigen` (lesen) und `GET /api/homelab`.

**Änderungen** meldet Missa als `info`: neue VM, verschwundene VM, geänderte IP. Beim
allerersten Scan lernt sie still, damit nicht alles als „neu“ kommt.

## Firewall-Logs (ZyWALL)

### Einrichten

1. **Auf ai01** rsyslog Logs per UDP empfangen lassen und in eine eigene Datei schreiben.
   Vorlage: [config/rsyslog-zywall.conf.example](../config/rsyslog-zywall.conf.example)
   (`<ZYWALL-IP>` ersetzen):

   ```bash
   sudo cp config/rsyslog-zywall.conf.example /etc/rsyslog.d/30-zywall.conf
   ```

   ```bash
   sudo install -d -m 0750 -o root -g missa /var/log/zywall
   ```

   ```bash
   sudo systemctl restart rsyslog
   ```

2. **In der ZyWALL** (Weboberfläche, von Hand): *Configuration → Log & Report → Log
   Settings*, einen *Remote Server* aktivieren: Adresse von ai01, Format *VRPT/Syslog*,
   Facility z. B. *Local 1*. Unter *Active Log* mindestens *Security Policy* (bzw.
   *Firewall*) und gern *IDP*/*Anti-Malware* auswählen.
3. **Netzfreigabe:** ZyWALL → ai01, UDP 514 (siehe [netzfreigaben.md](netzfreigaben.md)).
4. In `/etc/missa/missa.env`: `FIREWALL_LOG_DATEI=/var/log/zywall/zywall.log`, dann
   `sudo systemctl restart missa-core`.

Logrotate: Für `/var/log/zywall/*.log` eine normale logrotate-Regel anlegen. Missa erkennt
eine neue Datei und liest dort von vorn.

### Was Missa daraus macht

Jede Minute liest Missa neue Zeilen. Sie wertet die Felder `src`, `dst`, `msg`, `note`,
`cat` und `proto` aus und speichert die Verbindungen 30 Tage (Tabelle
`firewall_ereignisse`). Jeden Morgen um `FIREWALL_BERICHT_ZEIT` (07:30) schickt sie eine
Zusammenfassung des Vortags als `info`:

- **Gehäufte Blocks:** Quellen mit mindestens `FIREWALL_BLOCK_GRENZE` (100) geblockten
  Verbindungen am Tag, mit den häufigsten Ziel-Ports.
- **Port-Scans:** eine Quelle, die in 10 Minuten mindestens `FIREWALL_SCAN_PORTS` (15)
  verschiedene Ports anfragt.
- **Ungewöhnliche Ziele:** Verbindungen aus dem eigenen Netz zu Zielen und Ports, die es in
  den 7 Tagen davor nie gab.

Auf Nachfrage: „Was war gestern an der Firewall los?“ (Werkzeug `firewall_zusammenfassung`,
lesen; auch „heute“, „vorgestern“ oder ein Datum).

> **Offen:** Das genaue Log-Format deiner ZyWALL-Firmware ist nicht getestet. Missa erwartet
> Felder wie `src="203.0.113.7:54321" dst="198.51.100.10:22" msg="… DROP" note="ACCESS BLOCK"`.
> Sieht es bei dir anders aus, bitte ein paar Beispielzeilen schicken.
