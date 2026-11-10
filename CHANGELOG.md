# Changelog

Alle nennenswerten Änderungen an diesem Projekt stehen in dieser Datei.

Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionsnummern folgen [Semantic Versioning](https://semver.org/lang/de/).

## Unveröffentlicht

## 0.3.0 – 2026-11-10

### Hinzugefügt

- Missa erinnert dich auf die Minute genau, auch wiederkehrend und nach einem Neustart:
  „Erinnere mich um 6:30 Uhr an Training“, „in 20 Minuten“, „am Freitag um 14 Uhr“ oder
  „jeden Montag um 7“ – sie bestätigt Datum und Uhrzeit, fragt bei mehrdeutigen Angaben
  nach, holt verpasste Erinnerungen nach einem Ausfall mit Hinweis nach, und du kannst
  Erinnerungen im Chat auflisten, ändern, löschen und schlummern („in 10 Minuten nochmal“);
  Zustellung über Nextcloud Talk, Übersicht unter `GET /api/erinnerungen` (#26)
- Missa verwaltet deinen Kalender komplett selbst, inklusive Farben, eigener Kalender und
  Rückgängig-Funktion: Termine ändern und verschieben („Verschieb das Meeting morgen auf
  15 Uhr und mach es rot“), Kategorien mit eigenem Farbsystem, Erinnerungen, Serien samt
  einzelner Ausnahmen, ganztägige Termine, Kalender anlegen, umbenennen und einfärben; jede
  Änderung wird mit vorher/nachher protokolliert, lässt sich mit „mach das rückgängig“
  zurücknehmen und steht unter `GET /api/kalender/protokoll`; Einladungen an andere
  Personen verschickt sie nur nach deinem Ja (#27)

- Missa schreibt E-Mails in deinem Stil vor und schickt sie nur ab, wenn du zustimmst:
  „Schreib dem Chef, dass ich morgen später komme“ ergibt einen Entwurf im Entwürfe-Ordner,
  den Missa dir mit Empfänger, Betreff und Text zeigt und mit „Soll ich das abschicken?“
  abschliesst; erst dein „Ja, abschicken“ verschickt ihn, jede Änderung braucht ein neues Ja.
  Dein Schreibstil wird aus gesendeten Mails gelernt, liegt als bearbeitbare Datei vor und
  lässt sich im Chat anpassen („schreib förmlicher an Lehrer“). Einrichtung über die neuen
  `MAIL_*`-Einstellungen (#28)

- Missa erinnert an Geburtstage und daran, dich bei wichtigen Menschen zu melden:
  Geburtstage aus Nextcloud-Kontakte bekommen eine Erinnerung einige Tage vorher und am Tag
  selbst (29. Februar in Nicht-Schaltjahren am 28.); „Oma mindestens alle 3 Wochen“ markiert
  wichtige Kontakte, der letzte Kontakt kommt aus deinen Mails oder aus dem Chat („hab gerade
  mit Oma telefoniert“), und ist der Abstand überschritten, erinnert Missa freundlich und
  schlägt einen freien Zeitpunkt im Kalender vor (#29)

- Missa achtet von sich aus auf Pausen und Puffer in deinem Kalender: Jeden Abend prüft sie
  den nächsten Tag auf Termine direkt hintereinander, fehlende Mittagspause, zu volle Tage
  und fehlende Wegzeit, trägt Puffer- und Pausenblöcke im Kalender „Pausen“ selbst ein
  (rückgängig machbar) und sagt dir danach Bescheid; zerstört ein neuer Termin einen Puffer,
  weist sie sofort darauf hin und schlägt eine bessere Uhrzeit vor. Alle Grenzwerte sind
  über `PAUSEN_*` einstellbar (#30)

### Geändert

- Termine löscht Missa jetzt ohne Rückfrage – dafür lässt sich jede Löschung rückgängig
  machen. Nur wenn andere Personen eine Absage-Mail bekämen, fragt sie weiterhin vorher (#27)

## 0.2.1 – 2026-11-10

### Behoben

- Update-Script: Wurde der neue Code von Hand ausgecheckt und gab es zur installierten
  Version keinen Tag, meldete das Script fälschlich „schon aktuell“ und installierte
  nichts. Jetzt erkennt es die Ersteinrichtung auch in diesem Fall (#16)

## 0.2.0 – 2026-11-10

### Hinzugefügt

- Update auf ai01 mit einem Befehl: `sudo ./scripts/update.sh` holt die neueste Version,
  zeigt, was neu ist, ergänzt neue Einstellungen (ohne bestehende zu ändern), listet auf,
  was du noch ausfüllen musst und was optional ist, und springt bei Fehlern automatisch auf
  die vorherige Version zurück; `--dry-run` zeigt nur an (#16)
- Missa ist per Nextcloud Talk erreichbar, nur für deinen Account und den Admin-Account;
  alle anderen werden still ignoriert. Missa zeigt mit 👀, dass sie arbeitet, antwortet
  als Zitat-Antwort mit ihrem gewohnten Charakter, Gedächtnis und Werkzeugen und teilt
  sehr lange Antworten auf (Einrichtung siehe docs/nextcloud-talk.md) (#17)
- Missa kann dir von sich aus in Nextcloud Talk schreiben, z. B. bei Erinnerungen –
  nur in die eingestellte Unterhaltung und höchstens 10 Nachrichten pro Minute (#18)

## 0.1.0 – 2026-11-09

Erste Version von Missa-Core.

### Hinzugefügt

- Missa antwortet über eine eigene API mit ihrem Charakter: OpenAI-kompatible
  Endpunkte `/v1/chat/completions` (mit und ohne Streaming) und `/v1/models`,
  geschützt per Bearer-Token; der Charakter wird bei jeder Anfrage frisch aus
  der Charakterdatei gelesen, Gespräche mit Sitzungs-ID werden gemerkt, und
  `/health` zeigt, ob llama-server erreichbar ist (#2)
- Missa liest und schreibt Termine im Nextcloud-Kalender: Termine eines Tages
  oder Zeitraums anzeigen (auch wiederkehrende), neue Termine eintragen und
  Termine löschen – gelöscht wird erst, nachdem Louis ausdrücklich Ja gesagt hat (#3)
- Missa verwaltet deine Aufgaben in Nextcloud: offene (oder alle) Aufgaben
  anzeigen, neue Aufgaben mit Fälligkeit, Priorität und Notiz anlegen, Aufgaben
  abhaken und – nach deinem Ja – löschen; alles erscheint direkt in der
  Nextcloud-App „Aufgaben“ (#4)
- Missa merkt sich Wichtiges über dich: Auf „Merk dir …“ speichert sie den Fakt
  dauerhaft, findet passende Fakten bei jeder Frage von selbst wieder (auch in
  neuen Gesprächen), vergisst etwas erst nach deinem Ja, und unter
  `GET /api/gedaechtnis` kannst du alles Gespeicherte prüfen (#5)
- Installationsanleitung, API-Doku und systemd-Dienst: Missa-Core läuft auf ai01
  als Dienst unter dem Benutzer `missa`, startet nach einem Neustart von selbst,
  und `docs/api.md` erklärt, wie sich eine App verbindet (#6)
