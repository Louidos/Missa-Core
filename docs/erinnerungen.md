# Erinnerungen

Missa erinnert dich auf die Minute genau – einmalig oder wiederkehrend – und vergisst
dabei auch nach einem Neustart von ai01 nichts.

## So sagst du es

| Du sagst | Missa erinnert dich |
|----------|---------------------|
| „Erinnere mich um 6:30 Uhr an Training“ | heute um 6:30 – oder morgen, wenn 6:30 schon vorbei ist |
| „… in 20 Minuten“, „… in einer halben Stunde“ | genau so viel später |
| „… morgen um 8“, „… übermorgen früh“, „… heute Abend um 8“ | an diesem Tag |
| „… am Freitag um 14 Uhr“, „… nächsten Montag um 9“, „… am 24.12. um 18 Uhr“ | an diesem Datum |
| „… jeden Montag um 7“, „… werktags um 6:30“, „… täglich um 21 Uhr“ | immer wieder |
| „… jeden Montag und Donnerstag um 18 Uhr“, „… jeden Monat am 15. um 9“ | immer wieder |

Missa bestätigt jede Erinnerung mit Wochentag, Datum und Uhrzeit. Ist eine Angabe
mehrdeutig, fragt sie kurz nach, z. B. „um 6“, wenn es schon 7 Uhr ist:
„Meinst du heute um 18:00 Uhr oder morgen um 06:00 Uhr?“

Weitere Befehle im Chat:

- „Welche Erinnerungen hast du?“ – Liste mit Nummern
- „Verschieb die Erinnerung #3 auf 9 Uhr“, „Mach daraus nur noch eine einmalige“
- „Lösch die Erinnerung ans Training“
- „In 10 Minuten nochmal“ – schlummert die zuletzt geschickte Erinnerung

## Wie es funktioniert

- Die Zeitangabe rechnet **der Code** um, nicht das Sprachmodell
  (`src/missa/core/zeitangaben.py`). Zeitzone ist immer `TIMEZONE` (Standard
  `Europe/Zurich`), Sommer- und Winterzeit stimmen automatisch: „jeden Montag um 7“
  bleibt 7:00 Uhr, auch nach der Zeitumstellung.
- Erinnerungen stehen in der Datenbank `MISSA_DATEN/missa.db` (Tabelle `erinnerungen`) und
  überleben Neustarts und Updates.
- Ein Hintergrund-Task in Missa-Core wartet genau bis zum nächsten Zeitpunkt und stellt die
  Nachricht zu – in der Regel weniger als eine Sekunde später.
- **War ai01 aus**, holt Missa verpasste Erinnerungen direkt nach dem Start nach und schreibt
  dazu: „Verspätet: Eigentlich war das für … geplant“. Bei wiederkehrenden Erinnerungen
  kommt nur eine Nachricht, danach geht es mit dem nächsten regulären Termin weiter.
- Klappt die Zustellung nicht (z. B. Nextcloud gerade nicht erreichbar), versucht Missa es
  erneut – nach 30 Sekunden, dann mit wachsendem Abstand bis alle 15 Minuten.

## Zustellung

Erinnerungen gehen heute über **Nextcloud Talk** in die Unterhaltung
`NC_TALK_NOTIFY_CONVERSATION` (siehe [nextcloud-talk.md](nextcloud-talk.md)). Ohne diesen
Wert speichert Missa die Erinnerungen trotzdem, sagt dir aber ehrlich, dass sie dir gerade
nicht schreiben kann; sobald der Kanal eingerichtet ist, kommen sie (verspätet) an.

Weitere Kanäle (Telefon, App) kommen später als zusätzlicher Kanal dazu
(`src/missa/core/zustellung.py`), ohne dass sich an den Erinnerungen etwas ändert.

## Für Apps und das Admin-Panel

- Die Werkzeuge `erinnerung_anlegen`, `erinnerungen_anzeigen`, `erinnerung_aendern`,
  `erinnerung_loeschen` und `erinnerung_schlummern` stehen in `/health` unter `werkzeuge`.
- `GET /api/erinnerungen` listet alle geplanten Erinnerungen (siehe [api.md](api.md)).
