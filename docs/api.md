# API von Missa-Core

Missa-Core spricht das **OpenAI-Chat-Format**. Jede App, die mit der OpenAI-API umgehen
kann, kann sich mit Missa verbinden – sie muss nur Basis-URL, Schlüssel und Modellname
kennen. Missas Charakter, ihre Werkzeuge (Kalender, Aufgaben, Gedächtnis) und der
Gesprächsverlauf laufen komplett auf dem Server; die App schickt nur Text.

## Auf einen Blick

| Was | Wert |
|-----|------|
| Basis-URL (intern) | `http://ai01:8000` |
| Basis-URL (über Caddy, HTTPS) | die Adresse, die Caddy für Missa-Core weiterleitet, z. B. `https://missa.example.ch` |
| OpenAI-Basis-URL für Apps | Basis-URL + `/v1`, z. B. `https://missa.example.ch/v1` |
| Anmeldung | Header `Authorization: Bearer <MISSA_API_KEY>` |
| Modell | `missa` |
| Sitzung (Gesprächsverlauf) | Header `X-Missa-Session: <id>` (oder Feld `user`) |
| Antworten zum Vorlesen | Header `X-Missa-Modus: sprache` (optional) |
| Format | JSON, UTF-8; Streaming als Server-Sent Events |

In den Beispielen steht `$MISSA` für die Basis-URL und `$KEY` für den API-Schlüssel:

```bash
export MISSA=http://ai01:8000 KEY=dein-missa-api-key
```

## Anmeldung

Alle Endpunkte außer `/health` brauchen den Header

```
Authorization: Bearer <MISSA_API_KEY>
```

Fehlt er oder ist der Schlüssel falsch, antwortet Missa-Core mit **401** und dem Header
`WWW-Authenticate: Bearer`:

```json
{"error": {"message": "Fehlender oder ungültiger API-Schlüssel (Authorization: Bearer …)", "type": "authentication_error", "code": null}}
```

Die Anmeldung wird geprüft, *bevor* der Inhalt einer Anfrage gelesen wird. Nach 10
Fehlversuchen innerhalb von 5 Minuten ist die Adresse 15 Minuten gesperrt (**429**,
`Retry-After`); jeder Fehlversuch steht mit Adresse im Log. Anfragen über 20 MB lehnt
Missa-Core mit **413** ab. Eine öffentliche API-Beschreibung (`/docs`, `/redoc`,
`/openapi.json`) gibt es nicht.

## Sprechmodus: Antworten zum Vorlesen

Liest eine App Missas Antworten laut vor (Sprachassistent, Auto, Lautsprecher), schickt sie
zusätzlich den Header:

```http
X-Missa-Modus: sprache
```

Dann antwortet Missa so, dass es sich vorgelesen gut anhört: kurze Sätze, keine Listen,
kein Markdown, keine Emojis, keine Links oder Code, und Zahlen, Uhrzeiten und Daten so, wie
man sie spricht („halb acht“, „am dritten März“). Gross- und Kleinschreibung des Werts ist
egal; andere Werte und ein fehlender Header ändern nichts – dann ist alles wie bisher.
Rückfragen vor Zerstörerischem und alle anderen Sicherheitsregeln gelten im Sprechmodus
genauso.

```bash
curl -s "$MISSA/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Missa-Session: auto-louis" \
  -H "X-Missa-Modus: sprache" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Was steht heute an?"}]}'
```

## Sitzungen: Missa erinnert sich an das Gespräch

Damit Missa weiß, worüber ihr gerade sprecht, schickt die App eine **Sitzungs-ID** mit:

- Header `X-Missa-Session: handy-louis` (empfohlen) **oder**
- Feld `"user": "handy-louis"` im JSON (der Header hat Vorrang).

Die ID ist frei wählbar (höchstens 200 Zeichen), z. B. eine pro Gerät oder pro
Gesprächsfaden. Eine neue ID beginnt ein neues Gespräch.

So funktioniert es:

- **Schickt die App nur die neue Nachricht** (eine `user`-Nachricht), fügt Missa-Core die
  letzten 20 gespeicherten Nachrichten dieser Sitzung davor ein. Das ist der einfachste Weg.
- **Schickt die App ihren eigenen Verlauf mit** (mehrere Nachrichten), nimmt Missa-Core
  genau diesen und fügt nichts doppelt ein.
- In beiden Fällen speichert Missa-Core die letzte Frage und ihre Antwort in der Sitzung.
- **Ohne Sitzungs-ID** wird nichts gespeichert; jede Anfrage steht für sich.

Das **Gedächtnis** (Fakten, die sich Missa mit „Merk dir …“ gemerkt hat) gilt immer, über
alle Sitzungen hinweg.

## Endpunkte

### `GET /health` – läuft alles?

Ohne Anmeldung. **200**, wenn llama-server antwortet, sonst **503**.

```bash
curl $MISSA/health
```

```json
{
  "status": "ok",
  "version": "1.1.0",
  "llama": "ok",
  "charakter": "ok",
  "werkzeuge": ["merken", "erinnern", "vergessen", "termine_anzeigen", "termin_eintragen", "termin_loeschen", "aufgaben_anzeigen", "aufgabe_erstellen", "aufgabe_erledigt", "aufgabe_loeschen"]
}
```

Bei Störung: `"status": "gestoert"`, `"llama": "nicht erreichbar"`. `"charakter": "fehlt"`
heißt, die Charakterdatei ist nicht lesbar (Missa antwortet dann mit einem Ersatztext).

Ist Missa Control angebunden ([missa-control.md](missa-control.md)), kommt zusätzlich
`"missa_control": {"verbunden": true, "funktionen": 12, "letzte_aktualisierung": "…", "fehler": null}`
dazu. Ein ausgefallenes Panel macht `/health` nicht zu 503 – Missa arbeitet ohne es weiter.
Den genauen Fehlertext unter `fehler` gibt es nur mit Anmeldung (Bearer-Header); ohne steht
dort nur `"Fehler (Details nur mit Anmeldung)"`.

Ist der Geräte-Anschluss an (`MISSA_GERAETE_AKTIV=true`, siehe [geraete.md](geraete.md)),
kommt **nur mit Anmeldung** zusätzlich
`"geraete": [{"name": "laptop", "online": true, "werkzeuge": ["rechner_status", …], "seit": "…"}]`
dazu (`seit`: seit wann der Zustand online/offline gilt).

### `GET /v1/models` – verfügbare Modelle

```bash
curl $MISSA/v1/models -H "Authorization: Bearer $KEY"
```

```json
{"object": "list", "data": [{"id": "missa", "object": "model", "created": 1794253802, "owned_by": "missa"}]}
```

`GET /v1/models/missa` liefert nur den Eintrag `missa`; andere Namen ergeben 404.

### `POST /v1/chat/completions` – mit Missa sprechen

**Anfrage:**

```bash
curl $MISSA/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "X-Missa-Session: handy-louis" \
  -d '{"model": "missa", "messages": [{"role": "user", "content": "Was habe ich morgen?"}]}'
```

**Antwort:**

```json
{
  "id": "chatcmpl-5d949a9da1c949f48333a7ff1daf1a74",
  "object": "chat.completion",
  "created": 1794253812,
  "model": "missa",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "Morgen hast du um 14:00 den Zahnarzt in Bern."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 812, "completion_tokens": 18, "total_tokens": 830}
}
```

Die Antwort steht in `choices[0].message.content`.

**Felder der Anfrage:**

| Feld | Pflicht | Bedeutung |
|------|---------|-----------|
| `messages` | ja | Liste von Nachrichten `{"role": "...", "content": "..."}`. Mindestens eine Nachricht mit `role: "user"`. `content` darf Text oder eine Liste von Teilen im OpenAI-Format sein. |
| `model` | nein | Am besten `"missa"`; andere Werte werden ignoriert. |
| `stream` | nein | `true` für Streaming (siehe unten), Standard `false`. |
| `user` | nein | Sitzungs-ID, falls kein Header `X-Missa-Session` gesetzt ist. |
| `temperature`, `top_p`, `top_k`, `min_p`, `max_tokens`, `max_completion_tokens`, `stop`, `seed`, `presence_penalty`, `frequency_penalty`, `repeat_penalty` | nein | Werden an llama-server durchgereicht. |

Gut zu wissen:

- **system-Nachrichten der App** werden *hinter* Missas Charakter angehängt („Zusätzliche
  Hinweise der App“). Sie können Missa z. B. bitten, kürzer zu antworten, ersetzen ihren
  Charakter aber nie.
- **Eigene `tools` der App** werden ignoriert. Missa benutzt nur ihre eigenen Werkzeuge,
  und die führt der Server aus – die App bekommt immer eine fertige Textantwort.
- **Manches braucht ein Ja:** Bittet man Missa, eine Aufgabe, einen Fakt oder einen Termin
  zu löschen, einen Termin zu verschieben, einen Termin trotz Überschneidung einzutragen oder
  etwas zu ändern, bei dem andere Personen eine E-Mail bekämen, fragt sie zuerst nach.
  Ausgeführt wird erst, wenn in einer *nächsten* Nachricht ein Ja kommt. Dafür muss die App
  die Sitzung beibehalten (gleiche `X-Missa-Session`) oder den Verlauf mitschicken.
- **Geduld:** Das Modell läuft auf der CPU. Eine Antwort kann einige Sekunden bis Minuten
  dauern, besonders wenn Missa Werkzeuge benutzt. Den Timeout der App (und von Caddy)
  großzügig setzen, z. B. 10 Minuten, oder Streaming verwenden.

### Streaming (`"stream": true`)

```bash
curl -N $MISSA/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "X-Missa-Session: handy-louis" \
  -d '{"model": "missa", "stream": true, "messages": [{"role": "user", "content": "Erzähl mir einen Witz"}]}'
```

Die Antwort kommt als Server-Sent Events (`Content-Type: text/event-stream`), genau wie
bei OpenAI:

```
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "created": 1794253812, "model": "missa", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": null}]}

data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "created": 1794253812, "model": "missa", "choices": [{"index": 0, "delta": {"content": "Warum "}, "finish_reason": null}]}

data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "created": 1794253812, "model": "missa", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}

data: [DONE]
```

- Den Text ergeben alle `choices[0].delta.content` hintereinander.
- Benutzt Missa ein Werkzeug, entsteht eine Pause im Stream; danach geht der Text weiter.
- Tritt unterwegs ein Fehler auf (z. B. llama-server weg), kommt ein Ereignis
  `data: {"error": {"message": "…", "type": "llama_fehler"}}` und danach `data: [DONE]`.

### `GET /api/gedaechtnis` – was weiß Missa über mich?

Listet alle gespeicherten Fakten, damit Louis sie prüfen kann. Optional nur eine Kategorie:
`?kategorie=termine|vorlieben|projekte|sonstiges`.

```bash
curl $MISSA/api/gedaechtnis -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "fakten": [
    {"id": 1, "text": "Louis trinkt Kaffee ohne Zucker", "kategorie": "vorlieben", "erstellt": "2026-11-09T21:05:06+01:00"}
  ]
}
```

Fakten löscht man, indem man Missa im Chat bittet, etwas zu vergessen („Vergiss das mit
dem Kaffee“) und dann bestätigt.

### `GET /api/erinnerungen` – welche Erinnerungen sind geplant?

Listet alle offenen Erinnerungen, die nächste zuerst, dazu die Kanäle, über die Missa
zustellen kann (leer = kein Kanal eingerichtet). Zeiten in `TIMEZONE`.

```bash
curl $MISSA/api/erinnerungen -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "kanaele": ["nextcloud-talk"],
  "erinnerungen": [
    {"id": 1, "text": "Training", "nachricht": null, "faellig": "2026-11-11T06:30:00+01:00",
     "wiederholung": "woechentlich:2", "wiederholung_text": "jeden Mittwoch", "status": "offen",
     "erstellt": "2026-11-10T19:00:12+01:00", "zugestellt": null, "fehlversuche": 0,
     "kanal": null, "herkunft": "chat"}
  ]
}
```

Anlegen, ändern und löschen geht im Chat („Erinnere mich um 6:30 an Training“), siehe
[erinnerungen.md](erinnerungen.md). Seit dem Organisations-Update hat jeder Eintrag zusätzlich
die Felder `art` (`erinnerung`), `anweisung` und `pausiert`; Routinen erscheinen hier nicht.

### `GET /api/routinen` – welche Routinen laufen?

Alle Routinen (auch pausierte), nächster Lauf zuerst. Gleiche Felder wie bei
`/api/erinnerungen`, mit `art: "routine"`, der `anweisung` an Missa und `pausiert`.

```bash
curl $MISSA/api/routinen -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "routinen": [
    {"id": 4, "text": "Wochenrückblick", "art": "routine", "pausiert": false,
     "anweisung": "Schreib mir einen Wochenrückblick", "faellig": "2026-11-13T17:00:00+01:00",
     "wiederholung": "woechentlich:4", "wiederholung_text": "jeden Freitag", "status": "offen"}
  ]
}
```

### `GET /api/nachtschicht` – Nachtfenster und Nachtaufgaben

Das aktuelle Nachtfenster, wartende Nachtaufgaben und die Ergebnisse der letzten 7 Tage.

```bash
curl $MISSA/api/nachtschicht -H "Authorization: Bearer $KEY"
```

```json
{
  "fenster": {"von": "01:00", "bis": "06:00"},
  "anzahl": 1,
  "auftraege": [
    {"id": 2, "titel": "Aufgaben-Übersicht", "anweisung": "Fasse alle offenen Aufgaben zusammen",
     "art": "nacht", "status": "fertig", "ergebnis": "…", "fehler": null, "versuche": 1,
     "erstellt": "2026-11-10T21:03:00+01:00", "gestartet": "2026-11-11T01:00:02+01:00",
     "beendet": "2026-11-11T01:07:40+01:00", "gemeldet": "2026-11-11T06:00:01+01:00"}
  ]
}
```

Siehe [routinen-nachtschicht.md](routinen-nachtschicht.md).

### `POST /api/kontakte/kontakt` – Kontakt vermerken (für die Telefonie)

Vermerkt den letzten Kontakt mit einer Person – gedacht für die spätere Telefonie oder eine
App. Die Person wird über `telefon`, `email` oder `name` gefunden.

```bash
curl -X POST $MISSA/api/kontakte/kontakt -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"telefon": "+41 79 000 00 00", "zeit": "2026-11-12T18:30:00+01:00", "art": "Anruf"}'
```

```json
{"vermerkt": true, "name": "Maria Degiorgi", "letzter_kontakt": "2026-11-12", "art": "Anruf"}
```

`vermerkt: false`, wenn niemand passt; `400` ohne Angaben, `503` ohne Kontaktpflege.

### `GET /api/kalender/protokoll` – was hat Missa am Kalender geändert?

Alle Kalenderänderungen von Missa, die neueste zuerst (höchstens `?anzahl=50`, bis 500), mit
dem Zustand vorher und nachher (iCalendar-Text bei Terminen, JSON bei Kalendern). Rückgängig
machen geht im Chat („mach das rückgängig“), siehe [kalender.md](kalender.md).

```bash
curl "$MISSA/api/kalender/protokoll?anzahl=10" -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "aenderungen": [
    {"id": 1, "zeit": "2026-11-10T19:02:11+01:00", "aktion": "termin_geaendert",
     "kalender": "Persönlich", "uid": "4F2C…", "beschreibung": "Mi 11.11.2026 10:00–11:00: Meeting … → Mi 11.11.2026 15:00–16:00: Meeting | Farbe: rot …",
     "vorher": "BEGIN:VCALENDAR…", "nachher": "BEGIN:VCALENDAR…", "rueckgaengig": false}
  ]
}
```

`aktion` ist `termin_neu`, `termin_geaendert`, `termin_geloescht`, `kalender_neu` oder
`kalender_geaendert`.

### `GET /api/aktionen` – Aktionsprotokoll (nur lesen)

Jeder Werkzeug-Aufruf von Missa, die neueste zuerst: Zeit, Kanal (`api`, `talk`,
`hintergrund`, `ai01`), Account, Werkzeug, Stufe (`lesen`, `schreiben`, `zerstoererisch`),
Parameter (Passwörter und Tokens geschwärzt als `***`), ob Louis bestätigt hat, Status und
gekürztes Ergebnis. Dazu der Zustand des Notfall-Stopps. Der Endpunkt ist nur lesend – es gibt
kein POST/DELETE, und den Notfall-Stopp kann man darüber nicht aufheben.

Filter (alle optional): `von` und `bis` als Datum `JJJJ-MM-TT` (einschließlich), `anzahl`
(Standard 100, höchstens 1000), `werkzeug`, `status` (`ausgefuehrt`, `fehlgeschlagen`,
`wartet_auf_bestaetigung`, `gesperrt_notfall`, `abgelehnt`, `ungueltig`, `notfall_stopp`,
`notfall_freigabe`).

```bash
curl "$MISSA/api/aktionen?von=2026-11-11&bis=2026-11-11" -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "notfall": {"aktiv": false},
  "aktionen": [
    {"id": 7, "zeit": "2026-11-11T08:12:03+01:00", "kanal": "talk", "account": "louis",
     "werkzeug": "aufgabe_loeschen", "stufe": "zerstoererisch", "parameter": {"uid": "…"},
     "bestaetigt": true, "status": "ausgefuehrt", "ergebnis": "Aufgabe „Milch“ gelöscht."}
  ]
}
```

Mehr dazu: [rechte.md](rechte.md).

### `GET /api/alarme` – welche Alarme gab es?

Gemeldete Alarme (neueste zuerst) mit Stufe (`info`, `wichtig`, `notfall`), dazu alle
Alarm-Regeln, gerade ausgelöste Regeln, Ruhezeit und Ruhefenster. `?tage=7` zeigt die letzten
7 Tage (Standard 1, höchstens 90). Nur lesend; Regeln legt man im Chat an.

```bash
curl "$MISSA/api/alarme?tage=7" -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "ruhezeit": {"von": "22:00", "bis": "07:00"},
  "ruhefenster_minuten": 60,
  "regeln": [
    {"id": 1, "ziel": "ai01", "messwert": "cpu", "richtung": "ueber", "grenze": 90.0,
     "einheit": "%", "dauer_minuten": 5.0, "stufe": "notfall", "ruhefenster_minuten": null,
     "text": "#1: ai01: CPU über 90 % länger als 5 Minuten → notfall"}
  ],
  "aktiv": ["ai01: CPU über 90 % seit 14:02 Uhr (notfall)"],
  "alarme": [
    {"id": 3, "zeit": "2026-11-11T14:07:00+01:00", "stufe": "notfall",
     "schluessel": "regel:1:ai01", "titel": "ai01: CPU über 90 %",
     "text": "Seit 14:02 Uhr, aktuell 95 % (Regel #1, Quelle proxmox).", "quelle": "proxmox",
     "status": "gesendet", "zustellen_ab": "2026-11-11T14:07:00+01:00"}
  ]
}
```

`status` ist `gesendet` oder `aufgeschoben` (Ruhezeit, zugestellt ab `zustellen_ab`).
Mehr dazu: [server-alarme.md](server-alarme.md).

### `GET /api/server` – Messwerte und Erreichbarkeit

Die zuletzt gemessenen Werte aller Monitoring-Quellen, Quellen mit Fehlern, der Zustand
jedes Ziels der aktiven Erreichbarkeits-Prüfungen (`erreichbarkeit`, `domain` ist der Name
des Ziels, z. B. `cloud.louis-degiorgi.ch:8443`) und alle Prüfungen
(`erreichbarkeit_pruefungen`). Ohne eingerichtete Quelle ist `monitoring` `false`.

```bash
curl $MISSA/api/server -H "Authorization: Bearer $KEY"
```

```json
{
  "monitoring": true,
  "gemessen": "2026-11-11T14:07:00+01:00",
  "quellen": ["monitoring", "proxmox"],
  "quellen_fehler": {},
  "werte": [
    {"ziel": "ai01", "metrik": "cpu", "wert": 95.0, "einheit": "%", "quelle": "proxmox"}
  ],
  "erreichbarkeit": [
    {"domain": "git.louis-degiorgi.ch", "status": "ok", "seit": "2026-11-11T08:00:00+00:00",
     "fehler_in_folge": 0, "gemeldet": 0, "letzte_pruefung": "2026-11-11T13:05:00+00:00",
     "problem": "", "adressen": "203.0.113.10", "http_status": 200,
     "zertifikat_bis": "2027-01-20T10:11:12+00:00"}
  ],
  "erreichbarkeit_pruefungen": [
    {"id": 1, "ziel": "git.louis-degiorgi.ch", "domain": "git.louis-degiorgi.ch", "port": 443,
     "pfad": "/", "intervall_minuten": 5, "stufe": "wichtig", "pausiert": false, "notiz": ""}
  ]
}
```

### `GET /api/container` – Docker-Container der Hosts

Der zuletzt gelesene Stand je Host aus `SSH_HOSTS` (Missa liest alle 5 Minuten neu). Der
Endpunkt fragt die Hosts nicht selbst ab. Ohne SSH ist `ssh` `false`.

```bash
curl $MISSA/api/container -H "Authorization: Bearer $KEY"
```

```json
{
  "ssh": true,
  "hosts": {
    "wb01": {
      "stand": "2026-11-11T14:05:00+01:00",
      "container": [
        {"host": "wb01", "name": "nextcloud", "image": "nextcloud:29", "zustand": "running",
         "status": "Up 3 hours (healthy)", "gesundheit": "healthy", "ports": "", "cpu": "",
         "speicher": "", "problem": ""}
      ]
    }
  }
}
```

### `GET /api/anmeldungen` – SSH- und Nextcloud-Anmeldungen

Gespeicherte Anmeldungen (neueste zuerst, höchstens 1000) der letzten `tage` (Standard 1,
höchstens 90) und die als bekannt markierten IPs. Nur lesend.

```bash
curl "$MISSA/api/anmeldungen?tage=7" -H "Authorization: Bearer $KEY"
```

```json
{
  "ssh": true,
  "anzahl": 1,
  "anmeldungen": [
    {"quelle": "ssh", "host": "wb01", "zeit": "2026-11-11T13:02:00+00:00", "benutzer": "louis",
     "ip": "10.0.30.5", "erfolg": true, "art": "publickey"}
  ],
  "bekannte_ips": [
    {"ip": "10.0.30.5", "bekannt": 1, "notiz": "Laptop", "erstmals": "2026-11-11T13:02:00+00:00",
     "zuletzt": "2026-11-11T13:02:00+00:00", "erfolge": 1}
  ]
}
```

Mehr dazu: [container-logs-logins.md](container-logs-logins.md).

### `GET /api/updates` – anstehende Updates und Wartungsfenster

Der zuletzt erfasste Stand pro VM (Missa prüft täglich) in der Reihenfolge, in der sie
eingespielt würden (die eigene VM zuletzt), und das nächste Wartungsfenster. Nur lesend.

```bash
curl $MISSA/api/updates -H "Authorization: Bearer $KEY"
```

```json
{
  "ssh": true,
  "hosts": [
    {"host": "wb01", "geprueft": "2026-11-11T06:40:00+01:00", "anzahl": 3, "sicherheit": 2,
     "pakete": [{"name": "openssl", "neu": "3.0.15-1~deb12u1", "alt": "3.0.14-1~deb12u2",
                 "quellen": "stable-security", "sicherheit": true}],
     "neustart": false, "neustart_pakete": [], "fehler": ""}
  ],
  "reihenfolge": ["int01", "wb01", "ai01"],
  "wartungsfenster": {"beginn": "2026-11-14T10:00+01:00", "ende": "2026-11-14T11:00+01:00"}
}
```

Mehr dazu: [updates-zertifikate.md](updates-zertifikate.md).

### `GET /api/homelab` – VMs und IPs aus Proxmox

Missas Wissen über das Homelab vom letzten Scan (stündlich): pro VM bzw. Container Name,
VMID, Host, Status, CPU, RAM, Disk, VLANs, IPs (mit Quelle) und Pool. Nur lesend.

```bash
curl $MISSA/api/homelab -H "Authorization: Bearer $KEY"
```

```json
{
  "proxmox": true,
  "stand": "2026-11-12T09:00:00+01:00",
  "vms": [
    {"vmid": 110, "name": "rp01", "typ": "lxc", "node": "pve-id01", "status": "running",
     "cpus": 1, "ram_mb": 512, "disk_gb": 8, "vlans": [70], "ips": ["10.0.70.10"],
     "pool": "", "tags": [], "ip_quelle": "lxc"}
  ]
}
```

Mehr dazu: [homelab-firewall.md](homelab-firewall.md).

### `GET /api/vms` – Aufträge für Missas eigene VMs

Die letzten Aufträge (neueste zuerst) mit Name, VM-ID, IP, Status (`laeuft`, `fertig`,
`fehler`, `geloescht`), letztem Schritt und Fehler. Nur lesend, ohne neue Proxmox-Abfrage.

```bash
curl $MISSA/api/vms -H "Authorization: Bearer $KEY"
```

Mehr dazu: [proxmox-vms.md](proxmox-vms.md).

### `GET /api/lagebild` – was ist gerade los?

Das aktuelle Lagebild aus allen eingerichteten Quellen (Termine, Erinnerungen, Alarme, Server,
Hintergrund-Aufträge, Mails, Kontaktpflege, Uhrzeit, letzte Nachricht). Nur lesend; jeder
Aufruf fragt die Quellen neu ab. Fällt eine Quelle aus, steht sie mit `"status": "nicht
erreichbar"` in der Liste, der Rest kommt trotzdem. `text` ist ohne die Fremd-Markierungen,
die Missa im Gespräch verwendet; `fremd` sagt, welche Abschnitte fremden Inhalt enthalten.

```bash
curl "$MISSA/api/lagebild" -H "Authorization: Bearer $KEY"
```

```json
{
  "zeit": "2026-11-17T14:05:00+01:00",
  "fingerabdruck": "3f1c…",
  "zeichen": 412,
  "limit": 3000,
  "text": "### Uhrzeit\nDienstag, 17. November 2026, 14:05 Uhr\n\n### Erinnerungen\n- #12 „Zahnarzt anrufen“ – …",
  "quellen": [
    {"name": "Uhrzeit", "status": "ok", "fremd": false},
    {"name": "Termine", "status": "nicht erreichbar", "fremd": true},
    {"name": "Erinnerungen", "status": "ok", "fremd": false}
  ]
}
```

`status` ist `ok`, `leer` oder `nicht erreichbar`. `fingerabdruck` ändert sich nur, wenn sich
etwas ausser der Uhrzeit ändert. Mehr dazu: [initiative.md](initiative.md).

### `GET /api/initiative` – Zustand der Denkschleife

Nur lesend: an/aus, Intervall, Tagesbudget und heute verbrauchte Meldungen, Sammelzeit, Pause,
offene Notizen und der letzte Lauf (Format wie im Journal).

```bash
curl "$MISSA/api/initiative" -H "Authorization: Bearer $KEY"
```

```json
{
  "aktiv": true, "intervall_minuten": 15, "budget_pro_tag": 5, "heute_verbraucht": 2,
  "sammelzeit": "19:30", "pausiert_bis": null, "offene_notizen": 1, "journal_tage": 30,
  "letzter_lauf": {"id": 41, "zeit": "2026-11-17T14:15:00+01:00", "aktion": "nichts", "…": "…"}
}
```

### `GET /api/initiative/journal` – Gedanken-Journal

Die Läufe der Denkschleife (neueste zuerst) mit Entscheidung, Grund, Dauer und ob gemeldet
wurde. `?tage=7` (Standard 7, höchstens 90) und `?anzahl=100` (höchstens 1000). Nur lesend;
alle Texte sind geschwärzt.

```bash
curl "$MISSA/api/initiative/journal?tage=1" -H "Authorization: Bearer $KEY"
```

```json
{
  "tage": 1,
  "anzahl": 1,
  "laeufe": [
    {"id": 40, "zeit": "2026-11-17T14:00:00+01:00", "aktion": "melden",
     "punkte": [{"aktion": "melden", "stufe": "wichtig", "schluessel": "zahnarzt",
                 "titel": "Zahnarzt um 15 Uhr", "text": "…", "grund": "Weg dauert 40 Minuten",
                 "ergebnis": "gemeldet"}],
     "grund": "Weg dauert 40 Minuten", "dauer_ms": 18250, "uebersprungen": false,
     "ursache": "", "fehler": null, "gemeldet": true, "fingerabdruck": "3f1c…",
     "lagebild": "### Termine\n…"}
  ]
}
```

`ursache` sagt bei übersprungenen Läufen, warum (z. B. `Lagebild unverändert`,
`Chat-Anfrage läuft`, `Notfall-Stopp aktiv`). Mehr dazu: [initiative.md](initiative.md).

### `GET /api/geraete` – angeschlossene Geräte

Nur mit `MISSA_GERAETE_AKTIV=true` (sonst 404), Anmeldung mit dem API-Schlüssel. Alle
angelegten Geräte mit Zustand, Sitzungen, angenommenen Werkzeugen (mit der Stufe, die gilt)
und abgelehnten Werkzeugen mit Grund:

```bash
curl -s "$MISSA/api/geraete" -H "Authorization: Bearer $KEY"
```

```json
{
  "anzahl": 1,
  "geraete": [
    {
      "name": "laptop", "online": true, "verbunden": true, "seit": "2026-11-23T09:12:03+01:00",
      "angelegt": "2026-11-23T09:00:00+01:00", "gesperrt": null,
      "sitzungen": ["laptop-louis"],
      "werkzeuge": [{"name": "rechner_befehl", "stufe": "zerstoererisch", "beschreibung": "…"}],
      "abgelehnt": [{"name": "dateien_loeschen", "grund": "Name muss mit rechner_ beginnen"}]
    }
  ]
}
```

### `POST /api/geraete/anmelden`, `GET /api/geraete/auftraege`, `POST /api/geraete/ergebnis` – nur für Geräte

Diese drei Endpunkte ruft nur ein angeschlossenes Gerät auf, mit seinem **eigenen Token**
(`Authorization: Bearer <Geräte-Token>`, nicht der API-Schlüssel – der wird dort mit 401
abgelehnt, genauso wie ein Geräte-Token überall sonst). Den Vertrag im Wortlaut, Beispiele
und die Einrichtung beschreibt [geraete.md](geraete.md). Ohne `MISSA_GERAETE_AKTIV=true`
gibt es sie nicht (404).

### `POST /channels/nextcloud-talk/webhook` – nur für Nextcloud Talk

Kein Endpunkt für Apps: Hier liefert Nextcloud Talk Nachrichten an Missa (Bot-Webhook).
Er existiert nur, wenn Talk eingeschaltet ist, und ist nicht per Bearer-Token, sondern per
HMAC-Signatur mit dem Bot-Secret geschützt. Einrichtung und Ablauf:
[nextcloud-talk.md](nextcloud-talk.md).

## Fehler

Erfolgreiche Antworten haben immer `choices`. Geht etwas schief, kommt stattdessen ein
Fehler-Objekt im OpenAI-Format – **nie** `choices`. Eine App sollte deshalb zuerst den Status
prüfen, statt blind auf `choices` zuzugreifen:

```json
{
  "error": {
    "message": "Das Sprachmodell hat nicht innerhalb von 600 Sekunden geantwortet (LLAMA_TIMEOUT). …",
    "type": "llama_fehler",
    "code": "zeitlimit",
    "anfrage_id": "3f2c…"
  }
}
```

- `message`: ein deutscher Satz mit Ursache und nächstem Schritt – so kann ihn die App direkt
  anzeigen.
- `code`: fester Code (siehe Tabelle), zum Auswerten in der App.
- `anfrage_id` (nur bei `/v1/chat/completions`): Unter dieser ID stehen im Log von
  missa-core die Einzelheiten, bei Fehlern des Sprachmodells auch dessen Originaltext:
  `sudo journalctl -u missa-core | grep <anfrage_id>`.

| Status | `code` | Bedeutung |
|--------|--------|-----------|
| 400 | `ungueltige_anfrage` | Anfrage ungültig, z. B. leere `messages`, keine `user`-Nachricht, Sitzungs-ID zu lang, unbekannte Kategorie. |
| 401 | `nicht_angemeldet` | Kein oder falscher API-Schlüssel. |
| 413 | `zu_gross` | Die Anfrage ist grösser als 20 MB. |
| 429 | `zu_viele_fehlversuche` | Zu viele falsche Schlüssel von dieser Adresse – 15 Minuten warten. |
| 404 | `nicht_gefunden` | Unbekannter Endpunkt oder unbekanntes Modell. |
| 405 | `methode_nicht_erlaubt` | Falsche HTTP-Methode (z. B. `GET` statt `POST` bei `/v1/chat/completions`). |
| 502 | `anfrage_zu_gross` | Das Sprachmodell lehnt die Anfrage als zu gross ab (Kontext voll) – auch nachdem Missa sie einmal gekürzt hat (älterer Verlauf weg, Werkzeug-Ergebnisse gekürzt). Aufgaben einzeln schicken, neue Sitzung beginnen oder beim llama-server den Kontext (`-c`) vergrößern. |
| 502 | `modell_nicht_erreichbar` | llama-server läuft nicht, ist nicht erreichbar oder lädt gerade das Modell. |
| 502 | `unerwartete_antwort` | llama-server hat geantwortet, aber nicht im erwarteten Format. |
| 502 | `modell_fehler` | llama-server hat die Anfrage mit einem anderen Fehler abgelehnt (Originaltext im Log). |
| 504 | `zeitlimit` | llama-server hat nicht innerhalb von `LLAMA_TIMEOUT` Sekunden geantwortet (gilt je Runde der Werkzeug-Schleife). |
| 500 | `interner_fehler` | Unerwarteter Fehler in Missa-Core selbst (Einzelheiten im Log unter der `anfrage_id`). |
| 503 | – | Nur `/health`: llama-server antwortet nicht. |

Fehler des Sprachmodells haben `type: "llama_fehler"`. Beim Streaming kommt der Fehler als
letztes Ereignis vor `data: [DONE]` im selben Format (`data: {"error": {…}}`).

Fehler *in Werkzeugen* (z. B. Nextcloud nicht erreichbar) sind keine HTTP-Fehler: Missa
bekommt die Meldung und sagt sie ehrlich in ihrer Antwort.

### Fehler richtig abfangen (Python)

```python
import httpx

antwort = httpx.post(
    "https://missa.example.ch/v1/chat/completions",
    headers={"Authorization": "Bearer dein-missa-api-key", "X-Missa-Session": "skript"},
    json={"model": "missa", "messages": [{"role": "user", "content": "Was steht morgen an?"}]},
    timeout=900,  # mindestens so lang wie LLAMA_TIMEOUT – die Werkzeug-Schleife braucht Zeit
)
daten = antwort.json()
if antwort.status_code != 200 or "error" in daten:
    fehler = daten.get("error", {})
    print(f"Fehler {antwort.status_code} ({fehler.get('code')}): {fehler.get('message')}")
    print(f"Anfrage-ID fürs Log: {fehler.get('anfrage_id')}")
else:
    print(daten["choices"][0]["message"]["content"])
```

## Eine App anbinden

### Allgemein (jede OpenAI-kompatible App)

In den Einstellungen der App eintragen:

- **API-Basis-URL / Base URL:** `https://missa.example.ch/v1` (bzw. `http://ai01:8000/v1` im internen Netz)
- **API-Key:** der Wert von `MISSA_API_KEY`
- **Modell:** `missa`
- Falls die App eigene Header erlaubt: `X-Missa-Session: <Name des Geräts>`. Sonst schickt
  sie meist ihren Verlauf selbst mit – das funktioniert auch.

### Python (Paket `openai`)

```python
from openai import OpenAI

missa = OpenAI(
    base_url="https://missa.example.ch/v1",
    api_key="dein-missa-api-key",
    default_headers={"X-Missa-Session": "laptop-louis"},
    timeout=600,
)

antwort = missa.chat.completions.create(
    model="missa",
    messages=[{"role": "user", "content": "Was steht auf meiner Liste?"}],
)
print(antwort.choices[0].message.content)
```

Streaming:

```python
for teil in missa.chat.completions.create(
    model="missa",
    messages=[{"role": "user", "content": "Was habe ich morgen?"}],
    stream=True,
):
    print(teil.choices[0].delta.content or "", end="", flush=True)
```

### JavaScript (fetch)

```javascript
const antwort = await fetch("https://missa.example.ch/v1/chat/completions", {
  method: "POST",
  headers: {
    "Authorization": "Bearer dein-missa-api-key",
    "Content-Type": "application/json",
    "X-Missa-Session": "browser-louis",
  },
  body: JSON.stringify({
    model: "missa",
    messages: [{ role: "user", content: "Trag mir Freitag 14 Uhr Zahnarzt ein" }],
  }),
});
const daten = await antwort.json();
console.log(daten.choices[0].message.content);
```

## Was Missa kann (Werkzeuge)

Welche Werkzeuge aktiv sind, zeigt `/health` unter `werkzeuge`. Die App muss nichts davon
kennen – man fragt einfach in normaler Sprache.

| Bereich | Beispiel-Fragen | Werkzeuge |
|---------|-----------------|-----------|
| Kalender (Nextcloud) | „Was habe ich morgen?“, „Trag mir Freitag 14 Uhr Zahnarzt ein“, „Verschieb das Meeting auf 15 Uhr und mach es rot“, „Leg einen Kalender Lehre in Grün an“, „Mach das rückgängig“ | `termine_anzeigen`, `termin_eintragen`, `termin_aendern`, `termin_verschieben`, `termin_loeschen`, `kalender_anzeigen`, `kalender_anlegen`, `kalender_aendern`, `farbsystem_festlegen`, `kalender_rueckgaengig`, `kalender_protokoll` |
| Aufgaben (Nextcloud) | „Was steht auf meiner Liste?“, „Schreib auf: Milch kaufen“, „Hab ich erledigt“, nebenbei „muss noch Rail-Kit bestellen“ | `aufgaben_anzeigen`, `aufgabe_erstellen`, `aufgabe_erledigt`, `aufgabe_loeschen` |
| Notizen (Nextcloud Notes) | „Schreib dir auf, dass …“, „Ergänz die Homelab-Notiz um …“, „Was hab ich mir zu rp01 notiert?“ | `notiz_anlegen`, `notiz_ergaenzen`, `notizen_suchen`, `notiz_lesen` |
| Dateien (Nextcloud, WebDAV) | „Wo ist die Doku zu rp01?“, „Lies mir die Datei vor“, „Leg die Zusammenfassung in deinem Ordner ab“ | `dateien_suchen`, `ordner_anzeigen`, `datei_lesen`, `datei_schreiben` (nur in `Missa/`) |
| Gedächtnis | „Merk dir, dass ich Kaffee ohne Zucker trinke“, „Was weißt du über meine Schwester?“, „Das war falsch, …“, „Was hab ich dir letzte Woche über den Server erzählt?“, „Vergiss das mit dem Kaffee“ | `merken`, `erinnern`, `korrigieren`, `frueher_gesagt`, `vergessen` (siehe [gedaechtnis.md](gedaechtnis.md)) |
| Eigene Doku | „Welche IP hat rp01?“, „Was steht in meiner Doku zum Caddy-Setup?“ | `doku_fragen`, `doku_index_aktualisieren` (Antwort mit Dateiname; ohne Treffer „weiß ich nicht“) |
| Zusammenfassen | „Fass mir das zusammen: https://…“, „Fass die Offerte.pdf zusammen“, „Was steht in der Mail vom Chef?“ | `zusammenfassen`, `dokument_fragen`, `dokumente_anzeigen` (URLs nur aus Louis' Nachricht, siehe [doku-wissen.md](doku-wissen.md)) |
| Sicherung | „Mach ein Backup“, „Welche Backups gibt es?“ | `backup_erstellen`, `backups_anzeigen` (Wiederherstellen nur auf ai01, siehe [sicherung.md](sicherung.md)) |
| Erinnerungen | „Erinnere mich um 6:30 an Training“, „Jeden Montag um 7 an die Wochenplanung“, „In 10 Minuten nochmal“ | `erinnerung_anlegen`, `erinnerungen_anzeigen`, `erinnerung_aendern`, `erinnerung_loeschen`, `erinnerung_schlummern` |
| Routinen | „Jeden Freitag um 17 Uhr Wochenrückblick“, „Pausier den Wochenrückblick“, „Lösch die Routine“ | `routine_anlegen`, `routinen_anzeigen`, `routine_pausieren`, `routine_fortsetzen`, `routine_loeschen`, `routine_jetzt_ausfuehren` |
| Nachtschicht | „Mach mir heute Nacht eine Übersicht aller offenen Aufgaben“, „Nachtschicht von 2 bis 5 Uhr“ | `nachtaufgabe_anlegen`, `nachtaufgaben_anzeigen`, `nachtaufgabe_ergebnis`, `nachtaufgabe_loeschen`, `nachtschicht_einstellen` |
| Pausen und Puffer (Nextcloud-Kalender) | „Prüf mal morgen auf Pausen“, „… und trag die Pausen ein“, „Mach 20 Minuten Puffer“ | `tag_pruefen`, `pausen_regeln_anzeigen`, `pausen_regeln_einstellen` (dazu prüft Missa jeden Abend und bei jeder Terminanlage von selbst; eingetragen wird nur nach Ja) |
| Kontakte (Nextcloud) | „Wer hat bald Geburtstag?“, „Oma ist mir wichtig, mindestens alle 3 Wochen“, „Hab gerade mit Oma telefoniert“ | `kontakte_suchen`, `geburtstage_anzeigen`, `kontakt_wichtig_markieren`, `kontakt_nicht_mehr_wichtig`, `kontakt_gemeldet`, `letzter_kontakt`, `wichtige_kontakte_anzeigen` |
| E-Mail (nur wenn eingerichtet) | „Schreib dem Chef, dass ich morgen später komme“, „Was ist neu im Posteingang?“, „Schreib förmlicher an Lehrer“ – gesendet wird nur nach „Ja, abschicken“ | `emails_anzeigen`, `email_lesen`, `email_entwurf_erstellen`, `email_entwurf_aendern`, `email_entwuerfe_anzeigen`, `email_entwurf_verwerfen`, `email_adresse_suchen`, `schreibstil_anzeigen`, `schreibstil_lernen`, `schreibstil_anpassen` (kein Senden-Werkzeug – siehe [email.md](email.md)) |
| Nextcloud Talk (nur wenn eingerichtet) | „Schick mir die Übersicht per Talk“, „Erinnere mich per Talk an …“ | `talk_nachricht_senden` |
| Server-Alarme (nur wenn eine Monitoring-Quelle eingerichtet ist) | „Ruf mich an, wenn ai01 über 90 % ist“, „Wie ist die Auslastung von ai01?“, „Welche Alarm-Regeln gibt es?“, „Ruhezeit von 23 bis 6 Uhr“ | `server_werte_anzeigen`, `alarmregel_anlegen`, `alarmregeln_anzeigen`, `alarmregel_aendern`, `alarmregel_loeschen`, `alarme_anzeigen`, `alarm_einstellungen` |
| Lagebild und Denkschleife | „Was ist gerade los?“, „Stör mich höchstens dreimal am Tag“, „Denk mal eine Stunde nicht nach“ | `lagebild_anzeigen`, `initiative_einstellen`, `initiative_status`, `gedanken_anzeigen` („Warum hast du mir das geschrieben?“), `initiative_gelerntes_anzeigen`, `initiative_gelerntes_zuruecksetzen` (nur nach Ja; siehe [initiative.md](initiative.md)) |
| Erreichbarkeit (immer; Startwert aus `ERREICHBARKEIT_DOMAINS`) | „Ist git von außen erreichbar?“, „Welche Prüfungen hast du?“, „Prüf cloud lieber auf Port 8443“, „Prüf git nur noch jede Stunde“, „Pausier die Prüfung von ki“ | `erreichbarkeit_pruefen`, `erreichbarkeit_pruefungen_anzeigen`, `erreichbarkeit_pruefung_anlegen`, `erreichbarkeit_pruefung_aendern`, `erreichbarkeit_pruefung_pausieren`, `erreichbarkeit_pruefung_loeschen` |
| Container und Logs (nur wenn `SSH_HOSTS` gesetzt ist) | „Welche Container laufen auf wb01?“, „Warum geht Nextcloud nicht?“, „Starte nextcloud neu“ (nur nach Ja) | `container_anzeigen`, `container_logs`, `container_neustarten`, `fehlersuche`, `journal_lesen` |
| Updates (nur wenn `SSH_HOSTS` gesetzt ist) | „Welche Updates stehen an?“, „Plan ein Wartungsfenster“, „Spiel die Updates auf wb01 ein“ (nur nach Ja), „Starte wb01 neu“ (nur nach Ja) | `updates_anzeigen`, `wartungsfenster_vorschlagen`, `wartungsfenster_eintragen`, `updates_plan`, `updates_einspielen`, `host_neustarten` |
| Zertifikate und Domains | „Wann laufen die Zertifikate ab?“ | `zertifikate_pruefen` |
| Homelab (nur mit Proxmox-Token) | „Welche IP hat rp01?“, „Welche VMs gibt es?“ | `homelab_anzeigen` |
| Eigene VMs (nur mit VM-Token) | „Richte einen Minecraft-Server ein“, „Welche VMs hast du angelegt?“, „Lösch mc01“ (nur nach Ja) | `vm_planen`, `vm_erstellen`, `missa_vms_anzeigen`, `playbooks_anzeigen`, `vm_starten`, `vm_stoppen`, `vm_loeschen` |
| Firewall (nur mit `FIREWALL_LOG_DATEI`, nur lesen) | „Was war gestern an der Firewall los?“ | `firewall_zusammenfassung` |
| Anmeldungen (nur wenn `SSH_HOSTS` gesetzt ist) | „Wer hat sich heute angemeldet?“, „10.0.30.5 ist mein Laptop“ | `anmeldungen_anzeigen`, `ip_als_bekannt_markieren`, `ip_nicht_mehr_bekannt`, `bekannte_ips_anzeigen`, `login_einstellungen` |
