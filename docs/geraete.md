# Geräte anschliessen (z. B. den Laptop)

Ein Gerät – zuerst der Laptop mit der Oberfläche hypr-shell – kann sich an Missa anschliessen
und eigene Werkzeuge anmelden (`rechner_…`). Missa benutzt sie wie ihre eigenen, sie laufen
aber auf dem Gerät: Dateien suchen, lesen und öffnen, Programme starten, Fenster steuern und
nach Rückfrage Befehle ausführen. Erinnerungen und Alarme erscheinen zusätzlich als
Benachrichtigung am Desktop.

Die Verbindung baut **immer das Gerät** auf (ai01 muss den Laptop nie erreichen). Jedes Gerät
hat einen **eigenen Token** – nicht den API-Schlüssel. Ohne `MISSA_GERAETE_AKTIV=true` gibt es
die Endpunkte nicht (404) und alles ist wie bisher.

Der Code auf dem Gerät liegt in einem anderen Repo; hier steht nur die Seite von Missa-Core.

## Einrichten in vier Schritten

1. **Token anlegen** (auf ai01). Der Token wird genau einmal angezeigt; gespeichert ist nur
   sein Hash (SHA-256) in `MISSA_DATEN/missa.db`:

   ```bash
   sudo /opt/missa/app/scripts/einrichten/geraet-anlegen.sh laptop
   ```

   Den Token auf dem Gerät eintragen (dort, wo das Geräte-Repo es beschreibt).

2. **Variablen setzen** in `/etc/missa/missa.env` (alle mit Erklärung siehe unten):

   ```bash
   MISSA_GERAETE_AKTIV=true
   ```

3. **Dienst neu starten:**

   ```bash
   sudo systemctl restart missa-core
   ```

4. **Prüfen**, sobald das Gerät läuft:

   ```bash
   curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/health | jq .geraete
   ```

   ```json
   [{"name": "laptop", "online": true, "werkzeuge": ["rechner_status", "rechner_oeffnen"], "seit": "2026-11-23T09:12:03+01:00"}]
   ```

   Mehr Einzelheiten (Sitzungen, Stufen, abgelehnte Werkzeuge mit Grund):

   ```bash
   curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/api/geraete | jq
   ```

Läuft Missa-Core hinter Caddy und verbindet sich das Gerät von aussen, siehe
[Reverse Proxy](#reverse-proxy-caddy).

### Tokens verwalten

```bash
sudo /opt/missa/app/scripts/einrichten/geraet-anlegen.sh --liste
sudo /opt/missa/app/scripts/einrichten/geraet-anlegen.sh --sperren laptop
```

Ein gesperrter Token gilt **sofort** nicht mehr, ohne Neustart: Anfragen bekommen 401, ein
offener Stream endet spätestens nach 15 Sekunden. Für einen neuen Token (z. B. weil der alte
verloren ist): erst sperren, dann neu anlegen. Gerätenamen: 1–32 Zeichen, Kleinbuchstaben,
Ziffern, `-` und `_`.

## Vertrag „Geräte-Anschluss v1“

Das Gerät hält sich an denselben Text.

Anmeldung bei allen drei Endpunkten: `Authorization: Bearer <Geräte-Token>`. Das ist ein eigener Token pro Gerät, nicht der API-Schlüssel. Die Verbindung baut immer das Gerät auf.

1. `POST /api/geraete/anmelden`
   Anfrage: `{"protokoll": 1, "geraet": "<name>", "sitzungen": ["<Sitzungs-ID>"], "werkzeuge": [{"name": "rechner_…", "beschreibung": "…", "parameter": {JSON-Schema}, "stufe": "lesen|schreiben|zerstoererisch"}]}`
   Antwort 200: `{"ok": true, "geraet": "<name>", "angenommen": ["…"], "abgelehnt": [{"name": "…", "grund": "…"}]}`
2. `GET /api/geraete/auftraege` – Server-Sent Events, bleibt offen, alle 15 Sekunden ein Kommentar `: ping`
   `event: auftrag` mit `data: {"id": "<uuid>", "werkzeug": "rechner_…", "argumente": {…}, "frist_s": 60}`
   `event: meldung` mit `data: {"titel": "…", "text": "…", "stufe": "info|wichtig|notfall"}`
   `event: abbruch` mit `data: {"id": "<uuid>"}`
3. `POST /api/geraete/ergebnis`
   Erfolg: `{"id": "<uuid>", "ok": true, "ergebnis": "<Text, höchstens 20000 Zeichen>"}`
   Fehler: `{"id": "<uuid>", "ok": false, "art": "abgelehnt|zeitlimit|fehler", "fehler": "<kurzer Satz>"}`

Regeln: Bricht der Stream ab, gilt das Gerät nach 30 Sekunden als offline. Ein Auftrag ohne Ergebnis bis `frist_s` gilt als Zeitlimit. Jede Auftrags-ID wird höchstens einmal ausgeführt. Nach einem Neustart des Dienstes meldet sich das Gerät neu an.

Werkzeuge, die der Laptop anmelden wird (Mindeststufe ist verbindlich):

| Name | Mindeststufe | Zweck |
|---|---|---|
| rechner_status | lesen | Akku, Auslastung, Netz, offene Sitzung |
| rechner_dateien_suchen | lesen | Dateien nach Name oder Inhalt finden |
| rechner_datei_lesen | lesen | Textinhalt einer Datei oder Liste eines Ordners |
| rechner_fenster_anzeigen | lesen | offene Fenster und installierte Apps |
| rechner_zwischenablage_lesen | lesen | aktueller Inhalt der Zwischenablage |
| rechner_oeffnen | schreiben | Datei, Ordner, Adresse oder App öffnen |
| rechner_fenster_steuern | schreiben | fokussieren, verschieben, schliessen |
| rechner_medien | schreiben | Wiedergabe, Lautstärke, Helligkeit |
| rechner_benachrichtigen | schreiben | Benachrichtigung am Desktop |
| rechner_oberflaeche | schreiben | Panel öffnen, Hintergrundbild, Einstellung setzen |
| rechner_datei_anlegen | schreiben | neue Datei schreiben, überschreibt nie |
| rechner_zwischenablage_setzen | schreiben | Text in die Zwischenablage |
| rechner_befehl | zerstoererisch | beliebiger Befehl als Benutzer |
| rechner_datei_aendern | zerstoererisch | überschreiben, verschieben, in den Papierkorb |
| rechner_prozess_beenden | zerstoererisch | Programm hart beenden |
| rechner_energie | zerstoererisch | sperren, abmelden, neu starten, ausschalten |

### Ergänzungen zum Vertrag (wie Missa-Core ihn auslegt)

Diese Punkte stehen nicht im Vertrag, ändern ihn aber nicht – ein Gerät, das sich genau an
den Text oben hält, funktioniert:

- **Fehler-Antworten:** 401 (Token fehlt, falsch oder gesperrt), 429 nach zehn Fehlversuchen
  in fünf Minuten (dieselbe Sperre wie für die API, 15 Minuten, mit `Retry-After`), 400 bei
  ungültiger Anfrage (z. B. `protokoll` nicht 1 oder `geraet` passt nicht zum Token), 404 ohne
  `MISSA_GERAETE_AKTIV`. Fehler kommen im gleichen Format wie bei der API:
  `{"error": {"message": …, "type": …, "code": …}}`.
- **`POST /api/geraete/ergebnis`** antwortet `200 {"ok": true}`; `404` für eine unbekannte ID
  (oder die eines anderen Geräts) und `409`, wenn die ID schon erledigt oder abgebrochen ist
  (jede ID gilt höchstens einmal).
- **Stream:** Direkt nach dem Verbinden kommt ein Kommentar `: verbunden`. Eine neue
  Verbindung desselben Geräts ersetzt die alte (die alte endet). Aufträge, die noch nicht
  gesendet waren, gehen an die neue Verbindung; schon gesendete werden nie wiederholt.
- **Anmeldung = Lebenszeichen:** Nach `anmelden` (und nach jedem Ergebnis) gilt das Gerät
  30 Sekunden lang als online, auch wenn der Stream noch nicht offen ist. Ein Stream ohne
  vorherige Anmeldung wird angenommen (Meldungen kommen an), Werkzeuge gibt es aber erst nach
  der Anmeldung.
- **Lange Ergebnisse** werden auf 20 000 Zeichen gekürzt (mit Hinweis), statt abgelehnt.
  Eine Fehlerbeschreibung wird auf 500 Zeichen gekürzt; eine unbekannte `art` gilt als
  `fehler`.
- **`frist_s`** ist `MISSA_GERAETE_FRIST` (Standard 60). Läuft sie ab, schickt Missa
  `event: abbruch` und nimmt kein Ergebnis mehr an.

## Was Missa mit den Werkzeugen macht

- **Eigene Gruppe** im Werkzeugkasten, je Gerät eine. Geht das Gerät offline, verschwinden die
  Werkzeuge; verbindet es sich wieder, sind sie ohne neue Anmeldung zurück.
- **Stufe:** Für die Namen aus der Tabelle gilt die Mindeststufe – das Gerät darf höher
  melden, nie tiefer (meldet es `rechner_befehl` als `lesen`, bleibt es `zerstoererisch`).
  Unbekannte `rechner_*`-Namen sind mindestens `schreiben`.
- **Abgelehnt** (mit Grund in der Antwort und im Log): Namen ohne Vorsilbe `rechner_`, Namen
  eines eingebauten Werkzeugs, schon vergebene Namen (z. B. von einem zweiten Gerät), mehr als
  `MISSA_GERAETE_MAX_WERKZEUGE`, fehlende Beschreibung, falsche Stufe, Parameter ohne
  JSON-Schema-Objekt, Doppeltes.
- **Nur in den Sitzungen des Geräts:** Die Werkzeuge gibt es nur in den Sitzungen, die das
  Gerät bei der Anmeldung nennt (Header `X-Missa-Session` der App auf dem Gerät). In Nextcloud
  Talk und anderen Apps kennt Missa sie nicht – so bleibt die Anfrage ans Sprachmodell dort
  klein. `MISSA_GERAETE_ALLE_SITZUNGEN=true` macht sie überall sichtbar.
- **Anleitung im Systemprompt:** Ist in der Sitzung ein Gerät online, bekommt Missa einen
  kurzen Abschnitt: erst suchen, dann öffnen; bei mehreren Treffern nachfragen; Pfade nie
  raten; „geöffnet“ nur nach einem echten Ergebnis; Befehle vorher erklären. Ohne Gerät ist
  der Systemprompt unverändert.
- **Rückfrage:** Für `zerstoererisch` zeigt Missa vorher Gerät und genaue Aktion, z. B.
  „Auf laptop wird ausgeführt: `rm ~/tmp/*.log`“, und führt erst nach deinem Ja in der
  nächsten Nachricht derselben Sitzung aus. Es gelten alle Regeln aus [rechte.md](rechte.md).
- **Fremde Inhalte:** Jedes Ergebnis vom Gerät ist fremder Inhalt (markiert). Danach braucht
  in derselben Anfrage auch Schreibendes dein Ja – steht in einer gelesenen Datei „lösch
  alles“, passiert ohne Rückfrage nichts.
- **Notfall-Stopp** sperrt alles ausser `lesen`. **Aktionsprotokoll:** Jeder Aufruf steht mit
  Gerätename (Spalte `geraet`) unter `GET /api/aktionen`, Geheimnisse geschwärzt.
- **Hintergrund** (Routinen, Nachtschicht, Initiative): Geräte-Werkzeuge laufen dort nie,
  auch keine lesenden – ausser mit `MISSA_GERAETE_IM_HINTERGRUND=nur_lesen` (dann nur
  lesende).
- **Ehrlich, wenn es nicht klappt:** Ist das Gerät offline, antwortet es nicht in der Frist
  oder lehnt es ab, bekommt Missa einen klaren Satz und sagt dir das, statt etwas zu
  behaupten.

## Meldungen am Desktop

Erinnerungen, Alarme und Hinweise kommen als `event: meldung` an jedes Gerät, das online ist.
Die Stufe steht schon im Text: 🚨 → `notfall`, ⚠️ → `wichtig`, eine Erinnerung (⏰) →
`wichtig`, alles andere `info`.

| `MISSA_GERAETE_MELDUNGEN` | Was passiert |
|---|---|
| `zusaetzlich` (Standard) | über Talk **und** ans Gerät, wenn es online ist |
| `bevorzugt` | nur ans Gerät, wenn es online ist – sonst wie bisher über Talk |
| `aus` | nie ans Gerät |

Ruhezeit, Ruhefenster und Stufen der Alarme gelten unverändert (siehe
[server-alarme.md](server-alarme.md)): In der Ruhezeit kommen nur Notfälle durch, der Rest
am Morgen gesammelt. Eine Meldung gilt als zugestellt, sobald ein Weg geklappt hat – nichts
kommt doppelt auf demselben Weg.

## Alle Variablen

| Variable | Standard | Bedeutung |
|---|---|---|
| `MISSA_GERAETE_AKTIV` | `false` | `true` schaltet den Geräte-Anschluss ein. Aus = die Endpunkte gibt es nicht (404), alles wie bisher. |
| `MISSA_GERAETE_FRIST` | `60` | So viele Sekunden hat ein Gerät für einen Auftrag (5–600), danach Zeitlimit und `event: abbruch`. |
| `MISSA_GERAETE_ALLE_SITZUNGEN` | `false` | `true` = die Werkzeuge eines Geräts gibt es in jeder Sitzung, auch in Nextcloud Talk. |
| `MISSA_GERAETE_MAX_WERKZEUGE` | `20` | Höchstens so viele Werkzeuge pro Gerät; der Rest wird bei der Anmeldung abgelehnt. |
| `MISSA_GERAETE_IM_HINTERGRUND` | `aus` | `nur_lesen` erlaubt Routinen, Nachtschicht und Initiative die lesenden Werkzeuge eines Geräts. Schreibendes läuft dort nie. |
| `MISSA_GERAETE_MELDUNGEN` | `zusaetzlich` | `aus`, `zusaetzlich` oder `bevorzugt` – siehe [Meldungen am Desktop](#meldungen-am-desktop). |

Fest eingestellt (Vertrag): Ping alle 15 Sekunden, offline nach 30 Sekunden ohne Stream,
Ergebnis höchstens 20 000 Zeichen.

## Reverse Proxy (Caddy)

Der Stream `GET /api/geraete/auftraege` bleibt stundenlang offen. Der Reverse Proxy muss
Server-Sent Events **sofort weitergeben** (nicht puffern) und darf die Verbindung nicht nach
kurzer Zeit schliessen. Beispiel für Caddy auf rp01:

```caddyfile
missa.example.ch {
	# Stream der Geräte: nicht puffern, lange offen lassen.
	@geraete_stream path /api/geraete/auftraege
	reverse_proxy @geraete_stream 10.0.70.10:8000 {
		flush_interval -1
		transport http {
			# Alle 15 s kommt ein Ping – länger wartet nie jemand auf Daten.
			read_timeout 2m
		}
		# Beim Neuladen von Caddy offene Streams erst nach 5 Minuten trennen.
		stream_close_delay 5m
	}
	reverse_proxy 10.0.70.10:8000
}
```

Die Adresse von ai01 an deine anpassen. Hast du in den globalen Optionen von Caddy
`servers { timeouts { write … } }` gesetzt, muss der Wert grösser als die Dauer eines Streams
sein (oder weg). Missa-Core schickt selbst `Cache-Control: no-cache` und
`X-Accel-Buffering: no`. Der Geräte-Token geht wie der API-Schlüssel im Header – nur über
HTTPS von aussen erreichbar machen.

## Fehlersuche

| Was du siehst | Ursache und Abhilfe |
|---|---|
| `404` auf `/api/geraete/…` | `MISSA_GERAETE_AKTIV=true` fehlt oder der Dienst wurde nicht neu gestartet. |
| `401` | Token falsch, gesperrt oder es wurde der API-Schlüssel statt des Geräte-Tokens geschickt. `geraet-anlegen.sh --liste` zeigt, ob das Gerät aktiv ist. |
| `429` | Zehn Fehlversuche in fünf Minuten von dieser Adresse – 15 Minuten warten (alle Endpunkte der Adresse sind gesperrt). |
| `400` „passt nicht zum Token“ | `geraet` in der Anmeldung muss genau der Name sein, für den der Token angelegt wurde. |
| Gerät ist nach kurzer Zeit offline | Der Stream bricht ab: Puffer oder Zeitlimit im Reverse Proxy (siehe oben). Im Log steht „Gerät … ist offline“. |
| Missa kennt die Werkzeuge nicht | Die App auf dem Gerät nutzt eine andere `X-Missa-Session` als die in `sitzungen` gemeldete – vergleichen mit `GET /api/geraete` (`sitzungen`). Oder das Gerät ist offline. |
| Ein Werkzeug fehlt | `GET /api/geraete` zeigt unter `abgelehnt` den Grund (z. B. keine Vorsilbe `rechner_`, zu viele Werkzeuge). |
| Keine Meldungen am Desktop | `MISSA_GERAETE_MELDUNGEN` ist `aus`, das Gerät ist offline oder es ist Ruhezeit (dann nur Notfälle). |

Im Log nachsehen:

```bash
sudo journalctl -u missa-core | grep -i "gerät"
```

## Für Entwickler

- Verbindung, Tokens, Aufträge, Werkzeuge und Zustellweg: `src/missa/core/geraete.py`
  (`GeraeteTokens`, `Geraete`, `GeraeteWerkzeuge`, `GeraeteZustellkanal`).
- Endpunkte: `src/missa/core/app.py` (`_geraete_endpunkte`); Token-Prüfung vor dem Lesen und
  Sperre nach Fehlversuchen: `src/missa/core/schutz.py`.
- Sichtbarkeit je Sitzung: `Werkzeugkasten.gruppe_ersetzen(…, sichtbar=…)` in
  `src/missa/core/werkzeuge.py`.
- Tests: `tests/test_geraete.py`, `tests/test_rechner_werkzeuge.py`,
  `tests/test_geraete_meldungen.py` mit dem Fake-Gerät `tests/geraet_fake.py`, das echtes HTTP
  mit einem Test-Server auf 127.0.0.1 spricht – nie mit einem echten Rechner.
