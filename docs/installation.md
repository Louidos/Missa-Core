# Installation auf ai01

Diese Anleitung installiert Missa-Core als systemd-Dienst unter dem Benutzer
`missa`. llama-server läuft bereits separat auf `127.0.0.1:8080` und wird hier
nicht verändert. HTTPS übernimmt Caddy auf einem anderen Server; Missa-Core
selbst spricht nur HTTP im internen Netz.

## Überblick: Wo liegt was?

| Pfad                       | Inhalt                                          | Besitzer / Rechte      |
|----------------------------|-------------------------------------------------|------------------------|
| `/opt/missa/app`           | Programmcode (Git-Checkout dieses Repos)        | `root`, 755            |
| `/opt/missa/venv`          | Python-Umgebung mit Missa-Core                  | `root`, 755            |
| `/opt/missa/data`          | Laufzeitdaten: `missa.db` (Verlauf, Gedächtnis) | `missa:missa`, 750     |
| `/etc/missa/missa.env`     | Konfiguration mit Schlüsseln und Passwörtern    | `root:missa`, 640      |
| `/etc/missa/charakter.md`  | Charakter von Missa                             | `root:missa`, 640      |
| `/etc/missa/llama.key`     | API-Key von llama-server (falls benutzt)        | `root:missa`, 640      |
| `/etc/systemd/system/missa-core.service` | systemd-Unit                      | `root`, 644            |

## 1. Voraussetzungen

- Linux mit systemd (ai01)
- Python 3.13 systemweit installiert (`python3.13 --version`), dazu `python3.13-venv`
- `git`
- llama-server läuft mit `--jinja` auf `127.0.0.1:8080` (prüfen: `curl http://127.0.0.1:8080/health`)

> Liegt Python unter `/home` (z. B. von pyenv oder uv), kann der Dienst es wegen
> `ProtectHome=true` nicht starten. Dann Python systemweit installieren oder in der
> Unit `ProtectHome=read-only` setzen.

## 2. Benutzer und Ordner anlegen

```bash
sudo useradd --system --home-dir /opt/missa --shell /usr/sbin/nologin missa
```

```bash
sudo install -d -o root -g root -m 755 /opt/missa
```

```bash
sudo install -d -o missa -g missa -m 750 /opt/missa/data
```

```bash
sudo install -d -o root -g missa -m 750 /etc/missa
```

## 3. Code holen und installieren

```bash
sudo git clone https://github.com/Louidos/Missa-Core.git /opt/missa/app
```

Auf eine veröffentlichte Version wechseln (statt des neuesten Stands von `main`):

```bash
sudo git -C /opt/missa/app checkout v0.2.0
```

Python-Umgebung anlegen und Missa-Core hineininstallieren:

```bash
sudo python3.13 -m venv /opt/missa/venv
```

```bash
sudo /opt/missa/venv/bin/pip install /opt/missa/app
```

Danach gibt es das Programm `/opt/missa/venv/bin/missa-core`.

## 4. Konfiguration

Vorlage kopieren und anpassen:

```bash
sudo install -m 640 -o root -g missa /opt/missa/app/config/missa.env.example /etc/missa/missa.env
```

```bash
sudo nano /etc/missa/missa.env
```

Einen zufälligen API-Schlüssel für die Apps erzeugen und bei `MISSA_API_KEY` eintragen:

```bash
openssl rand -hex 32
```

Charakter von Missa anlegen (Beispiel als Startpunkt):

```bash
sudo install -m 640 -o root -g missa /opt/missa/app/config/charakter.example.md /etc/missa/charakter.md
```

Läuft llama-server mit `--api-key`, denselben Schlüssel in `/etc/missa/llama.key` ablegen
(sonst diesen Schritt auslassen – ohne Datei schickt Missa-Core keinen Schlüssel):

```bash
sudo install -m 640 -o root -g missa /dev/null /etc/missa/llama.key
```

```bash
sudo nano /etc/missa/llama.key
```

### Nextcloud-App-Passwort

In Nextcloud: **Einstellungen → Sicherheit → Geräte & Sitzungen → „Neues App-Passwort
erstellen“** (Name z. B. „Missa“). Das angezeigte Passwort bei
`NEXTCLOUD_APP_PASSWORD` eintragen. So muss das normale Passwort nie auf ai01 liegen,
und das App-Passwort lässt sich jederzeit einzeln widerrufen.

### Alle Variablen

| Variable | Standard | Pflicht | Bedeutung |
|----------|----------|---------|-----------|
| `MISSA_HOST` | `0.0.0.0` | nein | Adresse, auf der die API lauscht. `0.0.0.0` = alle Netzwerkkarten (nötig, damit Caddy auf dem anderen Server zugreifen kann). |
| `MISSA_PORT` | `8000` | nein | Port der API. |
| `MISSA_API_KEY` | – | **ja** | Bearer-Token, mit dem sich jede App anmeldet. Ohne ihn startet der Dienst nicht. |
| `MISSA_CHARAKTER_DATEI` | `/etc/missa/charakter.md` | nein | Charakter von Missa. Wird bei jeder Anfrage neu gelesen – Änderungen wirken sofort, ohne Neustart. Fehlt die Datei, nutzt Missa einen kurzen Ersatztext (Warnung im Log, `/health` meldet `"charakter": "fehlt"`). |
| `MISSA_DATEN` | `/opt/missa/data` | nein | Ordner für `missa.db` (Gesprächsverlauf und Gedächtnis). Muss für `missa` beschreibbar sein. |
| `LLAMA_URL` | `http://127.0.0.1:8080` | nein | Adresse von llama-server. |
| `LLAMA_API_KEY_FILE` | `/etc/missa/llama.key` | nein | Datei mit dem API-Key von llama-server. Fehlt sie oder ist sie leer, wird kein Schlüssel geschickt. Wird bei jeder Anfrage neu gelesen. |
| `LLAMA_TIMEOUT` | `600` | nein | Wie viele Sekunden auf eine Antwort von llama-server gewartet wird. Auf der CPU großzügig lassen. |
| `TIMEZONE` | `Europe/Zurich` | nein | Zeitzone für Datum/Uhrzeit im Systemprompt, für Termine, Aufgaben und Erinnerungen. |
| `MISSA_LOG_LEVEL` | `INFO` | nein | Ausführlichkeit der Logs: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `NEXTCLOUD_URL` | – | für Kalender/Aufgaben | Adresse von Nextcloud, z. B. `https://cloud.example.ch` (ohne `/remote.php/dav`, das wird angehängt). |
| `NEXTCLOUD_USER` | – | für Kalender/Aufgaben | Nextcloud-Benutzername. |
| `NEXTCLOUD_APP_PASSWORD` | – | für Kalender/Aufgaben | App-Passwort (siehe oben). |
| `NEXTCLOUD_KALENDER` | – | für Kalender | Anzeigename des Kalenders, so wie er in Nextcloud heißt (Groß/Klein egal). |
| `NEXTCLOUD_AUFGABEN_LISTE` | – | für Aufgaben | Anzeigename der Liste in der App „Aufgaben“. |
| `NEXTCLOUD_ADRESSBUCH` | – (alle) | nein | Adressbuch für Geburtstage und Kontaktpflege (Anzeigename, z. B. `Kontakte`). |
| `KONTAKTE_GEBURTSTAG_VORLAUF` | `3` | nein | So viele Tage vor einem Geburtstag erinnert Missa (zusätzlich am Tag selbst); `0` = nur am Tag. |
| `KONTAKTE_PRUEFZEIT` | `08:00` | nein | Uhrzeit der täglichen Prüfung von Geburtstagen und Kontaktpflege. |
| `PAUSEN_AKTIV` | `true` | nein | Pausen-Wächter ein/aus (braucht `NEXTCLOUD_KALENDER`). |
| `PAUSEN_KALENDER` | `Pausen` | nein | Kalender für Missas Puffer- und Pausenblöcke (legt sie bei Bedarf an). |
| `PAUSEN_FARBE` | `türkis` | nein | Farbe des Pausen-Kalenders und der Blöcke. |
| `PAUSEN_PUFFER_MINUTEN` | `10` | nein | Mindest-Puffer zwischen zwei Terminen. |
| `PAUSEN_MITTAG_MINUTEN` | `30` | nein | Länge der Mittagspause. |
| `PAUSEN_MITTAG_VON` / `PAUSEN_MITTAG_BIS` | `11:30` / `14:00` | nein | Zeitfenster der Mittagspause. |
| `PAUSEN_MAX_STUNDEN` | `8` | nein | Ab so vielen Stunden Terminen gilt ein Tag als zu voll. |
| `PAUSEN_WEGZEIT_MINUTEN` | `30` | nein | Mindest-Wegzeit zwischen Terminen an verschiedenen Orten. |
| `PAUSEN_PRUEFZEIT` | `19:00` | nein | Abendliche Prüfung des nächsten Tages. |
| `NACHTSCHICHT_VON` / `NACHTSCHICHT_BIS` | `01:00` / `06:00` | nein | Nachtfenster für lange Aufgaben; am Ende kommt der Morgenbericht. Im Gespräch geänderte Zeiten haben Vorrang (siehe [routinen-nachtschicht.md](routinen-nachtschicht.md)). |
| `MONITORING_URL` / `MONITORING_TOKEN` | leer | nein | Eigenes Monitoring-Tool (JSON, Format in [monitoring.md](monitoring.md)); leer = aus. |
| `HOMEPAGE_URL` | leer | nein | Homepage-Dashboard: Status der Dienste mit `siteMonitor`; leer = aus. |
| `PROXMOX_URL` / `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` | leer / `missa@pve!monitor` / leer | nein | Proxmox-API mit nur lesendem Token (PVEAuditor), siehe [server-alarme.md](server-alarme.md); ohne alle drei aus. |
| `PROXMOX_TLS_PRUEFEN` / `PROXMOX_CA_DATEI` | `true` / leer | nein | Zertifikat von Proxmox prüfen, am besten mit der CA aus `/etc/pve/pve-root-ca.pem`. |
| `MONITORING_INTERVALL_SEKUNDEN` | `60` | nein | So oft holt Missa die Messwerte. |
| `ALARM_DAUER_MINUTEN` | `5` | nein | Standard-Dauer einer Alarm-Regel. |
| `ALARM_RUHEFENSTER_MINUTEN` | `60` | nein | Derselbe Alarm kommt frühestens nach so vielen Minuten wieder (im Gespräch änderbar). |
| `ALARM_RUHEZEIT_VON` / `ALARM_RUHEZEIT_BIS` | `22:00` / `07:00` | nein | Ruhezeit für `info` und `wichtig`; Notfälle kommen immer. `aus` = keine Ruhezeit. Im Gespräch änderbar. |
| `AUFFAELLIG_AKTIV` / `AUFFAELLIG_MIN_ABSTAND` | `true` / `15` | nein | Hinweise auf ungewöhnliche Anstiege (Prozentpunkte über dem 7-Tage-Mittel). |
| `ERREICHBARKEIT_DOMAINS` | leer (Vorlage: cloud., git., ki.louis-degiorgi.ch) | nein | Domains, die Missa über die öffentliche Adresse prüft (kommagetrennt); leer = aus. |
| `ERREICHBARKEIT_INTERVALL_MINUTEN` / `ERREICHBARKEIT_STUFE` | `5` / `wichtig` | nein | Prüfabstand und Alarm-Stufe für einen Ausfall. |
| `SSH_HOSTS` | leer | nein | Hosts für Container, Logs und Anmeldungen als `name=adresse[:port]`; leer = aus. Einrichtung siehe [container-logs-logins.md](container-logs-logins.md). |
| `SSH_BENUTZER` / `SSH_SCHLUESSEL` / `SSH_KNOWN_HOSTS` / `SSH_TIMEOUT` | `missa` / `/etc/missa/ssh/id_ed25519` / `/etc/missa/ssh/known_hosts` / `20` | nein | SSH-Zugang (Host-Schlüssel werden streng geprüft). |
| `CONTAINER_INTERVALL_MINUTEN` / `CONTAINER_IGNORIEREN` | `5` / leer | nein | Prüfabstand für hängende Container; Container, die nicht gemeldet werden. |
| `LOGIN_HOSTS` / `LOGIN_INTERVALL_MINUTEN` | leer (= alle) / `5` | nein | SSH-Login-Überwachung. |
| `LOGIN_FEHLVERSUCHE_GRENZE` / `LOGIN_FEHLVERSUCHE_FENSTER_MINUTEN` | `10` / `15` | nein | Ab so vielen Fehlversuchen in so vielen Minuten: `wichtig` (im Gespräch änderbar). |
| `LOGIN_AUTOMATISCH_BENUTZER` / `LOGIN_BEKANNTE_IPS` | `missa` / leer | nein | Automatische Benutzer (keine Zeit-Hinweise); IPs, die als bekannt gelten. |
| `LOGIN_NACHT_VON` / `LOGIN_NACHT_BIS` | `00:00` / `05:00` | nein | Ungewohnte Zeit, solange es wenig Verlauf gibt. |
| `NEXTCLOUD_LOG_HOST` / `NEXTCLOUD_LOG_QUELLE` | leer | nein | Nextcloud-Log für Logins und Fehlersuche: `datei` oder `journal[:Kennung]`. |
| `UPDATE_HOSTS` / `UPDATES_PRUEFZEIT` / `UPDATES_EIGENER_HOST` | leer (= alle) / `06:40` / `ai01` | nein | Update-Verwaltung; die eigene VM kommt beim Einspielen zuletzt. |
| `WARTUNG_TAGE` / `WARTUNG_VON` / `WARTUNG_BIS` / `WARTUNG_DAUER_MINUTEN` | `Sa,So` / `10:00` / `17:00` / `60` | nein | Wann Missa Wartungsfenster vorschlägt. |
| `ZERTIFIKAT_DOMAINS` / `DOMAIN_NAMEN` | leer (= Erreichbarkeits-Domains bzw. daraus abgeleitet) | nein | Zertifikate und Domain-Ablauf (RDAP). |
| `ZERTIFIKAT_WARNUNG_TAGE` / `ZERTIFIKATE_PRUEFZEIT` | `21,7` / `07:10` | nein | Warnschwellen in Tagen und tägliche Prüfzeit. |
| `DNS_ERWARTET` | leer | nein | Erwartete DNS-Einträge als „Name Typ Wert; …“. |
| `RDAP_AKTIV` / `RDAP_BOOTSTRAP_URL` | `true` / IANA-Liste | nein | Ablaufdatum der Domains per RDAP (einzige Abfrage nach außen). |
| `FEHLERSUCHE_DIENSTE` | leer | nein | Zuordnung Dienst → Host und Container/Units, z. B. `nextcloud:int01:nextcloud-app,nextcloud-db`. |
| `NC_TALK_ENABLED` | `false` | für Talk | Missa in Nextcloud Talk einschalten. |
| `NC_TALK_BASE_URL` | – | für Talk | Nextcloud-Adresse wie `overwrite.cli.url` (Prüfung des Absenders). |
| `NC_TALK_API_URL` | = `NC_TALK_BASE_URL` | nein | Adresse für Antworten an Nextcloud, falls ai01 sie intern anders erreicht. |
| `NC_TALK_BOT_SECRET` | – | für Talk | Gemeinsames Bot-Secret (40–128 Zeichen). |
| `NC_TALK_ALLOWED_USERS` | – | für Talk | Nur diese Nextcloud-Benutzer-IDs dürfen mit Missa sprechen (z. B. `louis,admin`); leer = alles ignorieren. |
| `NC_TALK_TIMEOUT` | `300` | nein | Höchstdauer einer Talk-Antwort in Sekunden. |
| `NC_TALK_NOTIFY_CONVERSATION` | – | nein | Talk-Unterhaltung (Token), in die Missa von sich aus schreiben darf. |
| `NC_TALK_NOTIFY_RATE` | `10` | nein | Höchstens so viele eigene Talk-Nachrichten pro Minute. |
| `MAIL_IMAP_HOST` | – | für E-Mail | IMAP-Server des eigenen Mailkontos (TLS, Port `MAIL_IMAP_PORT`, Standard `993`). |
| `MAIL_SMTP_HOST` | – | für E-Mail | SMTP-Server zum Senden. Port `MAIL_SMTP_PORT`: `465` = TLS (Standard), sonst STARTTLS (z. B. `587`). |
| `MAIL_USER` | – | für E-Mail | Benutzername des Mailkontos (meist die Adresse). |
| `MAIL_PASSWORD` | – | für E-Mail | Passwort bzw. App-Passwort des Mailkontos – nur hier, nie im Repo. |
| `MAIL_ABSENDER` | = `MAIL_USER` | nein | Absender, z. B. `Louis Degiorgi <louis@example.ch>`. |
| `MAIL_ORDNER_ENTWUERFE` | `Drafts` | nein | IMAP-Ordner für Entwürfe (so wie er auf dem Server heißt). |
| `MAIL_ORDNER_GESENDET` | `Sent` | nein | IMAP-Ordner für gesendete Mails (Quelle für das Stilprofil). |
| `MAIL_STIL_DATEI` | `MISSA_DATEN/schreibstil.md` | nein | Bearbeitbare Datei mit deinem Schreibstil-Profil. |

Fehlen `NEXTCLOUD_URL`, `NEXTCLOUD_USER` oder `NEXTCLOUD_APP_PASSWORD`, bietet Missa
keine Kalender- und Aufgaben-Werkzeuge an. Das Gedächtnis ist immer da. Nextcloud Talk ist
nur an, wenn `NC_TALK_ENABLED=true` und Adresse und Secret gesetzt sind – Einrichtung:
[nextcloud-talk.md](nextcloud-talk.md). E-Mail ist nur an, wenn `MAIL_IMAP_HOST`, `MAIL_SMTP_HOST`,
`MAIL_USER` und `MAIL_PASSWORD` gesetzt sind – siehe [email.md](email.md).

Beim Start von Hand (ohne systemd) liest `missa-core` die Datei `/etc/missa/missa.env`
selbst; ein anderer Ort geht mit `MISSA_ENV_DATEI=/pfad/zur/datei`. Schon gesetzte
Umgebungsvariablen haben Vorrang.

## 5. Dienst einrichten und starten

```bash
sudo install -m 644 /opt/missa/app/deploy/missa-core.service /etc/systemd/system/missa-core.service
```

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now missa-core
```

`enable` sorgt dafür, dass der Dienst nach jedem Neustart von ai01 automatisch startet.
Die Unit startet nach `llama-server.service` (falls es diese Unit gibt), startet
llama-server aber nicht selbst. Stürzt Missa-Core ab, startet systemd ihn nach 5 Sekunden
neu (`Restart=on-failure`).

Status und Logs:

```bash
systemctl status missa-core
```

```bash
journalctl -u missa-core -f
```

## 6. Prüfen

```bash
curl http://127.0.0.1:8000/health
```

Erwartet: `{"status":"ok", ..., "llama":"ok", "charakter":"ok", "werkzeuge":[...]}`.
Antwortet llama-server nicht, kommt HTTP 503 mit `"llama":"nicht erreichbar"`.

Eine erste Frage (Schlüssel aus `/etc/missa/missa.env` einsetzen):

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer DEIN_MISSA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "missa", "messages": [{"role": "user", "content": "Hallo Missa, wer bist du?"}]}'
```

Neustart-Test:

```bash
sudo reboot
```

Nach dem Hochfahren:

```bash
systemctl is-enabled missa-core && systemctl is-active missa-core
```

Erwartet: `enabled` und `active`.

## 7. Bedienung im Alltag

- **Charakter ändern:** `sudo nano /etc/missa/charakter.md` – wirkt bei der nächsten Anfrage, kein Neustart nötig.
- **Konfiguration ändern** (`missa.env`): danach `sudo systemctl restart missa-core`.
- **Gedächtnis prüfen:** `GET /api/gedaechtnis` (siehe [api.md](api.md)).
- **Sicherung:** Alle Daten stecken in `/opt/missa/data/missa.db`. Sicher kopieren, auch bei
  laufendem Dienst (braucht das Paket `sqlite3`):

  ```bash
  sudo -u missa sqlite3 /opt/missa/data/missa.db ".backup /opt/missa/data/missa-sicherung.db"
  ```

## 8. Aktualisieren

Ab Version 0.2.0 mit einem Befehl:

```bash
sudo /opt/missa/app/scripts/update.sh
```

Das Script holt den neuesten Release-Tag, zeigt die Neuerungen, installiert, ergänzt neue
Einstellungen, startet neu, prüft `/health` und springt bei Fehlern automatisch zurück.
Ersteinrichtung, Optionen und Einzelheiten: [update.md](update.md).

## 9. Fehlersuche

| Anzeichen | Ursache und Lösung |
|-----------|-------------------|
| Dienst startet nicht, Log: `Konfigurationsfehler: MISSA_API_KEY ist nicht gesetzt` | `MISSA_API_KEY` in `/etc/missa/missa.env` eintragen. |
| `/health` liefert 503, `"llama":"nicht erreichbar"` | llama-server läuft nicht oder `LLAMA_URL` stimmt nicht: `curl http://127.0.0.1:8080/health`. |
| Apps bekommen 401 | Falscher oder fehlender Header `Authorization: Bearer …`. |
| Missa sagt, Nextcloud lehne die Anmeldung ab | `NEXTCLOUD_USER` / `NEXTCLOUD_APP_PASSWORD` prüfen, ggf. neues App-Passwort erstellen. |
| Missa sagt, es gebe keinen Kalender „…“ | Die Meldung nennt die vorhandenen Namen – `NEXTCLOUD_KALENDER` bzw. `NEXTCLOUD_AUFGABEN_LISTE` genau so eintragen. |
| `/health` meldet `"charakter":"fehlt"` | `/etc/missa/charakter.md` fehlt oder ist für `missa` nicht lesbar (Rechte `root:missa 640`). |
| Antworten brechen nach langer Zeit ab | `LLAMA_TIMEOUT` erhöhen; auch den Timeout in Caddy und in der App großzügig setzen. |
