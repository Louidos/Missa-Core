# Missa-Core

Missa ist eine persönliche KI-Assistentin. Missa-Core ist der zentrale Dienst:
Er spricht als Missa, führt Werkzeuge aus (z. B. Kalender, Aufgaben, Gedächtnis)
und leitet Anfragen an ein lokal laufendes Sprachmodell (llama-server) weiter.
Jede App verbindet sich nur mit der API von Missa-Core.

## Projektstand

- Version: 0.1.0 (2026-11-09)
- Fertig: OpenAI-kompatible API (`/v1/chat/completions`, `/v1/models`, `/health`)
  mit Charakter, Werkzeug-Schleife und Gesprächsverlauf
- Fertig: Nextcloud-Kalender (Termine anzeigen, eintragen, nach Bestätigung löschen)
- Fertig: Nextcloud-Aufgaben (anzeigen, erstellen, abhaken, nach Bestätigung löschen)
- Fertig: Gedächtnis (merken, erinnern, vergessen; passende Fakten automatisch im Kontext;
  Übersicht unter `GET /api/gedaechtnis`)
- Fertig: systemd-Dienst, Installationsanleitung und API-Doku
- Später als eigene Aufträge: Telefon, Push, Smart Home
- Änderungen im Detail: [CHANGELOG.md](CHANGELOG.md)

## Dokumentation

- **[docs/installation.md](docs/installation.md)** – Installation auf ai01 als systemd-Dienst
  unter dem Benutzer `missa`, alle Variablen erklärt
- **[docs/api.md](docs/api.md)** – alle Endpunkte mit curl-Beispielen und wie sich eine App
  verbindet (Basis-URL, Bearer-Token, Modell `missa`, Header `X-Missa-Session`)

## Installation

Für den Betrieb auf ai01: siehe [docs/installation.md](docs/installation.md). Kurz:
Code nach `/opt/missa/app`, Python-Umgebung `/opt/missa/venv`, Konfiguration in
`/etc/missa/missa.env`, Charakter in `/etc/missa/charakter.md`, Dienst aus
[deploy/missa-core.service](deploy/missa-core.service).

Für die Entwicklung (Python 3.13, Linux):

```bash
python3.13 -m venv .venv
```

```bash
. .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

## Start

Im Betrieb startet systemd den Dienst:

```bash
sudo systemctl enable --now missa-core
```

Von Hand liest `missa-core` seine Einstellungen aus Umgebungsvariablen und zusätzlich aus
`/etc/missa/missa.env` (anderer Ort: `MISSA_ENV_DATEI`; schon gesetzte Variablen haben
Vorrang). Vorlage: [config/missa.env.example](config/missa.env.example),
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
