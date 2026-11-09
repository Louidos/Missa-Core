# Missa-Core

Missa ist eine persönliche KI-Assistentin. Missa-Core ist der zentrale Dienst:
Er spricht als Missa, führt Werkzeuge aus (z. B. Kalender, Aufgaben, Gedächtnis)
und leitet Anfragen an ein lokal laufendes Sprachmodell (llama-server) weiter.
Jede App verbindet sich nur mit der API von Missa-Core.

## Projektstand

- Version: noch keine veröffentlicht
- Die Grundstruktur steht; die Funktionen werden schrittweise ergänzt
  (siehe [CHANGELOG.md](CHANGELOG.md)).

## Voraussetzungen

- Linux
- Python 3.13

## Installation (Entwicklung)

```bash
python3.13 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Start

Folgt mit dem Missa-Core-Dienst.

## Prüfen

```bash
scripts/pruefen.sh
```

Das Skript führt `ruff check`, `ruff format --check` und `pytest` aus – genau wie die CI.

## Aufbau

| Ordner     | Inhalt                                                    |
|------------|-----------------------------------------------------------|
| `src/missa/` | Programmcode (Python-Paket)                             |
| `tests/`   | Tests (pytest)                                            |
| `scripts/` | Hilfsskripte                                              |
| `docs/`    | Dokumentation                                             |
| `config/`  | Beispiel-Konfiguration (keine echten Geheimnisse!)        |
| `deploy/`  | systemd-Unit                                              |

## Lizenz

MIT, siehe [LICENSE](LICENSE).
