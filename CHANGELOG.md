# Changelog

Alle nennenswerten Änderungen an diesem Projekt stehen in dieser Datei.

Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionsnummern folgen [Semantic Versioning](https://semver.org/lang/de/).

## Unveröffentlicht

### Hinzugefügt

- Missa antwortet über eine eigene API mit ihrem Charakter: OpenAI-kompatible
  Endpunkte `/v1/chat/completions` (mit und ohne Streaming) und `/v1/models`,
  geschützt per Bearer-Token; der Charakter wird bei jeder Anfrage frisch aus
  der Charakterdatei gelesen, Gespräche mit Sitzungs-ID werden gemerkt, und
  `/health` zeigt, ob llama-server erreichbar ist (#2)
