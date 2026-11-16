# Entwicklung

Wie man an Missa-Core arbeitet: Umgebung, Prüfungen, Tests, typische Erweiterungen und der
Weg vom Branch bis zur Version. Wie das Ganze aufgebaut ist: [architektur.md](architektur.md).

## Umgebung

Python 3.13 unter Linux.

```bash
python3.13 -m venv .venv
```

```bash
. .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

Für das Einrichten neuer VMs per Playbook zusätzlich `pip install -e ".[vms]"` (ansible-core).

Lokal starten, ohne ai01 – ohne llama-server antwortet Missa mit einem Fehler, `/health` und
die übrigen Endpunkte funktionieren aber:

```bash
MISSA_API_KEY=lokal-geheim-123 MISSA_CHARAKTER_DATEI=config/charakter.example.md MISSA_DATEN=./data missa-core
```

Mit einem lokal laufenden llama-server zusätzlich `LLAMA_URL=http://127.0.0.1:8080` setzen.
Alle übrigen Einstellungen: [installation.md](installation.md).

## Prüfen

```bash
scripts/pruefen.sh
```

Führt dasselbe aus wie die CI (`.github/workflows/ci.yml`): `ruff check`, `ruff format --check`,
`shellcheck`, `pip-audit`, `gitleaks` und `pytest`. Einzelne Tests:

```bash
pytest tests/test_kalender.py -k verschieben
```

Regeln, auf die die Prüfungen achten:

- Zeilenlänge 100, Ziel Python 3.13, ruff-Regeln `E F W I UP B SIM RUF S`.
- `S` sind Sicherheitsregeln. Ein Fehlalarm wird nicht global abgeschaltet, sondern in der
  Zeile mit Begründung markiert: `# noqa: S310 – Adresse kommt aus der Konfiguration`.
- Gedankenstriche und deutsche Anführungszeichen sind erlaubt (RUF001–003 aus).
- Keine echten Geheimnisse ins Repo. Erfundene Platzhalter, die gitleaks trotzdem meldet,
  kommen mit Begründung in `.gitleaks.toml`.

## Tests

Alle Tests laufen ohne Netz und ohne echte Dienste:

| Hilfe | Datei | Ersetzt |
|-------|-------|---------|
| `FakeLlama`, Fixtures `einstellungen`, `client`, `werkzeuge` | `tests/conftest.py` | llama-server (Antworten der Reihe nach, auch Streaming und Werkzeug-Aufrufe) |
| `werkzeug_aufruf()`, `text()` | `tests/conftest.py` | Bausteine für Antworten des Modells |
| CalDAV-Fake | `tests/caldav_fake.py` | Nextcloud-Kalender und -Aufgaben |
| Nextcloud-HTTP-Fake | `tests/nextcloud_http_fake.py` | Notes-API und WebDAV (merkt sich jede Anfrage) |
| SSH-Fake | `tests/ssh_fake.py` | die Hosts hinter SSH |
| Missa-Control-Fake | `tests/missa_control_fake.py` | MCP-Gateway des Panels |
| Charakter-Fragen | `tests/charakter/fragen.yaml` | Fragenset für den Charaktertest gegen das echte Modell ([charaktertest.md](charaktertest.md)) |

Feste Regeln, die eigene Tests prüfen:

- **Jedes Werkzeug hat einen Test**, der es beim Namen nennt (`tests/test_werkzeug_abdeckung.py`).
- **Jedes Werkzeug steht in [werkzeuge.md](werkzeuge.md)** (`tests/test_werkzeuge_doku.py`).
- Die Einrichtungs- und Notfall-Scripts sowie `update.sh` haben eigene Tests.

## Ein neues Werkzeug

Ein Werkzeug ist ein `Werkzeug(...)` aus `missa.core.werkzeuge`. Beispiel nach dem Muster von
`tools/talk.py`:

```python
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler


def wetter_werkzeuge(dienst: WetterDienst) -> list[Werkzeug]:
    return [
        Werkzeug(
            "wetter_anzeigen",
            "Zeigt das Wetter für einen Ort (heute und die nächsten Tage).",
            {
                "type": "object",
                "properties": {"ort": {"type": "string", "description": "z. B. Bern"}},
                "required": ["ort"],
            },
            dienst.anzeigen,  # darf sync oder async sein
            stufe=Stufe.LESEN,
        )
    ]
```

Schritt für Schritt:

1. **Modul** unter `src/missa/tools/` anlegen. Zugriffe auf fremde Dienste (HTTP, SSH …)
   gehören nach `src/missa/integrationen/`, das Werkzeug ruft sie nur auf.
2. **Stufe wählen** – ohne Stufe wird das Werkzeug nicht registriert:
   `LESEN` (ändert nichts), `SCHREIBEN` (harmlos oder rückgängig machbar),
   `ZERSTOERERISCH` (löscht, sendet, startet neu). Für zerstörerische Werkzeuge optional eine
   `vorschau`-Funktion, die für die Rückfrage beschreibt, was passieren würde.
3. **Fehler** als `WerkzeugFehler("…")` werfen – der Text geht verständlich ans Modell.
   Andere Ausnahmen werden abgefangen und protokolliert.
4. **Fremder Text?** Liefert das Werkzeug Inhalte, die nicht von Louis stammen (Mails, Dateien,
   Logs, Webseiten), den Namen in `FREMDE_QUELLEN` in `core/werkzeuge.py` eintragen. Dann wird
   das Ergebnis markiert, und Schreibendes danach braucht ein Ja.
5. **Registrieren** in `werkzeuge_laden()` in `src/missa/tools/__init__.py` – nur, wenn der
   Dienst konfiguriert ist (`if dienst is not None: …`). Den Dienst selbst baut
   `create_app()` in `core/app.py`.
6. **Regelmässige Prüfungen** (z. B. alle 5 Minuten) über `zeitplaner.regelmaessig(...)` oder
   `zeitplaner.taeglich(...)` in `create_app()` anmelden; Meldungen an Louis über
   `alarme.melden(...)` bzw. die Zustellung, nie direkt.
7. **Kontext** für den Systemprompt (z. B. aktive Alarme): eine Funktion `kontext(frage) -> str | None`
   als Kontextquelle beim `ChatDienst` eintragen. Kurz halten – sie läuft bei jeder Anfrage.
8. **Tests** schreiben, die das Werkzeug beim Namen nennen.
9. **Doku**: Zeile in [werkzeuge.md](werkzeuge.md), Beispiel-Fragen in der Tabelle in
   [api.md](api.md#was-missa-kann-werkzeuge), Anleitung für den Bereich unter `docs/` und
   Eintrag in [docs/README.md](README.md).

## Eine neue Einstellung

1. Feld in `Einstellungen` (`core/config.py`) mit Standardwert ergänzen und in `aus_umgebung()`
   einlesen. Ist es ein Geheimnis, den Wert in der Eigenschaft `geheimnisse` ergänzen (wird
   dann im Aktionsprotokoll und im Dienst-Log geschwärzt) – und die Variable so benennen, dass
   `env_ohne_geheimnisse()` in `core/sicherung.py` sie im Backup leert (Name enthält
   `PASSWORD`, `PASSWORT`, `SECRET`, `TOKEN`, `API_KEY` oder endet auf `_KEY`).
2. Bei Pflichtwerten in `pruefen()` eine verständliche Meldung ergänzen.
3. In `config/missa.env.example` mit Kommentar eintragen und in [installation.md](installation.md)
   erklären.
4. Kann Louis den Wert im Gespräch ändern, über `Vorgaben` (`core/vorgaben.py`) speichern: Der
   Wert aus dem Gespräch gilt dann vor dem aus der Konfiguration.

## Eine neue Tabelle

Es gibt kein Migrations-Werkzeug. Jedes Modul legt seine Tabellen mit
`CREATE TABLE IF NOT EXISTS` an und ergänzt neue Spalten beim Start (Muster: `migrieren()` in
`core/zeitplaner.py`). Alte Daten müssen dabei erhalten bleiben – ein Update auf ai01 darf nie
Gedächtnis oder Erinnerungen verlieren. Die neue Tabelle in [architektur.md](architektur.md#tabellen-in-missadb)
nachtragen. Sie landet automatisch im Backup, weil die ganze `missa.db` gesichert wird.

## Ein neuer Kanal

- **Nachrichten von Missa aus** (Erinnerungen, Alarme): eine Klasse mit `name` und
  `async senden(text)`, die bei Misserfolg `ZustellFehler` wirft, mit
  `zustellung.hinzufuegen(...)` anmelden. Vorbild: `TalkZustellkanal` in `tools/talk.py`.
- **Nachrichten an Missa** (wie Talk): eigenes Modul unter `src/missa/channels/`, das die
  Anfrage selbst absichert (Signatur, erlaubte Absender) und dann den gemeinsamen `ChatDienst`
  aufruft. Der Pfad muss in der Schutz-Middleware als offen eingetragen werden, sonst verlangt
  sie den API-Key.

## Arbeitsweise im Repo

- **Branch pro Änderung**, nie direkt auf `main` (Name z. B. `feat/…`, `fix/…`, `docs/…`).
- **Commits** nach [Conventional Commits](https://www.conventionalcommits.org/de/) auf Deutsch:
  `feat: Erinnerungen schlummern`, `fix: …`, `docs: …`, `refactor: …`, `test: …`, `chore: …`.
- **Pull Request** mit der Vorlage (`.github/pull_request_template.md`): was sich ändert, wie
  getestet, `Closes #…`. Issues über die Vorlagen „Fehler“ und „Wunsch“.
- **CHANGELOG.md** unter `## Unveröffentlicht` ergänzen, sobald sich für Louis etwas spürbar
  ändert – in verständlichen Sätzen, mit Issue-Nummer.
- **Version**: Beim Release den Abschnitt `Unveröffentlicht` in `## X.Y.Z – JJJJ-MM-TT`
  umbenennen, `version` in `pyproject.toml` und den Rückfallwert in `src/missa/__init__.py`
  anheben, README-Projektstand anpassen, dann den Tag `vX.Y.Z` setzen. `scripts/update.sh`
  auf ai01 holt immer den neuesten Release-Tag ([update.md](update.md)).
- Abhängigkeiten und GitHub-Actions hält Dependabot aktuell (`.github/dependabot.yml`); Actions
  sind auf feste Commits festgelegt.
