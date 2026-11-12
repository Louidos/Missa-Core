# Git-Server, GitHub und Claude-Code-Prompts

Missa behält den Überblick über deine eigenen Repos. Auf dem Git-Server (Gitea oder
Forgejo, z. B. git.louis-degiorgi.ch) legt sie Issues an, fasst offene Pull Requests zusammen
und meldet alte Branches. Auf GitHub (Konto Louidos) schaut sie nur nach. Außerdem macht sie
aus groben Ideen fertige Prompts für Claude Code. Jeder Teil ist aus, solange die Einstellungen
dafür fehlen.

## Git-Server (Gitea/Forgejo)

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Leg im Repo Missa-Core ein Issue an: Backup prüfen“ | legt das Issue an und nennt Nummer und Link | `git_issue_anlegen` (schreiben) |
| „Was ist auf dem Git-Server offen?“ | Server (Gitea/Forgejo mit Version), Repos, offene Pull Requests mit Branch, Autor und letzter Änderung | `git_ueberblick` (lesen) |
| „Welche Branches sind liegen geblieben?“ | Branches ohne Commit seit 14 Tagen | `git_alte_branches` (lesen) |

- **Erkennen:** Forgejo beantwortet `/api/forgejo/v1/version`, Gitea nur `/api/v1/version`.
  Daran erkennt Missa, was läuft.
- **Repo-Namen:** `Missa-Core` reicht, wenn es den Namen nur einmal gibt (Groß- und
  Kleinschreibung egal). Sonst `louis/Missa-Core`, oder du setzt `GIT_SERVER_BESITZER` als
  Standard. Archivierte Repos lässt Missa aus.
- **Alte Branches:** einmal pro Woche als `info` (`GIT_ALTE_BRANCHES_TAGE`, Standard 14).
  Der Standard-Branch zählt nicht.

**Token:** Leg auf dem Git-Server für den Benutzer, dessen Repos Missa sehen soll (oder einen
eigenen Benutzer `missa` mit Zugriff auf die Repos), unter *Einstellungen → Anwendungen* ein
Token an. Es braucht nur:

- `read:user`
- `read:repository`
- `write:issue` (zum Anlegen von Issues)

Mehr braucht es nicht, also kein `write:repository` und kein `admin`.

```ini
GIT_SERVER_URL=https://git.louis-degiorgi.ch
GIT_SERVER_TOKEN=…
```

## GitHub (nur lesend)

Alle `GITHUB_INTERVALL_MINUTEN` (Standard 30) schaut Missa in die eigenen Repos des Kontos
`GITHUB_KONTO` und meldet:

| Was | Stufe |
|-----|-------|
| fehlgeschlagener Actions-Build (Workflow, Branch, Link) | `wichtig` |
| neues Release | `info` |
| neues Issue (ohne Pull Requests) | `info` |

Beim ersten Blick auf ein Repo merkt sie sich nur den Stand. Gemeldet wird erst, was danach
neu dazukommt, damit es beim Start keine Flut alter Meldungen gibt. „Wie steht's auf GitHub?“
→ `github_ueberblick` (lesen): fehlgeschlagene Builds der letzten 7 Tage, neuestes Release und
offene Issues pro Repo. Lehnt GitHub das Token ab, kommt höchstens einmal am Tag ein `wichtig`.

**Token:** GitHub → *Settings → Developer settings → Fine-grained tokens*:

- Resource owner `Louidos`
- „All repositories“, oder nur die gewünschten Repos
- Repository permissions, alle nur **Read-only**:
  - *Actions*
  - *Contents*
  - *Issues*
  - *Metadata*

Missa schickt nur GET-Anfragen an `api.github.com`. Das ist die einzige Verbindung nach außen
dieser Funktion.

```ini
GITHUB_TOKEN=github_pat_…
GITHUB_KONTO=Louidos
# Optional, sonst alle eigenen Repos, die das Token lesen darf:
GITHUB_REPOS=Louidos/Missa-Core
```

## Claude-Code-Prompts aus groben Ideen

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Mach mir einen Prompt für Missa-Core: Missa soll …“ | ordnet die Idee in Gruppen und zeigt den fertigen Prompt | `claude_prompt_erstellen` (schreiben – speichert nur den Entwurf bei Missa) |
| „Ja, leg ihn als Issue an“ | legt das Issue „Claude-Code: …“ im Repo auf dem Git-Server an | `claude_prompt_als_issue` (zerstoererisch*) |

\* Ein Issue anzulegen zerstört nichts. Der Prompt soll aber ausdrücklich erst nach deinem Ja
im Repo landen. Die Stufe `zerstoererisch` sorgt dafür, dass die Rechte-Schicht vorher
nachfragt (wie beim Wartungsfenster).

**Die Vorlage** steht in [config/prompts/claude-code.md](../config/prompts/claude-code.md) und
lässt sich anpassen (eigener Pfad: `PROMPT_VORLAGE`). Sie enthält immer:

- den Abschnitt **„Repo-Aufbau und Arbeitsweise“** mit:
  - Struktur
  - Branch pro Gruppe
  - Conventional Commits
  - CHANGELOG
  - Tests
  - PR
  - SemVer
- pro Gruppe **Ziel**, **Aufgaben**, **Fertig, wenn**, **CHANGELOG-Zeile** und **Commit**.

Den Abschnitt „Repo-Aufbau und Arbeitsweise“ übernimmt Missa wörtlich aus der Vorlage, das
Modell schreibt nur die Gruppen. Fehlt in einer geänderten Vorlage etwas davon, sind die
Prompt-Werkzeuge aus, und im Log steht, was fehlt.

Missa prüft jede Gruppe:

- Der Commit muss ein Conventional Commit sein (`feat: …`, `fix: …`).
- Die CHANGELOG-Zeile beginnt mit einer Rubrik („Hinzugefügt – …“, „Behoben – …“).
- Aufgaben und Fertig-wenn haben mindestens einen Punkt.

Die Issues landen auf dem eigenen Git-Server. GitHub bleibt nur lesend.

## Netzfreigaben

Siehe [netzfreigaben.md](netzfreigaben.md):

- ai01 → Git-Server TCP 443
- ai01 → api.github.com TCP 443
