# Kalender: volle Kontrolle für Missa

Missa erledigt im Nextcloud-Kalender fast alles selbst und sagt dir danach kurz, was sie
gemacht hat. Jede Änderung wird protokolliert und lässt sich zurücknehmen.

**Nur nach deinem Ja:** bestehende Termine **löschen** oder **verschieben**, Termine eintragen,
die sich mit anderen **überschneiden** oder für die die Zeit zum **Ortswechsel** nicht reicht,
und alles, wobei andere Personen eine E-Mail bekämen.

## Was Missa kann

| Du sagst | Missa macht |
|----------|-------------|
| „Trag mir morgen 14 Uhr Zahnarzt ein, rot“ | Termin mit Farbe – oder zuerst ein Hinweis, falls um 14 Uhr schon etwas ist |
| „Verschieb das Meeting morgen auf 15 Uhr und mach es rot“ | färbt sofort, fragt dann: „Soll ich es von 10 auf 15 Uhr verschieben?“ – verschoben wird nach deinem Ja (Dauer bleibt) |
| „Trag mir Freitag 14 Uhr Zahnarzt ein, erinnere mich 1 Tag vorher“ | Termin mit Erinnerung (VALARM) |
| „Jeden Montag 18 Uhr Training, bis Ende Jahr“ | Serie (RRULE) bis 31.12. |
| „Nächsten Montag fällt das Training aus“ | löscht nur dieses eine Vorkommen (EXDATE) |
| „Am 16. ist das Training erst um 19 Uhr“ | ändert nur dieses Vorkommen (Ausnahme mit RECURRENCE-ID) |
| „Die Vorlesung ist Lehre“ | Kategorie „Lehre“ – und damit automatisch die Farbe aus dem Farbsystem |
| „Leg einen Kalender Server in Grau an“ | neuer Kalender mit Farbe |
| „Mach den Kalender Arbeit orange“, „Benenn Arbeit in Job um“ | Kalender einfärben oder umbenennen |
| „Lösch den Termin“ | fragt nach, löscht nach deinem Ja – danach rückholbar |
| „Was habe ich morgen?“ | Termine plus Hinweise zu fehlenden Puffern und Pausen |
| „Mach das rückgängig“ | nimmt die letzte Änderung zurück (oder eine bestimmte Nummer) |
| „Was hast du zuletzt am Kalender geändert?“ | Protokoll mit Nummern |

Termine können Titel, Ort, Beschreibung, Farbe, Kategorien, Erinnerungen,
Wiederholungen und ganztägig sein. `termine_anzeigen` kann mit `kalender='alle'` alle
Kalender auf einmal zeigen. Ändern, verschieben und löschen finden einen Termin auch in
anderen Kalendern als dem Standardkalender.

## Farben

- Termin-Farbe: Eigenschaft `COLOR` (CSS-Farbname, RFC 7986) – so zeigt sie die
  Nextcloud-Kalender-App an.
- Kalender-Farbe: `calendar-color` (Hex-Wert).
- Erlaubt sind deutsche Namen (rot, dunkelblau, türkis, hellgrün …), CSS-Namen oder Hex-Werte
  wie `#E53935`.

**Farbsystem:** Missa ordnet Kategorien Farben zu. Standard: Arbeit = blau, Lehre = grün,
Privat = lila, Server = grau, Pausen = türkis, Familie = orange, Gesundheit = rot,
Sport = hellgrün. Ein Termin mit Kategorie und ohne eigene Farbe bekommt die Farbe der
Kategorie; ein neuer Kalender mit gleichem Namen auch. Ändern: „Lehre soll ab jetzt gelb
sein“ (Werkzeug `farbsystem_festlegen`). Gespeichert in `missa.db`.

## Protokoll und Rückgängig

Jede Änderung (Termin eintragen, ändern, löschen, Kalender anlegen, ändern) steht mit dem
Zustand **vorher und nachher** in `missa.db` (Tabelle `kalender_aenderungen`).

- „Mach das rückgängig“ nimmt die jüngste noch offene Änderung zurück, „mach Änderung #4
  rückgängig“ eine bestimmte.
- Ein gelöschter Termin kommt mit derselben UID zurück.
- Einen von Missa angelegten Kalender löscht sie beim Rückgängigmachen nur, solange er leer ist.
- Übersicht für Apps und das Admin-Panel: `GET /api/kalender/protokoll` (siehe [api.md](api.md)).

## Einladungen: nur nach Rückfrage

Sobald andere Personen betroffen sind (Teilnehmer mit E-Mail-Adresse), verschickt Nextcloud
E-Mails an sie – Einladungen, Änderungen oder Absagen. Darum fragt Missa in diesen Fällen
**immer vorher**, und erst dein Ja in der nächsten Nachricht zählt (dieselbe Sperre wie früher
beim Löschen). Nur Farbe, Kategorien oder eigene Erinnerungen ändern geht ohne Rückfrage, weil
dabei niemand eine E-Mail bekommt.

## Konflikte: Doppelbuchungen und Ortswechsel

Bevor Missa einen Termin einträgt oder verschiebt, schaut sie in **allen** Kalendern nach:

- **Doppelbuchung:** Überschneidet sich der Termin mit einem anderen, trägt sie nichts ein,
  sagt dir, womit, und schlägt eine freie Zeit am selben Tag vor („um 15:15 Uhr wäre Platz“).
- **Ortswechsel:** Haben zwei Termine verschiedene Orte, braucht es mindestens **30 Minuten**
  Abstand (`PAUSEN_WEGZEIT_MINUTEN`, per Gespräch änderbar). Einen externen Wegzeit-Dienst
  gibt es bewusst nicht – nur diese Regel.
- Sagst du „trag ihn trotzdem ein“, trägt sie ihn so ein. Ganztägige und als „frei“
  markierte Termine sowie Missas eigene Pausenblöcke zählen nicht als Konflikt.

Puffer und Pausen siehe [pausen.md](pausen.md).

## Hinweise

- Der Standardkalender ist `NEXTCLOUD_KALENDER`. Ihn benennt Missa nicht um (sonst fände
  sie ihn nach einem Neustart nicht mehr); einfärben geht.
- Termine ohne Zeitzone gelten in `TIMEZONE` (Standard Europe/Zurich).
