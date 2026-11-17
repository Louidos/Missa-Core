# Missa Initiative

Missa soll nicht nur reagieren, sondern von sich aus merken, wenn gerade etwas wichtig ist.
Dafür gibt es mehrere Bausteine, die aufeinander aufbauen: Lagebild, Denkschleife, Meldungen,
Nerv-Budget, Gedanken-Journal, Lernen aus Rückmeldungen und das Briefing nach einer Auszeit.
Diese Seite beschreibt alle.

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

## Meldungen

Was die Denkschleife entscheidet, setzt der Code so um:

- **melden** läuft über die vorhandenen Alarm-Stufen (`Alarme.melden` mit
  `quelle = "initiative"`, der Stufe und dem Schlüssel `initiative:<thema>`). Damit gelten
  Ruhezeit und Ruhefenster automatisch: Nachts wartet eine Meldung bis zum Ende der
  Ruhezeit, und dasselbe Thema kommt innerhalb des Ruhefensters (Standard 60 Minuten,
  `alarm_einstellungen`) nicht noch einmal – ausser es ist dringender geworden. Einen eigenen
  Zustellweg gibt es nicht.
- **Gebündelt:** Mehrere Punkte in einem Lauf ergeben **eine** Nachricht mit einer kurzen
  Liste („3 Dinge, die mir aufgefallen sind“). Die Stufe ist die höchste der Punkte.
- **notieren** schreibt still in die Tabelle `initiative_notizen` (Zeit, Schlüssel, Text,
  erledigt). Offene Notizen erwähnt Missa im nächsten Gespräch **einmal** von sich aus
  (Kontextquelle wie bei den Ergebnissen der Nachtschicht); danach gelten sie als erledigt.
  Eine noch offene Notiz zum selben Thema wird ersetzt statt verdoppelt.
- Titel und Text schreibt Missa in ihrem Ton (der Charakter steht im Prompt der
  Denkschleife), nennt aber immer klar, worum es geht.
- Was aus jedem Punkt geworden ist (`gemeldet`, `notiert`, `im Ruhefenster schon gemeldet`),
  steht im Lauf in `initiative_laeufe`.

Eine Anruf-Stufe gibt es nicht: Es ist noch kein Telefon-Kanal eingerichtet. Kommt einer dazu,
wird er als weiterer Kanal bei `Zustellung` (`src/missa/core/zustellung.py`) registriert – die
Denkschleife selbst bleibt dabei unverändert und meldet höchstens `wichtig`.

## Nerv-Budget

Missa stört von sich aus nur begrenzt oft:

- Höchstens `INITIATIVE_BUDGET_PRO_TAG` Meldungen pro Tag (Standard 5). Gezählt werden nur
  Meldungen mit `quelle = "initiative"`, pro Kalendertag in `TIMEZONE` – und zwar an dem Tag,
  an dem sie zugestellt werden (eine nachts aufgeschobene zählt für den Morgen).
- Ist das Budget aufgebraucht, wird aus `melden` automatisch `notieren`; der Lauf vermerkt
  „Tagesbudget aufgebraucht“.
- Alarme aus festen Regeln (Server, Erreichbarkeit, Logins, Erinnerungen, Routinen) zählen
  **nie** mit und werden nie zurückgehalten.
- Sind abends Notizen offen, kommt um `INITIATIVE_SAMMELZEIT` (Standard 19:30) **eine**
  Sammelmeldung mit allen (Stufe `info`, `quelle = "initiative-sammlung"`). Sie zählt nicht
  gegen das Budget. Danach gelten die Notizen als erledigt.

Im Gespräch:

| Du sagst | Werkzeug | Wirkung |
|---|---|---|
| „Stör mich höchstens dreimal am Tag“ | `initiative_einstellen` (`budget_pro_tag=3`) | Budget 3, bleibt nach einem Neustart |
| „Denk mal eine Stunde nicht nach“ | `initiative_einstellen` (`pause_minuten=60`) | Läufe werden eine Stunde übersprungen |
| „Denk nur noch alle 30 Minuten nach“ | `initiative_einstellen` (`intervall_minuten=30`) | neues Intervall, sofort eingeplant |
| „Schick mir die Sammlung um 18 Uhr“ | `initiative_einstellen` (`sammelzeit="18:00"`) | neue Sammelzeit |
| „Schalt deine Denkschleife aus“ | `initiative_einstellen` (`aktiv=false`) | keine Läufe mehr |
| „Wie oft hast du dich heute gemeldet?“ | `initiative_status` | an/aus, Intervall, heute verbraucht, letzter Lauf |

`initiative_einstellen` hat die Stufe `schreiben`, `initiative_status` die Stufe `lesen`. Die
Werte stehen in `vorgaben` und haben Vorrang vor `missa.env`.

## Gedanken-Journal

Was Missa wann gedacht und warum sie so entschieden hat, steht im Journal (Tabelle
`initiative_laeufe`) – nachlesbar im Gespräch und über die API:

- „Warum hast du mir das geschrieben?“ → Werkzeug `gedanken_anzeigen` (`frage=warum`, Stufe
  `lesen`): Zeitpunkt, Titel und Grund der letzten eigenen Meldung.
- „Was hast du heute gedacht?“ → `gedanken_anzeigen` (`frage=heute`, optional `tage`): alle
  Läufe mit Entscheidung und Grund, übersprungene zusammengefasst („12× Lagebild unverändert“).
- [`GET /api/initiative/journal`](api.md#get-apiinitiativejournal--gedanken-journal) und
  [`GET /api/initiative`](api.md#get-apiinitiative--zustand-der-denkschleife) für Missa
  Control (nur lesend, nur mit API-Key).

Aufräumen: Ein täglicher Auftrag im Zeitplaner (04:20) löscht Läufe, die älter als
`INITIATIVE_JOURNAL_TAGE` (Standard 30) sind. Übersprungene Läufe ohne Änderung werden schon
nach 2 Tagen gelöscht, damit das Journal nicht volläuft. Alle Texte im Journal sind vor dem
Speichern geschwärzt (Passwörter, Tokens, Schlüssel).

## Lernen aus Rückmeldungen

Antwortest du **innerhalb von 30 Minuten** nach einer eigenen Meldung von Missa kurz mit
einer klaren Bewertung, merkt sie sich das (Tabelle `initiative_rueckmeldungen`: Zeit,
Schlüssel, gut/schlecht, Wortlaut):

| Bewertung | Wörter (Auszug) |
|---|---|
| gut | „war gut“, „danke, hilfreich“, „gut so“, „guter Hinweis“, „gut zu wissen“, „mehr davon“, 👍 |
| schlecht | „lass das“, „nerv nicht damit“, „nervt“, „nicht hilfreich“, „brauch ich nicht“, „bitte nicht mehr“, 👎 |

- Erkannt wird im **Code** über feste Wortlisten (`RUECKMELDUNG_WOERTER` in
  `core/initiative.py`, wie `STUFEN_WOERTER`), nicht über das Modell. „Schlecht“ wird zuerst
  geprüft („nicht hilfreich“ ist keine gute Bewertung).
- Ein blosses „danke“ zählt nicht, ebenso längere Nachrichten (mehr als 12 Wörter) und
  Antworten in einer anderen Unterhaltung als der, in der die Meldungen ankommen
  (`NC_TALK_NOTIFY_CONVERSATION`). Gibt es keine eigene Meldung der letzten 30 Minuten, ist
  „lass das“ einfach Teil des Gesprächs.
- Nach einer schlechten Rückmeldung ist das Thema `INITIATIVE_STUMM_TAGE` (Standard 14) Tage
  **stumm**: Will die Denkschleife dazu melden, wird nur notiert. Das erzwingt der Code, nicht
  der Prompt. Eine neue gute Rückmeldung zum Thema hebt das auf.
- Die letzten Rückmeldungen stehen als kurze Liste im Prompt der Denkschleife
  („gern gesehen: …“, „nicht erwünscht: …“).
- Rückmeldungen ändern **nie** etwas an Alarmen aus festen Regeln (Server, Erreichbarkeit,
  Logins, Erinnerungen, Routinen).
- Reaktionen (👍/👎) direkt auf Bot-Nachrichten in Talk werden noch nicht ausgewertet: Der
  Talk-Webhook verarbeitet nur Text-Nachrichten (`Create`), und Missa kennt die IDs ihrer
  eigenen Meldungen nicht. Ein 👍 oder 👎 als eigene **Nachricht** zählt aber.

Im Gespräch:

- „Was hast du über meine Rückmeldungen gelernt?“ → `initiative_gelerntes_anzeigen` (Stufe
  `lesen`)
- „Vergiss, was du über meine Rückmeldungen gelernt hast“ →
  `initiative_gelerntes_zuruecksetzen` (Stufe `zerstoererisch`, nur nach deinem Ja)

## Briefing nach einer Auszeit

War ai01 aus (Neustart, Update, Stromausfall), bekommt Louis danach **eine**
Zusammenfassung statt vieler Einzelmeldungen:

- Missa-Core vermerkt jede Minute „zuletzt gelaufen“ (Tabelle `herzschlag`). Beim Start
  ergibt sich daraus die Dauer der Auszeit.
- Ab `BRIEFING_AB_MINUTEN` (Standard 30) Auszeit kommt nach dem Start ein Briefing: wie lange
  Missa weg war, welche Erinnerungen und Routinen nachgeholt werden, was während der Ruhezeit
  aufgelaufen ist, heutige Termine, ungelesene Mails (Anzahl und Absender), Auffälligkeiten am
  Server und fertige Nachtaufgaben. Kürzere Neustarts bleiben still.
- Das Nachholen verpasster Erinnerungen und Routinen und der Morgenbericht der Nachtschicht
  bleiben unverändert – das Briefing fasst nur zusammen.
- Das Briefing wird aus Daten im Code gebaut und steht sofort als schlichte Fassung im
  Zeitplaner (geht also auch raus, wenn danach etwas schiefgeht). Ist das Modell erreichbar,
  formuliert Missa es in ihrem Ton; sonst kommt die schlichte Fassung (spätestens nach
  3 Minuten).
- In der Ruhezeit wartet das Briefing bis zu deren Ende. Fallen Briefing und Ende der Ruhezeit
  zusammen, gibt es **nur eine** Nachricht: Das Briefing übernimmt die Sammelmeldung, auch für
  Alarme, die bis dahin noch dazukommen. `notfall` kommt weiterhin sofort.
- Das Briefing zählt nicht gegen das Tagesbudget der Denkschleife.

### Sammelmeldung am Ende der Ruhezeit

Unabhängig vom Briefing kommen in der Ruhezeit aufgeschobene Alarme der Stufen `info` und
`wichtig` jetzt am Ende der Ruhezeit als **eine** Sammelmeldung statt einzeln (siehe
[server-alarme.md](server-alarme.md)). Damit ist auch der offene Punkt „morgens
zusammengefasst“ aus [stand.md](stand.md) erledigt.

## Einstellungen

| Variable | Standard | Bedeutung |
|---|---|---|
| `INITIATIVE_LAGEBILD_ZEICHEN` | 3000 | Höchstlänge des Lagebilds (mindestens 1000) |
| `INITIATIVE_AKTIV` | `false` | Denkschleife an oder aus |
| `INITIATIVE_INTERVALL_MINUTEN` | 15 | Abstand zwischen zwei Läufen (mindestens 5) |
| `INITIATIVE_BUDGET_PRO_TAG` | 5 | Höchstens so viele Meldungen pro Tag (0 = nie) |
| `INITIATIVE_SAMMELZEIT` | 19:30 | Uhrzeit der abendlichen Sammelmeldung |
| `INITIATIVE_JOURNAL_TAGE` | 30 | So lange bleiben Läufe im Gedanken-Journal |
| `INITIATIVE_STUMM_TAGE` | 14 | So lange ist ein Thema nach „lass das“ stumm |
| `BRIEFING_AKTIV` | `true` | Briefing nach einer Auszeit an oder aus |
| `BRIEFING_AB_MINUTEN` | 30 | Ab so vielen Minuten Auszeit gibt es ein Briefing |
