# Missa-Core

Missa ist eine persönliche KI-Assistentin. Missa-Core ist der zentrale Dienst:
Er spricht als Missa, führt Werkzeuge aus (z. B. Kalender, Aufgaben, Gedächtnis)
und leitet Anfragen an ein lokal laufendes Sprachmodell (llama-server) weiter.
Jede App verbindet sich nur mit der API von Missa-Core.

## Projektstand

- Version: noch keine veröffentlicht
- Fertig: OpenAI-kompatible API (`/v1/chat/completions`, `/v1/models`, `/health`)
  mit Charakter, Werkzeug-Schleife und Gesprächsverlauf
- Weitere Funktionen folgen schrittweise (siehe [CHANGELOG.md](CHANGELOG.md)).

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

Missa-Core liest seine Einstellungen aus Umgebungsvariablen. Beim Start von Hand
wird zusätzlich `/etc/missa/missa.env` gelesen (anderer Ort: `MISSA_ENV_DATEI`);
schon gesetzte Variablen haben Vorrang. Vorlage: [config/missa.env.example](config/missa.env.example),
Beispiel-Charakter: [config/charakter.example.md](config/charakter.example.md).

```bash
MISSA_API_KEY=geheim MISSA_CHARAKTER_DATEI=config/charakter.example.md MISSA_DATEN=./data missa-core
```

Ohne `MISSA_API_KEY` startet der Dienst nicht. Kurztest:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer geheim" -H "Content-Type: application/json" \
  -d '{"model": "missa", "messages": [{"role": "user", "content": "Hallo Missa!"}]}'
```

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
