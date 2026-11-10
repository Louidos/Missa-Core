# Missa-Core aktualisieren

Auf ai01 reicht ein Befehl:

```bash
sudo /opt/missa/app/scripts/update.sh
```

Das Script holt die neueste veröffentlichte Version (den neuesten Release-Tag `vX.Y.Z`),
zeigt, was neu ist, installiert neue Abhängigkeiten, ergänzt neue Einstellungen und
startet Missa neu. Geht dabei etwas schief, wechselt es automatisch zurück auf die
Version, die vorher lief.

## Einmalige Ersteinrichtung

Das Update-Script gibt es ab Version 0.2.0. Läuft auf ai01 noch eine ältere Version,
fehlt es dort. Dann **einmal** von Hand den neuen Stand holen:

```bash
cd /opt/missa/app
```

```bash
sudo git fetch --tags
```

```bash
sudo git checkout "$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
```

Ist auf GitHub noch kein Release-Tag zu sehen (`git tag` zeigt nichts), stattdessen den
neuesten Stand von main holen:

```bash
sudo git pull
```

Danach das Script starten (bei der Variante mit `git pull` mit `--main`):

```bash
sudo ./scripts/update.sh
```

Das Script merkt, dass der Code schon neu, aber noch nicht installiert ist, und erledigt
den Rest: Abhängigkeiten installieren, Einstellungen ergänzen, Neustart, Prüfung. Ab jetzt
reicht immer nur noch `sudo ./scripts/update.sh`.

### Optional: Kurzbefehl `missa-update`

```bash
echo "alias missa-update='sudo /opt/missa/app/scripts/update.sh'" >> ~/.bashrc
```

```bash
source ~/.bashrc
```

Danach geht das Update von überall mit `missa-update` (auch `missa-update --dry-run`).
Bitte einen Alias verwenden, keinen Symlink: Das Script findet sein Repo über den
eigenen Speicherort.

## Optionen

| Aufruf | Wirkung |
|--------|---------|
| `sudo ./scripts/update.sh` | auf den neuesten Release-Tag wechseln (stabil, empfohlen) |
| `sudo ./scripts/update.sh --main` | auf den neuesten Stand von main wechseln (enthält auch noch nicht veröffentlichte Änderungen) |
| `sudo ./scripts/update.sh --version v0.1.0` | auf genau diese Version wechseln, auch zurück auf eine ältere |
| `./scripts/update.sh --dry-run` | nur anzeigen, was passieren würde – ändert nichts, braucht kein `sudo` |
| `sudo ./scripts/update.sh --erzwingen` | neu installieren und neu starten, auch wenn schon alles aktuell ist (z. B. zum Reparieren) |
| `./scripts/update.sh --help` | Hilfe |

## Was das Script macht

1. **Prüfen:** Liegt das Script im geklonten Repo? Gibt es lokale Änderungen an
   versionierten Dateien? Dann bricht es ab und zeigt sie an (sichern z. B. mit
   `sudo git -C /opt/missa/app stash`). Ist GitHub erreichbar?
2. **Startart erkennen:** systemd-Dienst `missa-core` mit Python-Umgebung (so ist ai01
   eingerichtet). Die Python-Umgebung und die Einstellungsdatei liest es aus der Unit
   (`ExecStart=`, `EnvironmentFile=`), sonst gelten `/opt/missa/venv` und
   `/etc/missa/missa.env`. Gäbe es stattdessen eine `compose.yaml`, würde es
   `docker compose build` / `up -d` benutzen.
3. **Merken, was läuft:** Der installierte Stand wird gemerkt (`logs/.installiert`),
   damit ein Rücksprung möglich ist.
4. **Ziel bestimmen:** `git fetch --tags`, dann neuester Release-Tag (bzw. main oder die
   angegebene Version). Ist schon alles aktuell, meldet es das und beendet sich **ohne
   Neustart**.
5. **Neuerungen zeigen:** die Abschnitte aus `CHANGELOG.md`, die seit der laufenden Version
   dazugekommen sind.
6. **Code wechseln und installieren:** `git checkout` auf das Ziel, dann
   `pip install --upgrade` in die Python-Umgebung. Datenbank-Tabellen legt Missa-Core beim
   Start selbst an; eigene Migrationen gibt es derzeit nicht. Kommt einmal ein Script
   `scripts/migrationen.sh` dazu, ruft das Update es automatisch auf.
7. **Neue Einstellungen ergänzen** (siehe unten).
8. **Neustart und Prüfung:** `systemctl restart missa-core`, dann bis zu 60 Sekunden lang
   `GET /health`. Erfolgreich ist das Update, wenn `/health` die neue Versionsnummer meldet.
   Antwortet nur llama-server nicht, gibt es eine Warnung, aber keinen Rücksprung – das
   Problem hätte die alte Version genauso.

## Neue Einstellungen

Die Einstellungsdatei (`/etc/missa/missa.env`) wird **nie überschrieben**. Das Script
vergleicht sie mit der Vorlage `config/missa.env.example` der neuen Version. Fehlt ein
Schlüssel, hängt es ihn unten an – mit dem Kommentar aus der Vorlage, dem Beispielwert
(oft leer) und dem Hinweis `# neu in vX.Y.Z`. Vorher legt es eine Sicherungskopie an
(`missa.env.vor-update-JJJJMMTT-HHMMSS`, gleiche Rechte wie das Original).

- Schlüssel, die in der Datei schon stehen – auch auskommentiert (`#NAME=…`) – fasst es nicht an.
- Am Ende listet es alle ergänzten Schlüssel auf und hebt die leeren hervor:
  **„Bitte noch ausfüllen“**. Leere Werte, die in der Vorlage mit `# Optional:` markiert sind,
  stehen getrennt unter „Optional (darf leer bleiben)“. Nach dem Ausfüllen:
  `sudo systemctl restart missa-core`.
- Neue Funktionen sind so gebaut, dass sie mit leeren Werten ausgeschaltet bleiben und den
  Start nicht verhindern (z. B. Nextcloud Talk mit `NC_TALK_ENABLED=false`).

## Wenn etwas schiefgeht

Schlägt die Installation, der Neustart oder die Gesundheitsprüfung fehl (oder bricht man
mit Strg+C ab), springt das Script automatisch zurück:

1. `git checkout` auf den vorher installierten Stand,
2. diesen wieder installieren,
3. neu starten und `/health` prüfen.

Es meldet klar, was schiefging und ob die alte Version wieder läuft. Die schon ergänzten
Einstellungen bleiben stehen; die alte Version ignoriert unbekannte Schlüssel. Startet auch
die alte Version nicht, steht das deutlich da – dann hilft
`journalctl -u missa-core -n 50`.

## Log

Jeder Lauf schreibt nach `logs/update.log` im Repo-Ordner (nicht im Git). Dort stehen auch
die vollständigen Ausgaben von `git` und `pip`. Der Probelauf (`--dry-run`) schreibt nichts.

## Wenn sich die systemd-Unit ändert

Das Script übernimmt `deploy/missa-core.service` nicht selbst (die installierte Unit könnte
angepasst sein). Weicht sie ab, gibt es eine Warnung. Übernehmen:

```bash
sudo install -m 644 /opt/missa/app/deploy/missa-core.service /etc/systemd/system/missa-core.service
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart missa-core
```

## Testen

**Automatisch:** `tests/test_update_script.py` startet das echte `update.sh` in einer
Ersatzwelt: ein lokales Git-Repo spielt GitHub (Tags v0.1.0, v0.2.0 und ein neuerer
main-Stand), `systemctl`, `pip` und `curl` sind kleine Ersatz-Scripts. Geprüft werden
Update, „schon aktuell“, `--main`, `--version`, Probelauf, Ergänzen der Einstellungen,
lokale Änderungen, GitHub nicht erreichbar, Ersteinrichtung und der Rücksprung, wenn die
Installation, der Start oder `/health` fehlschlägt. `shellcheck` prüft das Script in der CI.

```bash
pytest tests/test_update_script.py
```

**Auf ai01 von Hand:**

1. Probelauf: `./scripts/update.sh --dry-run` zeigt Ziel, Neuerungen und neue Einstellungen.
2. Rücksprung proben, ohne etwas kaputtzumachen: `--erzwingen` installiert den laufenden
   Stand neu, und der Testschalter lässt die Prüfung danach absichtlich scheitern:

   ```bash
   sudo MISSA_UPDATE_TEST_FEHLER=1 ./scripts/update.sh --erzwingen
   ```

   Erwartet: „Testmodus … absichtlich fehlgeschlagen“, dann „Die alte Version … läuft wieder“,
   und `curl http://127.0.0.1:8000/health` meldet weiter dieselbe Version.
