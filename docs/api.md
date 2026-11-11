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
  "version": "0.3.0",
  "llama": "ok",
  "charakter": "ok",
  "werkzeuge": ["merken", "erinnern", "vergessen", "termine_anzeigen", "termin_eintragen", "termin_loeschen", "aufgaben_anzeigen", "aufgabe_erstellen", "aufgabe_erledigt", "aufgabe_loeschen"]
}
```

Bei Störung: `"status": "gestoert"`, `"llama": "nicht erreichbar"`. `"charakter": "fehlt"`
heißt, die Charakterdatei ist nicht lesbar (Missa antwortet dann mit einem Ersatztext).

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
- **Manches braucht ein Ja:** Bittet man Missa, eine Aufgabe oder einen Fakt zu löschen
  oder einen Termin zu ändern, bei dem andere Personen eine E-Mail bekämen (Einladung,
  Änderung, Absage), fragt sie zuerst nach. Ausgeführt wird erst, wenn in einer *nächsten*
  Nachricht ein Ja kommt. Dafür muss die App die Sitzung beibehalten (gleiche
  `X-Missa-Session`) oder den Verlauf mitschicken. Eigene Termine ändert und löscht Missa
  ohne Rückfrage – protokolliert und rückgängig machbar.
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

### `POST /channels/nextcloud-talk/webhook` – nur für Nextcloud Talk

Kein Endpunkt für Apps: Hier liefert Nextcloud Talk Nachrichten an Missa (Bot-Webhook).
Er existiert nur, wenn Talk eingeschaltet ist, und ist nicht per Bearer-Token, sondern per
HMAC-Signatur mit dem Bot-Secret geschützt. Einrichtung und Ablauf:
[nextcloud-talk.md](nextcloud-talk.md).

## Fehler

Alle Fehler kommen im OpenAI-Format `{"error": {"message": "...", "type": "...", "code": ...}}`.

| Status | Bedeutung |
|--------|-----------|
| 400 | Anfrage ungültig, z. B. leere `messages`, keine `user`-Nachricht, Sitzungs-ID zu lang, unbekannte Kategorie. |
| 401 | Kein oder falscher API-Schlüssel. |
| 404 | Unbekannter Endpunkt oder unbekanntes Modell. |
| 405 | Falsche HTTP-Methode (z. B. `GET` statt `POST` bei `/v1/chat/completions`). |
| 502 | llama-server ist nicht erreichbar oder hat einen Fehler gemeldet (`type: "llama_fehler"`). |
| 503 | Nur `/health`: llama-server antwortet nicht. |
| 504 | llama-server hat nicht innerhalb von `LLAMA_TIMEOUT` geantwortet. |

Fehler *in Werkzeugen* (z. B. Nextcloud nicht erreichbar) sind keine HTTP-Fehler: Missa
bekommt die Meldung und sagt sie ehrlich in ihrer Antwort.

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
| Kalender (Nextcloud) | „Was habe ich morgen?“, „Trag mir Freitag 14 Uhr Zahnarzt ein“, „Verschieb das Meeting auf 15 Uhr und mach es rot“, „Leg einen Kalender Lehre in Grün an“, „Mach das rückgängig“ | `termine_anzeigen`, `termin_eintragen`, `termin_aendern`, `termin_loeschen`, `kalender_anzeigen`, `kalender_anlegen`, `kalender_aendern`, `farbsystem_festlegen`, `kalender_rueckgaengig`, `kalender_protokoll` |
| Aufgaben (Nextcloud) | „Was steht auf meiner Liste?“, „Schreib auf: Milch kaufen“, „Hab ich erledigt“ | `aufgaben_anzeigen`, `aufgabe_erstellen`, `aufgabe_erledigt`, `aufgabe_loeschen` |
| Gedächtnis | „Merk dir, dass ich Kaffee ohne Zucker trinke“, „Was weißt du über meine Schwester?“, „Vergiss das“ | `merken`, `erinnern`, `vergessen` |
| Erinnerungen | „Erinnere mich um 6:30 an Training“, „Jeden Montag um 7 an die Wochenplanung“, „In 10 Minuten nochmal“ | `erinnerung_anlegen`, `erinnerungen_anzeigen`, `erinnerung_aendern`, `erinnerung_loeschen`, `erinnerung_schlummern` |
| Routinen | „Jeden Freitag um 17 Uhr Wochenrückblick“, „Pausier den Wochenrückblick“, „Lösch die Routine“ | `routine_anlegen`, `routinen_anzeigen`, `routine_pausieren`, `routine_fortsetzen`, `routine_loeschen`, `routine_jetzt_ausfuehren` |
| Nachtschicht | „Mach mir heute Nacht eine Übersicht aller offenen Aufgaben“, „Nachtschicht von 2 bis 5 Uhr“ | `nachtaufgabe_anlegen`, `nachtaufgaben_anzeigen`, `nachtaufgabe_ergebnis`, `nachtaufgabe_loeschen`, `nachtschicht_einstellen` |
| Pausen und Puffer (Nextcloud-Kalender) | „Prüf mal morgen auf Pausen“, „… und trag die Pausen ein“ | `tag_pruefen` (dazu prüft Missa jeden Abend und nach jeder Terminanlage von selbst) |
| Kontakte (Nextcloud) | „Wer hat bald Geburtstag?“, „Oma ist mir wichtig, mindestens alle 3 Wochen“, „Hab gerade mit Oma telefoniert“ | `kontakte_suchen`, `geburtstage_anzeigen`, `kontakt_wichtig_markieren`, `kontakt_nicht_mehr_wichtig`, `kontakt_gemeldet`, `wichtige_kontakte_anzeigen` |
| E-Mail (nur wenn eingerichtet) | „Schreib dem Chef, dass ich morgen später komme“, „Was ist neu im Posteingang?“, „Schreib förmlicher an Lehrer“ – gesendet wird nur nach „Ja, abschicken“ | `emails_anzeigen`, `email_lesen`, `email_entwurf_erstellen`, `email_entwurf_aendern`, `email_entwuerfe_anzeigen`, `email_entwurf_verwerfen`, `email_adresse_suchen`, `schreibstil_anzeigen`, `schreibstil_lernen`, `schreibstil_anpassen` (kein Senden-Werkzeug – siehe [email.md](email.md)) |
| Nextcloud Talk (nur wenn eingerichtet) | „Schick mir die Übersicht per Talk“, „Erinnere mich per Talk an …“ | `talk_nachricht_senden` |
