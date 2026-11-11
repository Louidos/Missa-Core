# Server-Alarme, Monitoring und Erreichbarkeit

Missa behält die Server im Blick: Sie liest regelmäßig Messwerte (nur lesend), meldet sich
nach Regeln, die du ihr im Gespräch sagst, weist auf Auffälliges hin, **bevor** eine Grenze
erreicht ist, und prüft, ob deine Domains von außen erreichbar sind.

Alles ist ohne Konfiguration aus und stört den Start nicht. Die Einstellungen stehen im
Block „Server-Alarme und Monitoring“ in
[config/missa.env.example](../config/missa.env.example).

## Alarm-Stufen

| Stufe     | Bedeutung | Ruhezeit | So sagst du es |
|-----------|-----------|----------|----------------|
| `info`    | gut zu wissen | wartet bis zum Ende der Ruhezeit | „sag kurz Bescheid“, „nur als Hinweis“ |
| `wichtig` | bald anschauen | wartet bis zum Ende der Ruhezeit | „warn mich“, „wichtig“ |
| `notfall` | sofort | kommt **immer sofort**, auch nachts | „ruf mich an“, „weck mich“, „sofort“, „Notfall“ |

Ein Telefon-Anruf ist noch nicht eingerichtet. Bis dahin kommt auch ein Notfall über die
vorhandenen Kanäle (Nextcloud Talk, `NC_TALK_NOTIFY_CONVERSATION`), deutlich markiert mit
„🚨 NOTFALL“. Kommt später ein Telefon-Kanal dazu, braucht es keine neuen Regeln.

**Ruhezeit** (Standard 22:00–07:00): `info` und `wichtig` werden gesammelt und am Ende der
Ruhezeit zugestellt, mit dem Hinweis, wann Missa es bemerkt hat. Ändern per Gespräch:
„Ruhezeit von 23 bis 6 Uhr“, „Keine Ruhezeit mehr“ (Werkzeug `alarm_einstellungen`).

**Ruhefenster** (Standard 60 Minuten): Derselbe Alarm kommt nicht im Minutentakt. Hält ein
Problem an, erinnert Missa höchstens einmal pro Ruhefenster daran. Wird es dringender
(`info` → `wichtig` → `notfall`), meldet sie sich sofort. Ändern per Gespräch:
„Wiederhol Alarme höchstens alle 2 Stunden“, oder pro Regel (`wiederholen_nach_minuten`).

Alarme laufen auch während eines Notfall-Stopps weiter. Sie sind Nachrichten an dich und
keine Aktionen.

## Regeln per Gespräch

| Du sagst | Missa speichert |
|----------|-----------------|
| „Ruf mich an, wenn ai01 über 90 % ist“ | ai01, CPU über 90 %, länger als 5 Minuten → `notfall` |
| „Bei Auslastung über 90 % länger als 5 Minuten: Notfall“ | alle Server, CPU über 90 %, 5 Minuten → `notfall` |
| „Warn mich, wenn die Disk von int01 über 85 % ist“ | int01, Disk über 85 %, 5 Minuten → `wichtig` |
| „Sag Bescheid, wenn wb01 nicht läuft“ | wb01, läuft unter 1, 5 Minuten → `info` |
| „Welche Alarm-Regeln gibt es?“ | Liste mit Nummern und was gerade ausgelöst ist |
| „Ändere Regel 2 auf 95 %“ / „… auf 10 Minuten“ / „… auf wichtig“ | geänderte Regel |
| „Lösch Regel 2“ | Rückfrage, gelöscht erst nach deinem Ja (`zerstoererisch`) |

Gespeichert werden **Ziel** (VM- oder Host-Name, oder „alle“), **Messwert** (`cpu`, `ram`,
`disk`, `laeuft`, `erreichbar`, `antwortzeit` oder ein eigener Name aus deinem Monitoring-Tool),
**Richtung** (über/unter), **Grenze**, **Dauer** (Standard `ALARM_DAUER_MINUTEN`, 5 Minuten)
und **Stufe**. Die Regeln stehen in der Tabelle `alarm_regeln` in `MISSA_DATEN/missa.db` und
überleben Neustarts und Updates.

Eine Regel löst aus, wenn der Wert **die ganze Dauer** über der Grenze liegt. Eine kurze
Spitze reicht nicht. Ist der Wert danach wieder normal, kommt ein `info` „… wieder normal“
mit der Dauer des Alarms.

## Hinweise auf Auffälliges

Missa merkt sich für jeden Prozentwert (CPU, RAM, Disk) das Mittel pro Stunde und Tag
(Tabelle `messwert_stunden`, 30 Tage aufbewahrt). Daraus kennt sie den **üblichen Verlauf**:
das Mittel derselben Tageszeit über die letzten 7 Tage.

- Liegt der aktuelle Wert (Mittel der letzten 5 Minuten, mindestens 3 Messungen) mindestens
  `AUFFAELLIG_MIN_ABSTAND` (15) Prozentpunkte **und** deutlich mehr als die übliche Schwankung
  über dem Üblichen, kommt ein `info`, z. B. „ai01: CPU ungewöhnlich hoch – gerade 45 %,
  üblich um diese Zeit sind etwa 20 %“.
- Bei doppeltem Abstand (30 Punkte) wird daraus `wichtig`.
- Erst ab 3 Tagen Verlauf für diese Stunde, damit die ersten Tage nicht laut sind.
- Dieselbe Auffälligkeit kommt höchstens alle 6 Stunden, außer sie wird dringender.
- Gemeldet werden nur **Anstiege**. Ein Abfall (z. B. nach dem Aufräumen der Disk) bleibt
  still; einen gestoppten Dienst zeigen Regeln wie „läuft nicht“ und die Erreichbarkeit.
- Läuft für denselben Wert schon ein Regel-Alarm, kommt kein zusätzlicher Hinweis.

Ausschalten: `AUFFAELLIG_AKTIV=false`.

## Quellen

Missa holt alle `MONITORING_INTERVALL_SEKUNDEN` (Standard 60) die Werte aller eingerichteten
Quellen, gleichzeitig und **nur lesend**:

| Quelle | Einstellung | Liefert |
|--------|-------------|---------|
| Eigenes Monitoring-Tool | `MONITORING_URL`, optional `MONITORING_TOKEN` | beliebige Werte im JSON-Format aus [monitoring.md](monitoring.md) |
| Homepage-Dashboard | `HOMEPAGE_URL` | pro Dienst mit `siteMonitor`: `erreichbar` (1/0) und `antwortzeit` (ms) |
| Proxmox | `PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET` | pro VM, Container und Host: `cpu`, `ram`, `disk` (%), `laeuft` (1/0) |

Liefern zwei Quellen denselben Wert (z. B. CPU von ai01), gilt die erste: eigenes Monitoring
vor Homepage vor Proxmox. Antwortet eine Quelle 15 Minuten lang nicht, kommt ein `info`
(höchstens alle 12 Stunden) und ein weiteres, wenn sie wieder antwortet.

Bei VMs (qemu) kennt Proxmox die Disk-Belegung meist nicht. Diese Werte fehlen dann, bis
dein Monitoring-Tool sie liefert. Container (lxc) und der Host liefern sie.

Im Chat: „Wie ist die Auslastung von ai01?“ (Werkzeug `server_werte_anzeigen`).
Über die API: `GET /api/server`, siehe [api.md](api.md).

### Proxmox einrichten (nur lesendes Token)

Auf **pve-id01** als root, von Hand (Missa führt das nie selbst aus):

```bash
bash proxmox-lesetoken.sh --dry-run
```

```bash
bash proxmox-lesetoken.sh
```

Das Script ([scripts/einrichten/proxmox-lesetoken.sh](../scripts/einrichten/proxmox-lesetoken.sh))
legt den Benutzer `missa@pve` ohne Passwort an, gibt ihm die Rolle **PVEAuditor** (nur lesen)
und erzeugt das Token `missa@pve!monitor` mit eigener Rechte-Trennung (`--privsep 1`), das
ebenfalls nur PVEAuditor hat. Das Secret wird einmal angezeigt. Trag es auf ai01 in
`/etc/missa/missa.env` ein (`PROXMOX_TOKEN_SECRET`) und kopiere für die Zertifikatsprüfung
`/etc/pve/pve-root-ca.pem` nach ai01 (`PROXMOX_CA_DATEI`).

Danach braucht ai01 eine Freigabe zu pve-id01, Port 8006 (siehe
[netzfreigaben.md](netzfreigaben.md)). Missa ändert keine Firewall-Regeln.

## Erreichbarkeit von außen

Alle `ERREICHBARKEIT_INTERVALL_MINUTEN` (Standard 5) prüft Missa jede Domain aus
`ERREICHBARKEIT_DOMAINS` (z. B. `cloud.`, `git.` und `ki.louis-degiorgi.ch`):

1. **DNS**: Lässt sich der Name auflösen?
2. **Zertifikat**: TLS-Verbindung auf Port 443 mit voller Prüfung. Abgelaufen, falscher
   Name oder nicht vertrauenswürdig zählt als Ausfall. Das Ablaufdatum wird gespeichert.
3. **HTTPS-Antwort**: `GET https://<domain>/`. 2xx, 3xx und auch 401/403 (z. B. die
   API-Key-Prüfung von `ki.`) heißen „der Dienst ist da“. 5xx und keine Antwort zählen als Ausfall.

Nach **zwei Fehlschlägen hintereinander** meldet Missa den Ausfall (Stufe
`ERREICHBARKEIT_STUFE`, Standard `wichtig`), mit dem Grund. Sobald die Domain wieder
antwortet, kommt ein `info` mit der Dauer des Ausfalls. Der Zustand steht in der Tabelle
`erreichbarkeit` und überlebt Neustarts: Nach einem Neustart wird ein Ausfall nicht noch
einmal gemeldet.

Im Chat: „Ist git von außen erreichbar?“ (Werkzeug `erreichbarkeit_pruefen`).

**Grenze dieser Prüfung:** Sie läuft auf ai01 über die öffentliche Domain. Löst ai01 die
Domain intern auf (Split-DNS) oder erlaubt die ZyWALL keinen Weg von innen auf die
öffentliche Adresse (NAT-Loopback), sieht das Ergebnis anders aus als von außen. Ein echter
Test von außerhalb des Netzes braucht einen externen Host und kommt später.

## Werkzeuge

| Werkzeug | Stufe | Wofür |
|----------|-------|-------|
| `server_werte_anzeigen` | lesen | aktuelle Messwerte, optional für ein Ziel |
| `alarmregel_anlegen` | schreiben | neue Regel (gleiche Regel wird nicht doppelt angelegt) |
| `alarmregeln_anzeigen` | lesen | alle Regeln und was gerade ausgelöst ist |
| `alarmregel_aendern` | schreiben | Grenze, Richtung, Dauer, Stufe, Ziel, Messwert, Ruhefenster |
| `alarmregel_loeschen` | zerstoererisch | löschen, nur nach deinem Ja |
| `alarme_anzeigen` | lesen | zuletzt gemeldete Alarme, Ruhezeit, Ruhefenster |
| `alarm_einstellungen` | schreiben | Ruhezeit und Ruhefenster ändern |
| `erreichbarkeit_pruefen` | lesen | Domains sofort prüfen |

Es gibt kein Werkzeug, das auf einem Server etwas startet, stoppt oder ändert. Proxmox wird
ausschließlich per GET gelesen.
