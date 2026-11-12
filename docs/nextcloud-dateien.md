# Aufgaben, Notizen und Dateien in Nextcloud

Alles läuft über dieselben Zugangsdaten wie Kalender und Kontakte (`NEXTCLOUD_URL`,
`NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD`). Nichts verlässt dein Netz.

## To-dos aus Gesprächen (Nextcloud Tasks)

Aufgaben liegen in der Liste `NEXTCLOUD_AUFGABEN_LISTE` (CalDAV, VTODO) und erscheinen in der
App „Aufgaben“. Neben „Schreib auf: Milch kaufen“ erkennt Missa auch, wenn du **nebenbei**
sagst, dass du etwas noch tun musst:

| Du sagst | Missa |
|----------|-------|
| „Ich muss noch Rail-Kit bestellen“ | legt „Rail-Kit bestellen“ an und sagt: „Hab ich dir als Aufgabe notiert.“ |
| „Nicht vergessen: Steuererklärung abgeben“ | Aufgabe „Steuererklärung abgeben“ |
| „Ich sollte morgen unbedingt Oma anrufen“ | Aufgabe „Oma anrufen“ (Fälligkeit nur, wenn du eine nennst) |
| „Muss ich noch was kaufen?“ | nichts – das ist eine Frage |

Der Code erkennt solche Sätze (`src/missa/tools/todo_erkennung.py`) und gibt dem Modell einen
Hinweis; angelegt wird die Aufgabe als Werkzeug, darum kannst du sofort widersprechen
(„Nee, lass das“ → „Hab ich erledigt“ oder löschen nach Rückfrage). Eine gleichnamige offene
Aufgabe wird nicht doppelt angelegt.

**Deck** (Kanban-Boards) ist nicht angebunden: Es hat eine eigene API, und für To-dos reicht
Tasks. Kommt das später, wird es ein weiteres Werkzeug.

## Notizen (Nextcloud Notes)

Braucht die Nextcloud-App **Notes**.

- „Schreib dir auf, dass das Gäste-WLAN-Passwort am Kühlschrank hängt“ → neue Notiz in der
  Kategorie `NEXTCLOUD_NOTIZEN_KATEGORIE` (Standard `Missa`)
- „Ergänz die Homelab-Notiz: ai01 hat jetzt 64 GB RAM“ → Text mit Datum unten angehängt
- „Was hab ich mir zu rp01 notiert?“ → Suche in Titel und Inhalt, dann `notiz_lesen`

Missa löscht keine Notizen.

## Dateien (WebDAV)

| Du sagst | Missa |
|----------|-------|
| „Wo ist die Doku zu rp01?“ | sucht Dateien und Ordner mit „rp01“ im Namen und nennt den Pfad (Textdateien zuerst) |
| „Such im Ordner Doku nach caddy“ | Suche nur in diesem Ordner |
| „Was liegt im Ordner Doku/Homelab?“ | Inhalt des Ordners |
| „Lies mir Doku/Homelab/rp01.md vor“ | liest Text- und Markdown-Dateien (bis 1 MB, lange Texte gekürzt) |
| „Leg die Zusammenfassung in deinem Ordner ab“ | schreibt `Missa/zusammenfassung.md` |

Gesucht wird nach **Namen** (die WebDAV-Suche von Nextcloud). Fragen *zum Inhalt* deiner
Dokus beantwortet Missa über den lokalen Doku-Index (siehe [doku-wissen.md](doku-wissen.md)).

### Schreiben nur in `Missa/`

- Jeder Pfad wird **relativ zu Missas Ordner** gebaut (`NEXTCLOUD_DATEIEN_ORDNER`, Standard
  `Missa`) und danach geprüft. `../`, absolute Pfade wie `/Dokumente/x.md` oder
  `Missa/../x.md` lehnt der Code ab – außerhalb von `Missa/` kann sie nichts schreiben.
- Bestehende Dateien werden **nie überschrieben**, nur ergänzt (`anhaengen`).
- Ein Werkzeug zum **Löschen gibt es nicht**, weder in `Missa/` noch sonst wo.
- Jeder Schreibvorgang steht im Aktionsprotokoll (siehe [rechte.md](rechte.md)).
