# Missa Initiative

Missa soll nicht nur reagieren, sondern von sich aus merken, wenn gerade etwas wichtig ist.
Dafür gibt es mehrere Bausteine, die aufeinander aufbauen. Diese Seite beschreibt alle.

## Lagebild

Das Lagebild ist ein kompaktes Bild davon, was gerade los ist. Missa baut es aus allen
Diensten, die eingerichtet sind – fehlt ein Dienst, fehlt einfach sein Abschnitt:

| Abschnitt | Woher | Inhalt |
|---|---|---|
| Uhrzeit | immer | Wochentag, Datum, Uhrzeit |
| Termine | Nextcloud-Kalender | Termine heute und morgen (alle Kalender) |
| Erinnerungen | Zeitplaner | offene Erinnerungen der nächsten 24 Stunden |
| Alarme | Alarme | Alarme der letzten 24 Stunden, auch aufgeschobene (Ruhezeit) |
| Server | Monitoring | aktive Server-Alarme, Messquellen, die nicht antworten |
| Erreichbarkeit | `ERREICHBARKEIT_DOMAINS` | gestörte Domains |
| Hintergrund-Aufträge | Routinen, Nachtschicht | wartende, laufende und in den letzten 24 Stunden fertige Aufträge |
| Mails | E-Mail | Anzahl ungelesener Mails und Absender (keine Betreffe) |
| Kontaktpflege | Kontakte | wichtige Personen, bei denen der Abstand überschritten ist |
| Letzte Nachricht von Louis | Gesprächsverlauf | wann du zuletzt geschrieben hast |

So fragst du danach: „Was ist gerade los?“, „Gibt's was Neues?“ (Werkzeug
`lagebild_anzeigen`, Stufe `lesen`). Missa Control und andere Apps lesen es über
[`GET /api/lagebild`](api.md#get-apilagebild--was-ist-gerade-los).

Regeln:

- **Fehlertolerant:** Wirft eine Quelle einen Fehler oder braucht sie länger als
  10 Sekunden, steht im Lagebild „<Quelle>: nicht erreichbar“. Der Rest kommt trotzdem.
- **Begrenzt:** Das Lagebild ist höchstens `INITIATIVE_LAGEBILD_ZEICHEN` Zeichen lang
  (Standard 3000, mindestens 1000). Wird es länger, werden alle langen Abschnitte
  gleichmässig gekürzt (mit „…“); kurze bleiben ganz, keine Quelle fällt weg.
- **Fremdes markiert:** Kalendertitel, Absender, Alarm-Texte (können aus Logs stammen) und
  Ergebnisse von Hintergrund-Aufträgen sind fremder Inhalt. Für das Modell werden sie wie
  überall mit `fremd_markieren` gekennzeichnet; danach braucht auch Schreibendes Louis' Ja.
- **Fingerabdruck:** Ein Hash über alle Abschnitte ausser der Uhrzeit. Bleibt er gleich, hat
  sich an der Lage nichts geändert – die Denkschleife spart sich dann den Modellaufruf.
- **Nur lesend:** Das Lagebild legt nichts an und ändert nichts.

## Denkschleife

Mit `INITIATIVE_AKTIV=true` schaut Missa alle `INITIATIVE_INTERVALL_MINUTEN` Minuten
(Standard 15, mindestens 5) von selbst aufs Lagebild und entscheidet:

- **nichts** – alles normal oder schon bekannt (der Normalfall),
- **notieren** – gut zu wissen, aber nicht eilig,
- **melden** – Louis sollte es bald wissen (Stufe `info` oder `wichtig`).

Standardmässig ist die Denkschleife **aus**: Nach einem Update ändert sich nichts von selbst.

### Ablauf eines Laufs

1. Übersprungen (mit Ursache vermerkt) wird, wenn der Notfall-Stopp aktiv ist, gerade eine
   Chat-Anfrage oder ein Hintergrund-Auftrag läuft, das Lagebild seit der letzten
   Entscheidung unverändert ist (gleicher Fingerabdruck) oder llama-server nicht gesund ist.
2. Sonst fragt Missa das Modell mit einem eigenen, kurzen Systemprompt: Charakter, Aufgabe
   und Lagebild – **ohne Werkzeuge**.
3. Die Antwort muss ein JSON-Objekt sein:
   `{"aktion": "nichts|notieren|melden", "stufe": "info|wichtig", "schluessel": "kalender-pausen", "titel": "…", "text": "…", "grund": "…"}`
   – mehrere Punkte als `{"punkte": [ … ]}`.
4. Der Code prüft streng: unlesbares JSON, eine unbekannte Aktion oder Stufe und
   `stufe = notfall` zählen als `nichts` und werden mit Grund vermerkt.
5. Jeder Lauf steht in der Tabelle `initiative_laeufe` (Zeit, Fingerabdruck, gekürztes
   Lagebild, Entscheidung, Grund, Dauer, übersprungen ja/nein mit Ursache, Fehler). Alle
   Texte werden vorher mit `schwaerzen()` behandelt.

### Sicherheit

- Die Denkschleife kann **nie einen Notfall** auslösen – das bleibt den festen Regeln
  (Server, Erreichbarkeit, Logins …) vorbehalten.
- Sie ruft keine Werkzeuge auf. Antwortet das Modell trotzdem mit einem Werkzeug-Aufruf, wird
  er ignoriert.
- Der Lauf zählt als Hintergrund (`im_hintergrund`): keine Bestätigungen, kein Mailversand,
  keine neuen Routinen oder Nachtaufgaben. Sonderrechte gibt es keine.
- Es läuft nie mehr als ein Durchlauf gleichzeitig.
- Fehler beim Modellaufruf brechen den Dienst nie ab; beim nächsten Intervall geht es neu
  los (auch wenn das Lagebild gleich geblieben ist).

## Einstellungen

| Variable | Standard | Bedeutung |
|---|---|---|
| `INITIATIVE_LAGEBILD_ZEICHEN` | 3000 | Höchstlänge des Lagebilds (mindestens 1000) |
| `INITIATIVE_AKTIV` | `false` | Denkschleife an oder aus |
| `INITIATIVE_INTERVALL_MINUTEN` | 15 | Abstand zwischen zwei Läufen (mindestens 5) |
