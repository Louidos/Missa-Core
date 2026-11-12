<!--
Vorlage für Claude-Code-Prompts, die Missa aus einer groben Idee macht.

Platzhalter: {{repo}}, {{titel}}, {{idee}} und im Gruppen-Block {{nummer}}, {{gruppe}},
{{ziel}}, {{aufgaben}}, {{fertig_wenn}}, {{changelog}}, {{commit}}. Der Block zwischen
„gruppe“ und „/gruppe“ wird für jede Gruppe wiederholt. Der Abschnitt
„Repo-Aufbau und Arbeitsweise“ und die fünf Felder pro Gruppe müssen bleiben – sonst
startet Missa die Prompt-Werkzeuge nicht. Dieser Kommentar landet nicht im Prompt.
-->
# Auftrag: {{repo}} – {{titel}}

{{idee}}

## Repo-Aufbau und Arbeitsweise

Halte dich bei jeder Gruppe an diese Regeln:

- **Struktur:** Schau dir zuerst den Aufbau des Repos an (Quellcode, Tests, Doku,
  `CHANGELOG.md`, `README.md`, Beispielkonfiguration) und behalte ihn bei. Neue Dateien
  kommen an die passende Stelle. Nichts umbenennen oder verschieben, was nicht zur Aufgabe
  gehört.
- **Branch pro Gruppe:** Jede Gruppe bekommt einen eigenen Branch von `main`, ein eigenes
  Issue und einen eigenen Pull Request. Erst die nächste Gruppe anfangen, wenn die vorige
  gemergt ist.
- **Conventional Commits:** `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` –
  kurz, genau und auf Deutsch. Pro Gruppe der unten angegebene Commit.
- **CHANGELOG:** `CHANGELOG.md` nach Keep a Changelog. Jede Gruppe trägt ihre Zeile unter
  „## Unveröffentlicht“ in der passenden Rubrik ein, mit der Issue-Nummer.
- **Tests:** Jede neue Funktion bekommt Tests, alle Tests und Prüfungen (Linter,
  Formatierung) laufen grün. Externe Dienste, Hosts und APIs werden in den Tests gemockt.
- **PR:** Mit Beschreibung, Checkliste aus „Fertig, wenn“ und `Closes #<Issue>`. Erst
  mergen (Squash), wenn die CI grün ist.
- **SemVer:** Am Ende eine neue Version nach Semantic Versioning (`feat` → Minor, `fix` →
  Patch, Breaking Change → Major) mit Commit `chore: Version X.Y.Z`, Tag `vX.Y.Z` und
  GitHub-Release. Tags nie verschieben oder löschen.
- **Sicherheit:** Geheimnisse (Passwörter, API-Keys, `.env`) nie committen, nur
  Beispieldateien. Jede neue Einstellung kommt mit Erklärung in die Beispielkonfiguration.
- **Sprache:** Deutsch in Doku, Kommentaren, Commits und PRs.

<!-- gruppe -->
## Gruppe {{nummer}}: {{gruppe}}

**Ziel:** {{ziel}}

**Aufgaben:**

{{aufgaben}}

**Fertig, wenn:**

{{fertig_wenn}}

**CHANGELOG-Zeile:** {{changelog}}

**Commit:** `{{commit}}`
<!-- /gruppe -->

## Abschluss

Berichte am Ende pro Gruppe: geänderte Dateien, Testergebnis, PR-Link, gemergt ja/nein,
dazu die Version und offene Punkte.
