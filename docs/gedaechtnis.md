# Langzeitgedächtnis

Alles liegt lokal in `MISSA_DATEN/missa.db` und wird mit der SQLite-Volltextsuche (FTS5)
durchsucht – auf der CPU, ohne externe Dienste und ohne dass Daten das Haus verlassen.

## Merken

| Du sagst | Missa |
|----------|-------|
| „Merk dir, dass ich Kaffee ohne Zucker trinke“ | speichert den Fakt mit Datum (`merken`) |
| „Ich hab mich für Debian entschieden“ | merkt sich die Entscheidung (Kategorie `entscheidungen`) |
| „Was weißt du über meine Schwester?“ | sucht im Gedächtnis (`erinnern`) |
| „Was hab ich dir letzte Woche über den Server erzählt?“ | durchsucht frühere Gespräche (`frueher_gesagt`) |

- Kategorien: `termine`, `vorlieben`, `projekte`, `entscheidungen`, `korrekturen`, `sonstiges`.
- Erzählst du etwas, das nach Vorliebe, Entscheidung oder Projekt klingt („ich mag …“, „ab
  jetzt …“, „mein Projekt …“), bekommt Missa einen Hinweis, es sich zu merken.
- Bei jeder Nachricht kommen die passendsten Fakten **und** passende frühere Aussagen von dir
  (letzte 90 Tage, mit Datum) in ihren Kontext. So weiß sie auch nach einem Neustart oder in
  einem anderen Kanal (Talk, App) noch, was du vorgestern gesagt hast.
- **Nicht doppelt:** Ist ein neuer Fakt fast gleich wie ein alter („Kaffee *ohne* Zucker“ statt
  „Kaffee *mit* Zucker“), ersetzt er den alten.

## Aus Korrekturen lernen

Sagst du „Das war falsch, …“ oder „Nein, richtig ist …“, erkennt der Code das und Missa
speichert die Korrektur (`korrigieren`):

1. Der falsche alte Eintrag wird **ersetzt** (nicht doppelt geführt).
2. Die Korrektur selbst bleibt als Eintrag der Kategorie `korrekturen` – eine spätere
   Korrektur zum selben Punkt ersetzt die frühere.
3. Bei ähnlichen Fragen steht sie ganz oben im Kontext: „Korrekturen von Louis (unbedingt
   beachten)“. So antwortet Missa beim nächsten Mal richtig.

## Vergessen

- „Vergiss das mit dem Kaffee“ → Missa zeigt, was alles gelöscht würde (Fakten **und**
  Nachrichten aus früheren Gesprächen, die „Kaffee“ enthalten), und löscht erst nach deinem Ja.
- „Vergiss Fakt #12“ geht auch.
- Gelöscht wird **wirklich**: aus der Tabelle, aus dem Suchindex, mit `secure_delete`. Danach
  ist es nicht mehr abrufbar. Ältere Backups enthalten es noch – die verschwinden nach
  `MISSA_BACKUP_BEHALTEN` Tagen von selbst (siehe [sicherung.md](sicherung.md)).

## Für Apps

`GET /api/gedaechtnis` listet alle Fakten (Feld `geaendert` neu dazu, siehe [api.md](api.md)).
Beim ersten Start nach dem Update wird die Tabelle automatisch umgebaut – Daten bleiben.
