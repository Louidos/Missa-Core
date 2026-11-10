# Pausen und Puffer

Missa achtet von sich aus darauf, dass in deinem Kalender Luft bleibt – und greift ein.

## Was sie prüft

| Prüfung | Grenze (Standard) | Einstellung |
|---------|-------------------|-------------|
| Puffer zwischen zwei Terminen | mindestens 10 Min. | `PAUSEN_PUFFER_MINUTEN` |
| Mittagspause | 30 Min. zwischen 11:30 und 14:00 | `PAUSEN_MITTAG_MINUTEN`, `PAUSEN_MITTAG_VON`, `PAUSEN_MITTAG_BIS` |
| zu voller Tag | mehr als 8 Stunden Termine | `PAUSEN_MAX_STUNDEN` |
| Wegzeit bei verschiedenen Orten | mindestens 30 Min. | `PAUSEN_WEGZEIT_MINUTEN` |
| Überschneidungen | – | – |

Berücksichtigt werden alle Kalender. Ganztägige Termine und Termine, die als „frei“ markiert
sind, zählen nicht. Termine mit „Pause“, „Mittag“, „Lunch“ oder „Essen“ im Titel zählen als
Pause. Eine Mittagspause plant Missa an Tagen mit mindestens zwei Terminen oder einem Termin
über Mittag.

## Wann sie prüft

- **Jeden Abend** um `PAUSEN_PRUEFZEIT` (Standard 19:00) den nächsten Tag. Fehlen Puffer
  oder die Mittagspause, trägt Missa selbst Blöcke im Kalender `PAUSEN_KALENDER` ein
  (Standard „Pausen“, Farbe `PAUSEN_FARBE`, wird bei Bedarf angelegt) und schreibt dir
  danach, was ihr aufgefallen ist und was sie eingetragen hat.
  - Drei Termine direkt hintereinander → Hinweis und ⏸️-Pufferblöcke davor und danach
    (eine kurze Lücke dazwischen wird auch als Puffer markiert).
  - Kein Platz für eine Mittagspause → 🍽️-Block, bevorzugt um 12:00.
  - Ist es zu voll oder fehlt Wegzeit, gibt es einen Hinweis (Termine verschiebt sie dafür
    nicht von selbst).
- **Bei jeder neuen oder verschobenen Terminanlage** (auch im Chat): Macht der Termin einen
  Puffer kaputt, sagt Missa es dir sofort und schlägt eine Alternative vor („Der Termin hätte
  um 11:10 Uhr mit genug Puffer Platz – soll ich ihn verschieben?“).
- **Auf Wunsch:** „Prüf mal morgen auf Pausen“ oder „… und trag die Pausen ein“
  (Werkzeug `tag_pruefen`).

Jeder Block ist als Missas Block markiert (`X-MISSA-ART`), wird protokolliert und lässt
sich wie jede Kalenderänderung mit „mach das rückgängig“ zurücknehmen
(siehe [kalender.md](kalender.md)). Ein zweiter Lauf trägt nichts doppelt ein.

Ausschalten: `PAUSEN_AKTIV=false`.
