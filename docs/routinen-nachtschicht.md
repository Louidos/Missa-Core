# Routinen und Nachtschicht

Erinnerungen, Routinen und die Nachtschicht nutzen **denselben Zeitplaner** in Missa-Core:
alles steht in `MISSA_DATEN/missa.db`, überlebt Neustarts und Updates, und die Zeitzone ist
immer `TIMEZONE` (Standard `Europe/Zurich`, Sommer-/Winterzeit automatisch).

## Routinen – wiederkehrende Aufträge an Missa

Eine Routine ist keine reine Erinnerung, sondern eine **Anweisung an Missa**, die sie zur Zeit
selbst ausführt – mit ihren Werkzeugen (Kalender, Aufgaben, Gedächtnis …). Ihre Antwort
bekommst du als Nachricht (heute über Nextcloud Talk).

| Du sagst | Was passiert |
|----------|--------------|
| „Jeden Freitag um 17 Uhr Wochenrückblick“ | Routine anlegen, Missa nennt den nächsten Lauf |
| „Werktags um 7 schau in meinen Kalender und sag mir, was heute ansteht“ | Routine mit genauer Anweisung |
| „Welche Routinen hast du?“ | Liste mit Nummer, nächstem Lauf, pausiert ja/nein |
| „Pausier den Wochenrückblick“ / „Mach ihn wieder an“ | pausieren / fortsetzen (ohne verpasste Läufe nachzuholen) |
| „Lösch die Routine #4“ | Missa fragt nach – gelöscht wird erst nach deinem Ja |
| „Führ den Wochenrückblick jetzt mal aus“ | einmaliger Lauf sofort, der Zeitplan bleibt |

Die Zeitangabe versteht der Code (wie bei Erinnerungen, siehe [erinnerungen.md](erinnerungen.md)).
Eine Routine braucht eine Wiederholung („jeden …“, „täglich“, „werktags“, „jeden Monat am 15.“);
für etwas Einmaliges nimmt Missa eine Erinnerung oder die Nachtschicht.

**War ai01 zur Zeit aus**, läuft die Routine direkt nach dem Start einmal nach (mit Hinweis,
dass sie verspätet ist), danach geht es mit dem nächsten regulären Termin weiter.
Wartet der letzte Lauf noch (das Modell ist auf der CPU langsam), wird kein zweiter gestartet.

## Nachtschicht – lange Aufgaben nachts erledigen

Das Modell läuft vorerst nur auf der CPU. Was lange dauert, kann Missa in der Nacht erledigen:

- „Mach mir heute Nacht eine Übersicht aller offenen Aufgaben“ → Nachtaufgabe vorgemerkt
- Sie läuft im **Nachtfenster** (Standard **01:00–06:00**), eine Aufgabe nach der anderen.
- Am **Ende des Fensters** kommt der Morgenbericht: „🌙 Guten Morgen! Das habe ich heute Nacht
  erledigt: …“ Wird eine Aufgabe erst nach dem Fenster fertig, kommt ihr Ergebnis sofort.
- Im **nächsten Gespräch** erwähnt Missa die neuen Ergebnisse einmal von sich aus.
- „Was hast du heute Nacht gemacht?“, „Zeig mir das ganze Ergebnis von #2“ – jederzeit abrufbar.
- „Nachtschicht von 2 bis 5 Uhr“ ändert das Fenster **dauerhaft** (gespeichert in der Datenbank,
  gilt auch nach Neustart und Update). Fenster über Mitternacht gehen auch („von 23 bis 5“).
- Startet eine Nachtaufgabe nicht in diesem Fenster, wartet sie bis zur nächsten Nacht.

Wird Missa-Core mitten in einer Aufgabe neu gestartet, läuft sie danach erneut. Klappt eine
Aufgabe nicht (z. B. llama-server nicht erreichbar), versucht Missa es nach 5 bzw. 10 Minuten
noch einmal; nach drei Versuchen steht sie als „fehlgeschlagen“ im Bericht.

Standardwerte in `/etc/missa/missa.env` (optional):

```
NACHTSCHICHT_VON=01:00
NACHTSCHICHT_BIS=06:00
```

Im Gespräch geänderte Zeiten haben Vorrang vor diesen Werten.

## Sicherheit im Hintergrund

Bei Routinen und Nachtaufgaben ist Louis nicht im Gespräch. Darum gilt im Code (nicht nur im
Prompt):

- **Keine Zustimmung im Hintergrund:** Rückfragen (Löschen, Einladungen …) können dort nie
  bestätigt werden – das geht nur in einem Gespräch mit Louis.
- **Nie eine E-Mail senden:** Die Senden-Sperre ignoriert Hintergrund-Aufträge vollständig.
  Ein Entwurf, den eine Routine vorbereitet, wird dir gezeigt, gilt aber nicht als „gesehen“ –
  ein späteres „Ja, abschicken“ sendet ihn nicht, bis du ihn im Gespräch anschaust.
- **Keine neuen Routinen oder Nachtaufgaben** aus dem Hintergrund (keine Endlosschleifen).
- Jede schreibende Aktion (anlegen, pausieren, löschen, Fenster ändern, jeder Lauf) steht im Log
  (`journalctl -u missa-core`).

## Wie es funktioniert

- Routinen stehen in der Tabelle `erinnerungen` mit `art = 'routine'` und laufen über dieselbe
  Schleife wie Erinnerungen (`src/missa/core/zeitplaner.py`). Die neuen Spalten ergänzt Missa-Core
  beim Start automatisch (Migration, alte Erinnerungen bleiben erhalten).
- Fällige Routinen und Nachtaufgaben kommen in die Tabelle `hintergrund_auftraege`. Ein eigener
  Hintergrund-Task (`src/missa/core/hintergrund.py`) übergibt sie nacheinander an Missa; der Chat
  bleibt dabei frei.
- Ergebnisse gehen über die gemeinsame Zustellung (`src/missa/core/zustellung.py`) raus – mit
  denselben Wiederholversuchen wie Erinnerungen. Neue Kanäle (Push, Telefon) kommen dort dazu.
- Im Gespräch geänderte Einstellungen stehen in der Tabelle `vorgaben` (`src/missa/core/vorgaben.py`).

## Für Apps und das Admin-Panel

- `GET /api/routinen` und `GET /api/nachtschicht` (siehe [api.md](api.md)).
- Werkzeuge: `routine_anlegen`, `routinen_anzeigen`, `routine_pausieren`, `routine_fortsetzen`,
  `routine_loeschen`, `routine_jetzt_ausfuehren`, `nachtaufgabe_anlegen`, `nachtaufgaben_anzeigen`,
  `nachtaufgabe_ergebnis`, `nachtaufgabe_loeschen`, `nachtschicht_einstellen`.
