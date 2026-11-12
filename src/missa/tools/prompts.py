"""Claude-Code-Prompts aus groben Ideen – nach der Vorlage ``config/prompts/claude-code.md``.

Louis erzählt eine grobe Idee. Das Modell ordnet sie in Gruppen (Ziel, Aufgaben, Fertig
wenn, CHANGELOG-Zeile, Commit), ``claude_prompt_erstellen`` setzt daraus mit der Vorlage den
fertigen Prompt zusammen und zeigt ihn. Der Abschnitt „Repo-Aufbau und Arbeitsweise“ kommt
immer aus der Vorlage, nie vom Modell. Als Issue im gewünschten Repo auf dem Git-Server landet
der Prompt erst nach Louis' Ja (``claude_prompt_als_issue``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler

if TYPE_CHECKING:
    from missa.tools.git import GitUeberblick

ABSCHNITT = "## Repo-Aufbau und Arbeitsweise"
#: Das muss der Abschnitt „Repo-Aufbau und Arbeitsweise“ der Vorlage immer nennen.
REGELN = (
    "Struktur",
    "Branch pro Gruppe",
    "Conventional Commits",
    "CHANGELOG",
    "Tests",
    "PR",
    "SemVer",
)
GRUPPE_ANFANG = "<!-- gruppe -->"
GRUPPE_ENDE = "<!-- /gruppe -->"
#: Diese Felder hat jede Gruppe – als Platzhalter im Gruppen-Block der Vorlage.
GRUPPE_FELDER = ("ziel", "aufgaben", "fertig_wenn", "changelog", "commit")
PLATZHALTER = re.compile(r"\{\{(\w+)\}\}")
KOMMENTAR = re.compile(r"\A\s*<!--(?!\s*/?gruppe\s*-->).*?-->\s*", re.DOTALL)
COMMIT = re.compile(
    r"^(feat|fix|docs|test|refactor|chore|perf|build|ci|style)(\([\w.-]+\))?!?: \S.{0,99}$"
)
RUBRIKEN = ("Hinzugefügt", "Geändert", "Behoben", "Entfernt", "Sicherheit", "Veraltet")
TABELLE = """
CREATE TABLE IF NOT EXISTS claude_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    titel TEXT NOT NULL,
    text TEXT NOT NULL,
    erstellt TEXT NOT NULL,
    issue TEXT
)
"""


class PromptVorlage:
    """Die Vorlage mit Pflicht-Abschnitt und Gruppen-Block (wird beim Laden geprüft)."""

    def __init__(self, text: str) -> None:
        text = KOMMENTAR.sub("", text, count=1)
        if ABSCHNITT not in text:
            raise ValueError(f"Prompt-Vorlage: der Abschnitt „{ABSCHNITT}“ fehlt")
        abschnitt = text.split(ABSCHNITT, 1)[1].split("\n## ", 1)[0]
        fehlend = [r for r in REGELN if r not in abschnitt]
        if fehlend:
            raise ValueError(f"Prompt-Vorlage: „{ABSCHNITT}“ nennt nicht: {', '.join(fehlend)}")
        if text.count(GRUPPE_ANFANG) != 1 or text.count(GRUPPE_ENDE) != 1:
            raise ValueError(f"Prompt-Vorlage: genau ein {GRUPPE_ANFANG} … {GRUPPE_ENDE} nötig")
        kopf, rest = text.split(GRUPPE_ANFANG, 1)
        block, fuss = rest.split(GRUPPE_ENDE, 1)
        if ABSCHNITT not in kopf:
            raise ValueError(f"Prompt-Vorlage: „{ABSCHNITT}“ muss vor den Gruppen stehen")
        fehlend = [f for f in GRUPPE_FELDER if "{{" + f + "}}" not in block]
        if fehlend:
            raise ValueError(f"Prompt-Vorlage: im Gruppen-Block fehlen {', '.join(fehlend)}")
        erlaubt = {"repo", "titel", "idee", "nummer", "gruppe", *GRUPPE_FELDER}
        unbekannt = set(PLATZHALTER.findall(text)) - erlaubt
        if unbekannt:
            raise ValueError(f"Prompt-Vorlage: unbekannte Platzhalter {sorted(unbekannt)}")
        self.kopf, self.block, self.fuss = kopf, block.strip("\n"), fuss

    @classmethod
    def laden(cls, pfad: Path) -> PromptVorlage:
        try:
            return cls(pfad.read_text(encoding="utf-8"))
        except OSError as fehler:
            raise ValueError(f"Prompt-Vorlage {pfad} nicht lesbar: {fehler}") from fehler

    @staticmethod
    def _einsetzen(text: str, werte: dict[str, str]) -> str:
        # In einem Durchgang: eingesetzter Text wird nicht noch einmal ersetzt.
        return PLATZHALTER.sub(lambda m: werte.get(m.group(1), m.group(0)), text)

    def ausfuellen(self, repo: str, titel: str, idee: str, gruppen: list[dict[str, Any]]) -> str:
        allgemein = {"repo": repo, "titel": titel, "idee": idee.strip()}
        bloecke = [
            self._einsetzen(self.block, {**allgemein, **gruppe, "nummer": str(nummer)})
            for nummer, gruppe in enumerate(gruppen, start=1)
        ]
        text = (
            self._einsetzen(self.kopf, allgemein)
            + "\n\n".join(bloecke)
            + self._einsetzen(self.fuss, allgemein)
        )
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def liste(werte: object, feld: str) -> str:
    if isinstance(werte, str):
        werte = [z for z in werte.splitlines() if z.strip()]
    if not isinstance(werte, list) or not werte:
        raise WerkzeugFehler(f"„{feld}“ braucht mindestens einen Punkt.")
    punkte = [" ".join(str(w).split()).removeprefix("- ").strip() for w in werte]
    return "\n".join(f"- {p}" for p in punkte if p)


def gruppe_pruefen(gruppe: object, nummer: int) -> dict[str, str]:
    if not isinstance(gruppe, dict):
        raise WerkzeugFehler(f"Gruppe {nummer} ist keine Angabe mit Feldern.")
    titel = " ".join(str(gruppe.get("titel") or "").split())
    ziel = " ".join(str(gruppe.get("ziel") or "").split())
    changelog = " ".join(str(gruppe.get("changelog") or "").split())
    commit = " ".join(str(gruppe.get("commit") or "").split())
    if not titel or not ziel:
        raise WerkzeugFehler(f"Gruppe {nummer} braucht einen Titel und ein Ziel.")
    if not changelog.startswith(RUBRIKEN):
        raise WerkzeugFehler(
            f"Gruppe {nummer}: Die CHANGELOG-Zeile beginnt mit einer Rubrik ("
            + ", ".join(RUBRIKEN)
            + "), z. B. „Hinzugefügt – …“."
        )
    if not COMMIT.match(commit):
        raise WerkzeugFehler(
            f"Gruppe {nummer}: „{commit}“ ist kein Conventional Commit (z. B. „feat: …“)."
        )
    return {
        "gruppe": titel,
        "ziel": ziel,
        "aufgaben": liste(gruppe.get("aufgaben"), "aufgaben"),
        "fertig_wenn": liste(gruppe.get("fertig_wenn"), "fertig_wenn"),
        "changelog": changelog,
        "commit": commit,
    }


class ClaudePrompts:
    """Entwürfe für Claude-Code-Prompts; als Issue erst nach Ja."""

    def __init__(
        self,
        vorlage: PromptVorlage,
        datenbank: Path,
        zone: ZoneInfo,
        git: GitUeberblick | None = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.vorlage = vorlage
        self.datei = datenbank
        self.zone = zone
        self.git = git
        self._uhr = uhr or (lambda: datetime.now(zone))
        with verbinden(datenbank) as db:
            db.execute(TABELLE)

    async def erstellen(
        self, repo: str, titel: str, idee: str, gruppen: list[dict[str, Any]]
    ) -> str:
        repo = " ".join(str(repo or "").split())
        titel = " ".join(str(titel or "").split())
        if not repo or not titel:
            raise WerkzeugFehler("Für den Prompt brauche ich das Repo und einen Titel.")
        if not isinstance(gruppen, list) or not gruppen:
            raise WerkzeugFehler("Teil die Idee in mindestens eine Gruppe auf.")
        if len(gruppen) > 12:
            raise WerkzeugFehler("Höchstens 12 Gruppen pro Prompt.")
        geprueft = [gruppe_pruefen(g, n) for n, g in enumerate(gruppen, start=1)]
        text = self.vorlage.ausfuellen(repo, titel, str(idee or ""), geprueft)
        with verbinden(self.datei) as db:
            nummer = db.execute(
                "INSERT INTO claude_prompts (repo, titel, text, erstellt) VALUES (?, ?, ?, ?)",
                (repo, titel, text, self._uhr().isoformat(timespec="seconds")),
            ).lastrowid
        hinweis = (
            "NOCH KEIN ISSUE. Zeig Louis den ganzen Prompt. Erst nach seinem Ja rufe "
            f"claude_prompt_als_issue mit entwurf={nummer} auf."
            if self.git is not None
            else "Als Issue anlegen geht nicht, weil kein Git-Server eingerichtet ist "
            "(GIT_SERVER_URL) – Louis kann den Prompt kopieren."
        )
        return f"Entwurf {nummer} für {repo}:\n\n{text}\n{hinweis}"

    def entwurf(self, nummer: object) -> dict[str, Any]:
        try:
            schluessel = int(str(nummer).strip())
        except ValueError:
            raise WerkzeugFehler(f"„{nummer}“ ist keine Entwurfsnummer.") from None
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT * FROM claude_prompts WHERE id = ?", (schluessel,)
            ).fetchone()
        if zeile is None:
            raise WerkzeugFehler(f"Einen Prompt-Entwurf {nummer} gibt es nicht.")
        return dict(zeile)

    async def als_issue_vorschau(self, entwurf: object) -> str:
        daten = self.entwurf(entwurf)
        if daten["issue"]:
            raise WerkzeugFehler(f"Entwurf {daten['id']} ist schon ein Issue: {daten['issue']}")
        return (
            f"Im Repo {daten['repo']} auf dem Git-Server wird das Issue "
            f"„Claude-Code: {daten['titel']}“ mit dem Prompt aus Entwurf {daten['id']} angelegt."
        )

    async def als_issue(self, entwurf: object) -> str:
        if self.git is None:
            raise WerkzeugFehler("Kein Git-Server eingerichtet (GIT_SERVER_URL).")
        daten = self.entwurf(entwurf)
        if daten["issue"]:
            return f"Entwurf {daten['id']} ist schon ein Issue: {daten['issue']}"
        antwort = await self.git.issue_anlegen(
            daten["repo"],
            f"Claude-Code: {daten['titel']}",
            daten["text"] + "\n---\n_Von Missa aus einer groben Idee erstellt._\n",
        )
        with verbinden(self.datei) as db:
            db.execute("UPDATE claude_prompts SET issue = ? WHERE id = ?", (antwort, daten["id"]))
        return antwort


def prompt_werkzeuge(prompts: ClaudePrompts) -> list[Werkzeug]:
    gruppe = {
        "type": "object",
        "properties": {
            "titel": {"type": "string"},
            "ziel": {"type": "string", "description": "Ein Satz: was danach geht"},
            "aufgaben": {"type": "array", "items": {"type": "string"}},
            "fertig_wenn": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Prüfbare Bedingungen",
            },
            "changelog": {
                "type": "string",
                "description": "z. B. „Hinzugefügt – Überblick über …“",
            },
            "commit": {"type": "string", "description": "Conventional Commit, z. B. feat: …"},
        },
        "required": ["titel", "ziel", "aufgaben", "fertig_wenn", "changelog", "commit"],
    }
    werkzeuge = [
        Werkzeug(
            "claude_prompt_erstellen",
            "Macht aus einer groben Idee einen Claude-Code-Prompt nach Louis' Vorlage "
            "(Repo-Aufbau und Arbeitsweise kommen aus der Vorlage). Ordne die Idee in Gruppen "
            "mit Ziel, Aufgaben, Fertig-wenn, CHANGELOG-Zeile und Commit und zeig den Prompt.",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Ziel-Repo, z. B. Missa-Core"},
                    "titel": {"type": "string"},
                    "idee": {"type": "string", "description": "Kurz, was erreicht werden soll"},
                    "gruppen": {"type": "array", "items": gruppe},
                },
                "required": ["repo", "titel", "gruppen"],
            },
            prompts.erstellen,
            stufe=Stufe.SCHREIBEN,
        ),
    ]
    if prompts.git is not None:
        werkzeuge.append(
            Werkzeug(
                "claude_prompt_als_issue",
                "Legt einen gezeigten Prompt-Entwurf als Issue im Repo auf dem Git-Server an – "
                "nur nach Louis' Ja.",
                {
                    "type": "object",
                    "properties": {"entwurf": {"type": "integer"}},
                    "required": ["entwurf"],
                },
                prompts.als_issue,
                stufe=Stufe.ZERSTOERERISCH,
                vorschau=prompts.als_issue_vorschau,
            )
        )
    return werkzeuge
