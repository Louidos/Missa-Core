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
Ruhezeit als **eine** Sammelmeldung zugestellt („🌅 Während der Ruhezeit ist einiges
aufgelaufen (3 Meldungen)“, je Alarm mit der Uhrzeit, zu der Missa ihn bemerkt hat); ein
einzelner Alarm kommt wie gewohnt allein. Kommt zur selben Zeit ein Briefing nach einer
Auszeit, steckt alles darin (siehe [initiative.md](initiative.md#briefing-nach-einer-auszeit)).
`notfall` kommt immer sofort. Ändern per Gespräch:
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

Welche Domains Missa von außen prüft, steht in ihrer Datenbank (Tabelle
`erreichbarkeit_pruefungen`) – nicht mehr fest in `missa.env`. Missa legt Prüfungen im
Gespräch selbst an, ändert Port, Pfad, Intervall und Stufe, pausiert und löscht sie, so wie
die Alarm-Regeln. Eine Änderung gilt sofort, ohne Neustart und ohne env-Änderung.

### Was geprüft wird

Jede Prüfung läuft in ihrem eigenen Intervall (Missa schaut jede Minute, welche fällig sind;
pausierte laufen nicht):

1. **DNS**: Lässt sich der Name auflösen?
2. **Zertifikat**: TLS-Verbindung auf dem Port der Prüfung (Standard 443) mit voller
   Prüfung. Abgelaufen, falscher Name oder nicht vertrauenswürdig zählt als Ausfall. Das
   Ablaufdatum wird gespeichert.
3. **HTTPS-Antwort**: `GET https://<domain>[:<port>]<pfad>` (Standard: Pfad `/`). 2xx, 3xx
   und auch 401/403 (z. B. die API-Key-Prüfung von `ki.`) heißen „der Dienst ist da“. 5xx und
   keine Antwort zählen als Ausfall. Die Fehlermeldung nennt den echten Port („Keine
   HTTPS-Verbindung (Port 8443): …“).

Nach **zwei Fehlschlägen hintereinander** meldet Missa den Ausfall mit der Stufe der
Prüfung (Standard `ERREICHBARKEIT_STUFE`, meist `wichtig`) und dem Grund. Sobald das Ziel
wieder antwortet, kommt ein `info` mit der Dauer des Ausfalls. Der Zustand steht in der
Tabelle `erreichbarkeit` und überlebt Neustarts: Nach einem Neustart wird ein Ausfall nicht
noch einmal gemeldet. Ist ein Ausfall ein Fehlalarm (z. B. der Dienst läuft auf 8443), bietet
Missa von sich aus an, die Prüfung anzupassen („Soll ich das künftig auf 8443 prüfen?“).

### Im Chat

| Du sagst | Missa macht | Werkzeug |
|---|---|---|
| „Welche Prüfungen hast du?“ | Liste mit Nummer, Ziel, Intervall, Stufe, pausiert und letztem Ergebnis | `erreichbarkeit_pruefungen_anzeigen` |
| „Prüf cloud.louis-degiorgi.ch auf Port 8443“ | neue Prüfung `cloud.louis-degiorgi.ch:8443` (gibt es sie schon, wird sie aktualisiert, nicht doppelt angelegt) | `erreichbarkeit_pruefung_anlegen` |
| „Prüf cloud lieber auf Port 8443“ | ändert die bestehende Prüfung dauerhaft | `erreichbarkeit_pruefung_aendern` |
| „Prüf git nur noch jede Stunde“ | Intervall 60 Minuten | `erreichbarkeit_pruefung_aendern` |
| „Prüf ki auf /health“, „Melde einen Ausfall von git nur als info“ | Pfad bzw. Stufe ändern | `erreichbarkeit_pruefung_aendern` |
| „Pausier die Prüfung von git“ / „Prüf git wieder“ | pausieren und fortsetzen | `erreichbarkeit_pruefung_pausieren` |
| „Lösch die Prüfung von git“ | löscht erst nach Rückfrage und deinem Ja | `erreichbarkeit_pruefung_loeschen` |
| „Ist git von außen erreichbar?“ | prüft sofort (alle aktiven oder eine bestimmte Prüfung) | `erreichbarkeit_pruefen` |

Eine Prüfung wird nach Nummer (`2`, `#2`) oder Name (`git`, `cloud.louis-degiorgi.ch:8443`)
gefunden; passt ein Name auf mehrere, fragt Missa nach der Nummer.

Grenzen, die Missa streng prüft (wie beim Start): nur öffentliche Domainnamen – keine
IP-Adressen, kein `localhost`, kein Schema wie `https://`, keine Leerzeichen; Port 1–65535,
Pfad ab `/` ohne Leerzeichen und `#`, Intervall 1–1440 Minuten, Stufe `info`, `wichtig` oder
`notfall`, höchstens 50 Prüfungen. So dient die Prüfung nicht zum Abtasten des internen Netzes.

Missa verwaltet damit **nur ihre Prüfungen**. Schlüssel, Passwörter, Rechte und alle anderen
Einstellungen bleiben in `/etc/missa/missa.env`; kein Werkzeug schreibt in diese Datei.

### Startwert aus `ERREICHBARKEIT_DOMAINS`

`ERREICHBARKEIT_DOMAINS` ist nur noch der **Startwert**. Ist die Tabelle beim Start leer und
wurde noch nie etwas übernommen, legt Missa die Einträge daraus einmal als Prüfungen an (mit
`ERREICHBARKEIT_INTERVALL_MINUTEN` und `ERREICHBARKEIT_STUFE`) und vermerkt das im Log und in
der Datenbank (Tabelle `stand`, Merker `erreichbarkeit_startwert`). Danach liest Missa die
Variable für die Prüfung nicht mehr – auch nicht, wenn du alle Prüfungen löschst. Steht in
der env später etwas, das nicht in der Tabelle ist, erwähnt Missa das einmal im Log.

Ein Eintrag ist `domain`, `domain:port`, `domain/pfad` oder `domain:port/pfad`, z. B.:

```ini
ERREICHBARKEIT_DOMAINS=cloud.louis-degiorgi.ch:8443,ki.louis-degiorgi.ch:8443/health,git.louis-degiorgi.ch
```

Ungültige Einträge verhindern den Start mit einer Meldung, die den falschen Eintrag nennt.
`ERREICHBARKEIT_INTERVALL_MINUTEN` und `ERREICHBARKEIT_STUFE` gelten weiter als Standard für
neue Prüfungen.

**Nach dem Update:** Auf einem System mit `ERREICHBARKEIT_DOMAINS` sind diese Domains nach
dem ersten Start als Prüfungen da. Im Zustand heisst ein Ziel mit Port 443 und Pfad `/` weiter
einfach wie die Domain – schon gemeldete Ausfälle werden nicht noch einmal gemeldet. Andere
Ziele heissen z. B. `cloud.louis-degiorgi.ch:8443`. Ändert sich Port oder Pfad einer Prüfung,
beginnt der Zustand für das neue Ziel frisch.

Die Zertifikatsprüfung ([updates-zertifikate.md](updates-zertifikate.md)) nimmt ohne
`ZERTIFIKAT_DOMAINS` die Domains der aktiven Prüfungen mit deren Port.

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
| `erreichbarkeit_pruefen` | lesen | Domains sofort prüfen (alle aktiven oder eine Prüfung) |
| `erreichbarkeit_pruefungen_anzeigen` | lesen | alle Erreichbarkeits-Prüfungen mit Ziel, Intervall, Stufe, Pause und letztem Ergebnis |
| `erreichbarkeit_pruefung_anlegen` | schreiben | neue Prüfung (gleiches Ziel wird nicht doppelt angelegt) |
| `erreichbarkeit_pruefung_aendern` | schreiben | Port, Pfad, Intervall, Stufe, Notiz |
| `erreichbarkeit_pruefung_pausieren` | schreiben | pausieren und fortsetzen |
| `erreichbarkeit_pruefung_loeschen` | zerstoererisch | löschen, nur nach deinem Ja |

Es gibt kein Werkzeug, das auf einem Server etwas startet, stoppt oder ändert. Proxmox wird
ausschließlich per GET gelesen.
