# Missa in Nextcloud Talk

Missa kann in einer Nextcloud-Talk-Unterhaltung mitschreiben. Talk ist dabei nur ein
weiterer **Kanal**: Es antwortet dieselbe Missa mit demselben Charakter, demselben
Gedächtnis und denselben Werkzeugen (Kalender, Aufgaben) wie über die API.

Missa reagiert **ausschließlich** auf die Nextcloud-Accounts in `NC_TALK_ALLOWED_USERS`
(gedacht: Louis' Account und der Admin-Account). Nachrichten aller anderen – auch Gäste,
föderierte Benutzer und Bots – ignoriert sie vollständig: keine Antwort, keine Reaktion,
kein Modellaufruf, nichts im Gesprächsverlauf oder Gedächtnis. Es entsteht nur ein
Log-Eintrag (Absender, Unterhaltung, Zeit – ohne Nachrichteninhalt).

Technisch nutzt Missa die offizielle Bot-Schnittstelle von Talk (Webhook-Bots, ab
Nextcloud 27.1 / Talk 17.1): https://nextcloud-talk.readthedocs.io/en/latest/bots/

## So funktioniert es

```
Louis schreibt in Talk ──► Nextcloud (int01) ──POST Webhook──► Missa-Core (ai01)
                                                                 │ 1. Signatur + Absender prüfen
                                                                 │ 2. sofort „200 OK“ an Talk
                                                                 │ 3. Reaktion 👀
                                                                 │ 4. Missa denkt nach (llama-server)
Antwort erscheint in Talk ◄── Bot-API (int01) ◄──POST Antwort────┘ 5. Antwort als Reply, 👀 weg
```

- Jeder Webhook ist mit dem gemeinsamen **Bot-Secret** signiert (HMAC-SHA256). Falsche
  Signatur oder fremder Absender-Server → `401`, sonst passiert nichts.
- Talk wartet nur 5 Sekunden auf den Webhook. Missa antwortet darum sofort und arbeitet im
  Hintergrund weiter; das 👀 zeigt, dass sie dran ist.
- Die Antwort kommt als Antwort auf die ursprüngliche Nachricht (Zitat). Markdown wird
  angezeigt. Antworten über 32 000 Zeichen kommen in mehreren Nachrichten (`Teil 1/2` …).
- Dauert es länger als `NC_TALK_TIMEOUT` (Standard 300 s) oder ist das Sprachmodell nicht
  erreichbar, schreibt Missa eine kurze Meldung statt still zu verschwinden.
- Gesprächsverlauf: Missa-Core führt den Verlauf pro Sitzung. Talk bekommt einen eigenen
  Verlauf **pro Unterhaltung** (Sitzung `nextcloud-talk:<token>`, letzte 20 Nachrichten).
  Das **Gedächtnis** („Merk dir …“) ist für alle Kanäle dasselbe.

## Einrichtung

### 1. Update auf ai01

```bash
sudo /opt/missa/app/scripts/update.sh
```

Das Update ergänzt in `/etc/missa/missa.env` die neuen Einstellungen (alle zunächst leer bzw.
`NC_TALK_ENABLED=false`) und listet sie am Ende auf.

### 2. Die eigene Nextcloud-Benutzer-ID finden

Die **Benutzer-ID** ist nicht unbedingt der Anzeigename. Auf int01:

```bash
sudo -u www-data php /var/www/nextcloud/occ user:list
```

Ausgabe z. B. `  - louis: Louis Degiorgi` – die ID ist der Teil **vor** dem Doppelpunkt
(`louis`). Genauso die ID des Admin-Accounts ablesen. (Läuft Nextcloud in Docker:
`docker exec -u www-data <container> php occ user:list`.)

### 3. Secret erzeugen

Auf einem beliebigen Rechner:

```bash
openssl rand -hex 32
```

Das ergibt 64 Zeichen (Talk verlangt 40 bis 128). Derselbe Wert kommt gleich in die
`missa.env` **und** in `occ talk:bot:install`. Nicht in Chats, Tickets oder ins Repo kopieren.

### 4. Werte in `/etc/missa/missa.env` eintragen (ai01)

```bash
sudo nano /etc/missa/missa.env
```

```ini
NC_TALK_ENABLED=true
# genau wie overwrite.cli.url in der config.php von Nextcloud:
NC_TALK_BASE_URL=https://cloud.louis-degiorgi.ch:8443
# optional, wenn ai01 Nextcloud intern anders erreicht (siehe „Netzwerk“):
NC_TALK_API_URL=
NC_TALK_BOT_SECRET=<der Wert aus Schritt 3>
# nur diese Accounts dürfen mit Missa sprechen – echte IDs aus Schritt 2:
NC_TALK_ALLOWED_USERS=louis,admin
NC_TALK_TIMEOUT=300
```

| Variable | Bedeutung |
|----------|-----------|
| `NC_TALK_ENABLED` | `true` schaltet Talk ein. Standard `false`. |
| `NC_TALK_BASE_URL` | Adresse von Nextcloud **genau wie `overwrite.cli.url`** in `config/config.php` von Nextcloud. Talk schickt diesen Wert im Header `X-Nextcloud-Talk-Backend` mit; stimmt er nicht, lehnt Missa den Webhook ab. Ohne diesen Wert bleibt Talk aus. |
| `NC_TALK_API_URL` | Optional: Adresse, über die ai01 die Bot-API für Antworten aufruft, falls anders als `NC_TALK_BASE_URL` (z. B. direkt intern). Leer = `NC_TALK_BASE_URL`. |
| `NC_TALK_BOT_SECRET` | Gemeinsames Geheimnis des Bots (40–128 Zeichen). Ohne Secret bleibt Talk aus. |
| `NC_TALK_ALLOWED_USERS` | Kommagetrennte Nextcloud-**Benutzer-IDs**, die mit Missa sprechen dürfen, z. B. `louis,admin`. Exakter Vergleich (Groß/Klein zählt). **Leer = Missa ignoriert alles** (Warnung beim Start). |
| `NC_TALK_TIMEOUT` | Höchstdauer einer Antwort in Sekunden (Standard 300). |

Danach neu starten und im Log prüfen:

```bash
sudo systemctl restart missa-core
```

```bash
journalctl -u missa-core -n 20 | grep -i talk
```

Erwartet: `Nextcloud Talk ist an: Webhook /channels/nextcloud-talk/webhook, erlaubte Benutzer: …`.

### 5. Bot in Nextcloud installieren (int01)

Mit der Vorlage `scripts/talk-bot-einrichten.sh` aus diesem Repo (auf int01 kopieren) oder
direkt mit `occ`. Die Webhook-Adresse ist die **interne** Adresse von ai01:

```bash
sudo -u www-data php /var/www/nextcloud/occ talk:bot:install \
  --feature webhook --feature response --feature reaction \
  "Missa" "<SECRET aus Schritt 3>" \
  "http://<IP-von-ai01>:8000/channels/nextcloud-talk/webhook" \
  "Persönliche Assistentin von Louis"
```

Tipp: Mit einem Leerzeichen vor dem Befehl landet das Secret in den meisten Shells nicht in
der History. Oder die Vorlage benutzen, sie fragt das Secret verdeckt ab:

```bash
./talk-bot-einrichten.sh installieren
```

Die Bot-ID ablesen:

```bash
sudo -u www-data php /var/www/nextcloud/occ talk:bot:list
```

### 6. Unterhaltung „Missa“ anlegen und den Bot dort aktivieren

1. In Talk eine **Gruppenunterhaltung** „Missa“ anlegen (Bots funktionieren nicht im
   1:1-Chat) und **nur** Louis' Account und den Admin-Account hinzufügen.
2. Die Unterhaltung öffnen; der **Token** steht in der Adresse:
   `https://cloud.louis-degiorgi.ch:8443/call/`**`abc123xy`**.
3. Bot aktivieren:

   ```bash
   sudo -u www-data php /var/www/nextcloud/occ talk:bot:setup <bot-id> <token>
   ```

   (oder `./talk-bot-einrichten.sh einrichten <bot-id> <token>`)

> **Achtung, Mitleser:** Missa antwortet zwar nur Louis und dem Admin – aber ihre Antworten
> erscheinen in der Unterhaltung. Wer sonst Mitglied der Unterhaltung ist, könnte sie lesen.
> Darum keine weiteren Personen in die Unterhaltung „Missa“ aufnehmen und sie nicht
> öffentlich (per Link) teilen.

### 7. Ausprobieren

In der Unterhaltung „Missa“ schreiben: „Hallo Missa, was habe ich morgen?“ – nach kurzer Zeit
erscheint 👀 an der Nachricht, dann die Antwort als Zitat-Antwort, und das 👀 verschwindet.

## Netzwerk und Firewall (ZyWALL)

Nextcloud und ai01 sprechen **direkt im internen Netz** miteinander; der Webhook muss nicht
aus dem Internet erreichbar sein und läuft nicht über Caddy.

| Richtung | Von → Nach | Port | Wozu |
|----------|-----------|------|------|
| Webhook | int01 (10.0.30.10) → ai01 | TCP `MISSA_PORT` (Standard 8000) | Talk schickt Nachrichten an Missa |
| Antworten | ai01 → int01 (10.0.30.10) | TCP 443 bzw. 80 (der Port, auf dem Nextcloud **intern** lauscht) | Missa schickt Antworten und Reaktionen |

Auf der ZyWALL also zwei Regeln anlegen, jeweils nur für diese Quell-/Ziel-Adressen und Ports.
Diese Regeln richtet Louis selbst ein; Missa-Core verändert nichts an Nextcloud, Caddy oder
der Firewall.

**Welche Adresse für Antworten?** Missa ruft `NC_TALK_API_URL` (oder `NC_TALK_BASE_URL`) +
`/ocs/v2.php/apps/spreed/api/v1/bot/…` auf. Zwei Möglichkeiten:

- **Öffentlicher Name, intern aufgelöst (empfohlen):** `NC_TALK_API_URL` leer lassen und auf
  ai01 den Namen auf int01 zeigen lassen, z. B. in `/etc/hosts`:
  `10.0.30.10 cloud.louis-degiorgi.ch`. Dann muss int01 den Port aus `NC_TALK_BASE_URL`
  (hier 8443) mit dem gültigen Zertifikat bedienen – sonst diese Variante nicht nehmen.
- **Direkt intern:** `NC_TALK_API_URL=http://10.0.30.10` (bzw. der interne Port). Dann muss
  `10.0.30.10` in `trusted_domains` der Nextcloud-`config.php` stehen, sonst antwortet
  Nextcloud mit einer Fehlerseite.

Mit `curl` von ai01 aus testen (erwartet: eine JSON-Antwort, keine Fehlerseite):

```bash
curl -s -H "OCS-APIRequest: true" "https://cloud.louis-degiorgi.ch:8443/ocs/v2.php/cloud/capabilities?format=json" | head -c 200
```

**Interne Adresse wird blockiert?** Talk erlaubt für Bot-Webhooks interne Adressen
normalerweise selbst. Zeigt `occ talk:bot:list -v` (Spalte `last_error_message`) trotzdem
Fehler wie „violates local access rules“, in der `config.php` von Nextcloud ergänzen:

```php
'allow_local_remote_servers' => true,
```

## Fehlersuche

| Anzeichen | Ursache und Lösung |
|-----------|-------------------|
| Kein 👀, keine Antwort | Log auf ai01 ansehen: `journalctl -u missa-core -f`. Steht dort nichts, kommt der Webhook nicht an → Firewall-Regel int01 → ai01, Webhook-Adresse in `occ talk:bot:list -v`, ist der Bot mit `talk:bot:setup` in der Unterhaltung aktiv (`occ talk:bot:list <token>`)? |
| Log: `Webhook mit ungültiger Signatur abgelehnt` | `NC_TALK_BOT_SECRET` stimmt nicht mit dem Secret aus `talk:bot:install` überein. |
| Log: `Webhook von unbekanntem Backend '…' abgelehnt` | `NC_TALK_BASE_URL` an den Wert im Log angleichen (das ist `overwrite.cli.url` von Nextcloud). |
| Log: `ignoriert – Absender users/… ist nicht erlaubt` | Die Benutzer-ID steht nicht (genau so) in `NC_TALK_ALLOWED_USERS`. |
| Log: `Antwort konnte nicht gesendet werden` | ai01 erreicht die Bot-API nicht → Firewall ai01 → int01, `NC_TALK_API_URL`, `trusted_domains`. |
| `occ talk:bot:list` zeigt einen `error_count` | Talk zählt jede Webhook-Zustellung, die nicht mit 200 beantwortet wird. `occ talk:bot:list -v` zeigt die letzte Fehlermeldung. Läuft `missa-core`? Ist Talk in `missa.env` eingeschaltet (sonst gibt es den Webhook nicht und Missa antwortet 404)? |

## Datenschutz und Logs

- Missa protokolliert Eingang, Absender, ob erlaubt oder ignoriert, Unterhaltung und Dauer.
- Inhalte ignorierter Nachrichten, das Secret und Signaturen werden **nie** geloggt; auch bei
  erlaubten Nachrichten steht nur die Länge im Log.
- Erlaubte Nachrichten und Missas Antworten landen im Gesprächsverlauf (`missa.db`, Sitzung
  `nextcloud-talk:<token>`), genau wie bei der API.
