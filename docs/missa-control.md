# Missa Control anbinden

[Missa Control](https://github.com/Louidos/missa-control) ist das Panel auf ai01 (Port 8090),
in dem du Integrationen einrichtest (Home Assistant, Geräte, Regeln, Telefonie …). Missa-Core
holt sich von dort alles, was im Panel eingerichtet ist, und benutzt es wie eigene Werkzeuge.
Das Panel ändert an Missa-Core nie etwas – Missa-Core fragt nur ab.

## Einrichtung in zwei Zeilen

1. Im Panel **Clients → Neuer Client** öffnen, Namen `missa-core` eingeben, den Token
   kopieren (er wird nur einmal angezeigt).
2. In `/etc/missa/missa.env` eintragen und den Dienst neu starten:

```bash
MISSA_CONTROL_URL=http://127.0.0.1:8090
MISSA_CONTROL_TOKEN=der-token-aus-dem-panel
```

```bash
sudo systemctl restart missa-core
```

Prüfen: `curl -s http://127.0.0.1:8000/health` zeigt unter `missa_control`, ob die
Verbindung steht und wie viele Funktionen geladen sind:

```json
"missa_control": {
  "verbunden": true,
  "funktionen": 12,
  "letzte_aktualisierung": "2026-11-16T13:05:00+01:00",
  "fehler": null
}
```

Danach im Panel **Protokoll → Ereignisse → Test-Ereignis senden**: Die Bestätigung kommt in
Nextcloud Talk an (dafür muss `NC_TALK_NOTIFY_CONVERSATION` gesetzt sein).

Ohne die beiden Zeilen ist alles wie bisher: kein Eintrag in `/health`, keine zusätzlichen
Werkzeuge, keine Verbindung zum Panel.

## Alle Einstellungen

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `MISSA_CONTROL_URL` | – | Adresse des Panels, z. B. `http://127.0.0.1:8090`. Leer = aus. |
| `MISSA_CONTROL_TOKEN` | – | Client-Token aus dem Panel (**Clients**). Nur in `missa.env`, nie im Repo. |
| `MISSA_CONTROL_TOKEN_FILE` | – | Statt des Tokens: eine Datei, in der nur der Token steht (z. B. `/etc/missa/missa-control.token`, Rechte `640 root:missa`). Wird bei jedem Aufruf neu gelesen – ein neuer Token wirkt ohne Neustart. |
| `MISSA_CONTROL_AKTUALISIEREN` | `60` | Alle so viele Sekunden holt Missa die Funktionsliste neu (mindestens 10). |
| `MISSA_CONTROL_ZEITLIMIT` | `330` | So viele Sekunden wartet ein Aufruf höchstens – Freigaben im Panel dürfen bis zu 5 Minuten dauern. |
| `MISSA_CONTROL_MAX_WERKZEUGE` | `40` | Höchstens so viele Panel-Funktionen; der Rest wird ausgelassen und im Log genannt. Jede Funktion macht jede Anfrage ans Sprachmodell grösser. |
| `MISSA_CONTROL_AUSLASSEN` | – | Funktionen mit diesen Namens-Vorsilben nicht laden, mit Komma getrennt, z. B. `kalender_,gedaechtnis_`. |
| `MISSA_CONTROL_IM_HINTERGRUND` | `nur_lesen` | Routinen und Nachtschicht dürfen Panel-Funktionen nur lesen. `alles` erlaubt dort auch schreibende Funktionen. |
| `MISSA_CONTROL_EREIGNISSE` | `an` | Ereignisse des Panels von sich aus melden (siehe unten). `aus` schaltet nur das ab, die Funktionen bleiben. |

## Was Missa daraus macht

- Jede Funktion des Panels wird ein Werkzeug mit **gleichem Namen**, gleicher Beschreibung und
  gleichen Parametern, z. B. `home_assistant_einschalten`.
- **Stufe:** Funktionen, die das Panel als „nur lesend“ markiert (`readOnlyHint`), sind
  `lesen`, alle anderen `schreiben`. Panel-Funktionen sind nie `zerstoererisch` – Missa fragt
  im Chat also nicht zusätzlich nach; über Rückfragen entscheidet das Panel (siehe unten).
- **Notfall-Stopp** und **Aktionsprotokoll** gelten wie für jedes Werkzeug: Beim Notfall-Stopp
  sind schreibende Panel-Funktionen gesperrt, jeder Aufruf steht unter `GET /api/aktionen`.
- **Neue Integrationen ohne Neustart:** Aktivierst du im Panel eine Integration, ist sie nach
  spätestens `MISSA_CONTROL_AKTUALISIEREN` Sekunden da. Meldet das Panel „Funktion gibt es
  nicht“, lädt Missa die Liste sofort neu.
- **Gleicher Name wie ein eingebautes Werkzeug:** Das eingebaute gewinnt, die Panel-Funktion
  wird ausgelassen und im Log genannt.
- Lange Ergebnisse werden gekürzt (mit Hinweis), Bilder und andere Inhalte ohne Text
  ausgelassen (mit kurzem Hinweis).

## Freigaben im Panel

Steht eine Funktion im Panel auf **„erst nachfragen“**, hält das Panel den Aufruf an, bis du
im Panel unter **Freigaben** entschieden hast – höchstens 5 Minuten. So lange wartet Missa
(`MISSA_CONTROL_ZEITLIMIT`). Erlaubst du, läuft die Funktion und Missa sagt dir das Ergebnis.
Lehnst du ab oder läuft die Zeit ab, gibt Missa den fertigen Satz des Panels weiter, z. B.
„Die Aktion … wurde vom Besitzer im Panel abgelehnt und nicht ausgeführt.“

Im Hintergrund (Routinen, Nachtschicht) laufen schreibende Panel-Funktionen nur mit
`MISSA_CONTROL_IM_HINTERGRUND=alles`; sonst bekommt Missa einen klaren Satz zurück und schlägt
dir die Aktion im nächsten Gespräch vor.

## Ereignisse: Missa meldet sich von selbst

Was das Panel meldet – eine Freigabe wartet, eine Integration ist ausgefallen, eine Regel hat
ausgelöst, ein Gerät meldet etwas –, schreibt dir Missa über Nextcloud Talk, ohne dass du
fragst. Dafür hält Missa-Core den Ereignis-Stream des Panels (`/api/events/stream`) offen.

| Dringlichkeit im Panel | Was Missa tut |
|------------------------|---------------|
| `niedrig` | nur ins Log |
| `normal` | Hinweis (Alarm-Stufe `info`) |
| `hoch` | Wichtig (Alarm-Stufe `wichtig`) |
| `dringend` | Notfall (Alarm-Stufe `notfall`) – kommt auch in der Ruhezeit |

- Es gelten **Ruhezeit** und **Ruhefenster** der Alarme (siehe
  [server-alarme.md](server-alarme.md)): In der Ruhezeit warten Hinweise und Wichtiges bis zum
  Morgen; dasselbe Ereignis (gleiche Art, Quelle und Titel) kommt im Ruhefenster nur einmal.
- **Freigabe wartet:** kommt sofort, mit dem Text des Panels und dem Hinweis „Im Panel unter
  ‚Freigaben‘ erlauben“. In der Ruhezeit nur ins Log (ausser sie ist `dringend`) – eine
  Freigabe läuft nach 5 Minuten ab, eine Nachricht am Morgen wäre wertlos. Jede Freigabe
  kommt einzeln, auch zwei gleiche hintereinander.
- **Test-Ereignis** (im Panel: **Protokoll → Ereignisse → Test-Ereignis senden**): kommt als
  kurze Bestätigung „✅ Test-Ereignis aus Missa Control ist angekommen.“ – sofort, auch in der
  Ruhezeit, weil du es gerade selbst ausgelöst hast.
- Der Text kommt **wörtlich** aus Titel und Nachricht des Ereignisses, ohne Umweg über das
  Sprachmodell – schnell und zuverlässig auch auf dem CPU-Server.
- **Nichts doppelt, nichts verloren:** Missa-Core merkt sich die Nummer des letzten Ereignisses
  in der Datenbank (Tabelle `stand`). Nach einem Neustart von Missa-Core oder des Panels setzt
  der Stream genau dort fort (`Last-Event-ID`) und liefert Verpasstes nach.
- **Erster Start:** Missa beginnt bei „jetzt“ – ältere Ereignisse werden nicht nachgeliefert.
- Bricht die Verbindung ab, verbindet Missa neu: erst nach 2 Sekunden, dann mit wachsender
  Wartezeit bis höchstens 5 Minuten.
- `/health` zeigt unter `missa_control.ereignisse`, ob der Stream steht und welche Nummer
  zuletzt gemeldet wurde (`"aus"` bei `MISSA_CONTROL_EREIGNISSE=aus`).

## Wenn das Panel ausfällt

Missa-Core startet und arbeitet normal weiter. Die zuletzt bekannten Panel-Funktionen bleiben
sichtbar; ruft Missa eine auf, sagt sie: „Missa Control ist gerade nicht erreichbar“. Sobald
das Panel wieder läuft, geht es von selbst weiter. In `/health` steht dann
`"verbunden": false` mit der Ursache unter `fehler` (`/health` bleibt trotzdem 200).

Ist der Token falsch oder widerrufen (`401`), steht im Log:

```text
Missa Control: Token ungültig (401) – im Panel unter „Clients“ einen neuen Token anlegen …
```

Nachsehen: `sudo journalctl -u missa-core | grep "Missa Control"`.

## Kalender und Gedächtnis nicht doppelt einrichten

Missa-Core hat einen eigenen Nextcloud-Kalender (`termine_anzeigen`, `termin_eintragen` …)
und ein eigenes Gedächtnis (`merken`, `erinnern` …). Richte die Integrationen **Kalender** und
**Gedächtnis** deshalb nicht zusätzlich im Panel ein, solange Missa-Core sie selbst hat – sonst
sieht das Sprachmodell zwei Werkzeuge für dasselbe und wählt mal das eine, mal das andere.
Sind sie im Panel schon aktiv, kannst du sie für Missa-Core auslassen:

```bash
MISSA_CONTROL_AUSLASSEN=kalender_,gedaechtnis_
```

## Für Entwickler

- Verbindung: `src/missa/integrationen/missa_control.py` – kleiner eigener Client mit httpx
  für das zustandslose MCP-Gateway (`tools/list`, `tools/call`, Antworten als JSON oder SSE).
  Das offizielle SDK (`mcp` 2.x) bringt einen eigenen HTTP-Stack (`httpx2`) und
  Server-Abhängigkeiten mit; für zwei Aufrufe ist der eigene Client schlanker und lässt sich
  wie die anderen Gegenstellen mit `httpx.MockTransport` testen.
- Werkzeuge: `src/missa/tools/panel.py` tauscht die Gruppe `missa_control` im Werkzeugkasten
  mit `Werkzeugkasten.gruppe_ersetzen` in einem Schritt aus.
- Ereignisse: `src/missa/tools/panel_ereignisse.py` (Stream, Neuverbinden, Zustellung über
  `Alarme`), letzte Nummer über `stand_lesen`/`stand_setzen` in `src/missa/core/datenbank.py`.
- Tests: `tests/test_missa_control.py` und `tests/test_missa_control_ereignisse.py` mit dem
  Nachbau `tests/missa_control_fake.py`.
