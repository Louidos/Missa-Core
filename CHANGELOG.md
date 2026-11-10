# Changelog

Alle nennenswerten Änderungen an diesem Projekt stehen in dieser Datei.

Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionsnummern folgen [Semantic Versioning](https://semver.org/lang/de/).

## Unveröffentlicht

### Hinzugefügt

- Update auf ai01 mit einem Befehl: `sudo ./scripts/update.sh` holt die neueste Version,
  zeigt, was neu ist, ergänzt neue Einstellungen (ohne bestehende zu ändern) und springt
  bei Fehlern automatisch auf die vorherige Version zurück; `--dry-run` zeigt nur an (#16)

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
