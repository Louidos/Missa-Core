# Pausen und Puffer

Missa achtet von sich aus darauf, dass in deinem Kalender Luft bleibt. Sie **weist hin und
schlägt vor – eingetragen wird erst nach deinem Ja.**

## Was sie prüft

| Prüfung | Grenze (Standard) | Einstellung |
|---------|-------------------|-------------|
| Puffer zwischen zwei Terminen | mindestens 15 Min. | `PAUSEN_PUFFER_MINUTEN` |
| Pause nach Stunden am Stück | nach spätestens 3 Std. mindestens 15 Min. Lücke | `PAUSEN_NACH_STUNDEN`, `PAUSEN_PAUSE_MINUTEN` |
| Mittagspause | 30 Min. zwischen 11:30 und 14:00 | `PAUSEN_MITTAG_MINUTEN`, `PAUSEN_MITTAG_VON`, `PAUSEN_MITTAG_BIS` |
| zu voller Tag | mehr als 8 Stunden Termine | `PAUSEN_MAX_STUNDEN` |
| Abstand bei verschiedenen Orten | mindestens 30 Min. (Regel, kein Wegzeit-Dienst) | `PAUSEN_WEGZEIT_MINUTEN` |
| Überschneidungen | – | – |

**Im Gespräch ändern:** „Mach 20 Minuten Puffer“, „Pause schon nach 2 Stunden“, „Bei
Ortswechsel 45 Minuten Abstand“ (Werkzeug `pausen_regeln_einstellen`), „Welche
Pausen-Regeln hast du?“ (`pausen_regeln_anzeigen`). Die Werte stehen in der Tabelle
`vorgaben` in `missa.db`, gelten sofort und auch nach einem Neustart – und haben Vorrang vor
den `PAUSEN_*`-Werten in `/etc/missa/missa.env`.

Berücksichtigt werden alle Kalender. Ganztägige Termine und Termine, die als „frei“ markiert
sind, zählen nicht. Termine mit „Pause“, „Mittag“, „Lunch“ oder „Essen“ im Titel zählen als
Pause. Eine Mittagspause plant Missa an Tagen mit mindestens zwei Terminen oder einem Termin
über Mittag.

## Wann sie prüft

- **Jeden Abend** um `PAUSEN_PRUEFZEIT` (Standard 19:00) den nächsten Tag. Sie schreibt dir,
  was ihr aufgefallen ist, und schlägt Blöcke vor: „Mein Vorschlag: ⏸️ Puffer 13:45–14:00,
  🍽️ Mittagspause 12:00–12:30. Soll ich dir das eintragen?“ Erst auf dein „Ja, trag ein“
  trägt sie die Blöcke im Kalender `PAUSEN_KALENDER` ein (Standard „Pausen“, Farbe
  `PAUSEN_FARBE`, wird bei Bedarf angelegt).
  - Termine direkt hintereinander → Hinweis und Vorschlag für ⏸️-Puffer davor und danach.
  - Mehr als 3 Stunden am Stück → Hinweis mit Vorschlag, wann eine Pause passt.
  - Kein Platz für eine Mittagspause → Vorschlag für einen 🍽️-Block, bevorzugt um 12:00.
  - Zu voll oder zu knapper Ortswechsel → Hinweis (verschoben wird nichts von selbst).
- **In der Tagesübersicht** („Was habe ich morgen?“): dieselben Hinweise direkt dabei.
- **Bei jeder neuen oder verschobenen Terminanlage:** Macht der Termin einen Puffer kaputt,
  trägt Missa ihn ein, sagt es dir aber sofort und schlägt eine bessere Zeit vor. Bei einer
  **Doppelbuchung** oder einem zu knappen **Ortswechsel** trägt sie erst gar nicht ein, sondern
  fragt vorher (siehe [kalender.md](kalender.md)).
- **Auf Wunsch:** „Prüf mal morgen auf Pausen“ (Werkzeug `tag_pruefen`) – und nach deinem
  Ja „trag die Pausen ein“.

Jeder Block ist als Missas Block markiert (`X-MISSA-ART`), wird protokolliert und lässt
sich wie jede Kalenderänderung mit „mach das rückgängig“ zurücknehmen
(siehe [kalender.md](kalender.md)).

Ausschalten: `PAUSEN_AKTIV=false`.
