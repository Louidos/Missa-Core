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

## Einstellungen

| Variable | Standard | Bedeutung |
|---|---|---|
| `INITIATIVE_LAGEBILD_ZEICHEN` | 3000 | Höchstlänge des Lagebilds (mindestens 1000) |
