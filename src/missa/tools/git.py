"""Überblick über die eigenen Repos: Git-Server (Gitea/Forgejo) und GitHub (nur lesend).

**Git-Server** (``GIT_SERVER_URL``, Token ``GIT_SERVER_TOKEN``):

- „Leg im Repo X ein Issue an: …“ → ``git_issue_anlegen`` (schreiben)
- offene Pull Requests zusammenfassen (lesen)
- alte Branches: kein Commit seit ``GIT_ALTE_BRANCHES_TAGE`` (14) Tagen, wöchentlich als
  ``info`` gemeldet und auf Nachfrage (lesen)

**GitHub** (``GITHUB_TOKEN`` nur lesend, Konto ``GITHUB_KONTO``): Alle
``GITHUB_INTERVALL_MINUTEN`` schaut Missa nach fehlgeschlagenen Actions-Builds (``wichtig``),
neuen Releases und neuen Issues (``info``). Beim ersten Blick auf ein Repo merkt sie sich nur
den Stand – gemeldet wird erst, was danach neu dazukommt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from missa.core.alarme import AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.github import GitHubLeser
from missa.integrationen.gitserver import GitServer, name_pruefen
from missa.integrationen.monitoring import QuellenFehler

if TYPE_CHECKING:
    from missa.core.alarme import Alarme

log = logging.getLogger(__name__)

TABELLE = """
CREATE TABLE IF NOT EXISTS github_gesehen (
    art TEXT NOT NULL,
    schluessel TEXT NOT NULL,
    zeit TEXT NOT NULL,
    PRIMARY KEY (art, schluessel)
)
"""
MAX_TITEL = 200
MAX_TEXT = 60_000


def zeit_lesen(text: object) -> datetime | None:
    try:
        zeit = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeit if zeit.tzinfo else zeit.replace(tzinfo=UTC)


class GitUeberblick:
    """Repos, Pull Requests, alte Branches und Issues auf dem eigenen Git-Server."""

    def __init__(
        self,
        server: GitServer,
        zone: ZoneInfo,
        alarme: Alarme | None = None,
        *,
        besitzer: str = "",
        alte_tage: int = 14,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.server = server
        self.zone = zone
        self.alarme = alarme
        self.besitzer = besitzer.strip()
        self.alte_tage = alte_tage
        self._uhr = uhr or (lambda: datetime.now(zone))

    async def _repos(self) -> list[dict]:
        return [r for r in await self.server.repos() if not r.get("archived")]

    async def repo_finden(self, name: object) -> tuple[str, str]:
        """``Missa-Core`` oder ``louis/Missa-Core`` → (Besitzer, Repo) wie auf dem Server."""
        text = str(name or "").strip().strip("/")
        if "/" in text:
            besitzer, _, repo = text.partition("/")
            return name_pruefen(besitzer, "Konto"), name_pruefen(repo)
        gesucht = name_pruefen(text).casefold()
        repos = await self._repos()
        treffer = [r for r in repos if str(r.get("name", "")).casefold() == gesucht]
        if self.besitzer and len(treffer) > 1:
            treffer = [
                r
                for r in treffer
                if str((r.get("owner") or {}).get("login", "")).casefold()
                == self.besitzer.casefold()
            ] or treffer
        if len(treffer) == 1:
            return str(treffer[0]["owner"]["login"]), str(treffer[0]["name"])
        if not treffer:
            namen = ", ".join(sorted(str(r.get("full_name", "?")) for r in repos)) or "keine"
            raise WerkzeugFehler(f"Ein Repo „{text}“ finde ich nicht. Vorhanden: {namen}.")
        namen = ", ".join(sorted(str(r.get("full_name")) for r in treffer))
        raise WerkzeugFehler(f"„{text}“ gibt es mehrmals ({namen}) – welches meinst du?")

    async def issue_anlegen(self, repo: str, titel: str, text: str = "") -> str:
        titel = " ".join(str(titel or "").split())
        if not titel:
            raise WerkzeugFehler("Ein Issue braucht einen Titel.")
        if len(titel) > MAX_TITEL or len(text or "") > MAX_TEXT:
            raise WerkzeugFehler("Titel oder Text des Issues sind zu lang.")
        besitzer, name = await self.repo_finden(repo)
        try:
            issue = await self.server.issue_anlegen(besitzer, name, titel, text or "")
        except QuellenFehler as fehler:
            raise WerkzeugFehler(str(fehler)) from fehler
        return f"Issue #{issue['number']} in {besitzer}/{name} angelegt: {titel}" + (
            f" – {issue['html_url']}" if issue.get("html_url") else ""
        )

    async def ueberblick(self) -> str:
        """Server, Repos und offene Pull Requests (lesen)."""
        art = await self.server.erkennen()
        repos = await self._repos()
        zeilen = [f"Git-Server: {art}, {len(repos)} Repos."]
        offen: list[str] = []
        for repo in sorted(repos, key=lambda r: str(r.get("full_name", "")).casefold()):
            besitzer, name = str(repo["owner"]["login"]), str(repo["name"])
            if not repo.get("open_pr_counter", 1):
                continue
            try:
                pulls = await self.server.pulls(besitzer, name)
            except QuellenFehler as fehler:
                offen.append(f"- {besitzer}/{name}: nicht abrufbar ({fehler})")
                continue
            for pr in pulls:
                aktualisiert = zeit_lesen(pr.get("updated_at"))
                alter = (
                    f", zuletzt geändert {aktualisiert.astimezone(self.zone):%d.%m.%Y}"
                    if aktualisiert
                    else ""
                )
                offen.append(
                    f"- {besitzer}/{name} #{pr.get('number')}: {pr.get('title', '?')} "
                    f"({(pr.get('head') or {}).get('ref', '?')} → "
                    f"{(pr.get('base') or {}).get('ref', '?')}, von "
                    f"{(pr.get('user') or {}).get('login', '?')}{alter})"
                )
        if offen:
            zeilen.append(f"Offene Pull Requests ({sum(1 for o in offen if '#' in o)}):")
            zeilen += offen
        else:
            zeilen.append("Keine offenen Pull Requests.")
        return "\n".join(zeilen)

    async def alte_branches(self) -> list[tuple[str, str, datetime]]:
        """(Repo, Branch, letzter Commit) für Branches ohne Commit seit ``alte_tage`` Tagen."""
        grenze = self._uhr() - timedelta(days=self.alte_tage)
        alt: list[tuple[str, str, datetime]] = []
        for repo in await self._repos():
            besitzer, name = str(repo["owner"]["login"]), str(repo["name"])
            standard = str(repo.get("default_branch") or "")
            for branch in await self.server.branches(besitzer, name):
                if branch.get("name") == standard:
                    continue
                zeit = zeit_lesen((branch.get("commit") or {}).get("timestamp"))
                if zeit is not None and zeit < grenze:
                    alt.append((f"{besitzer}/{name}", str(branch.get("name")), zeit))
        return sorted(alt, key=lambda e: (e[0].casefold(), e[2]))

    def _branches_text(self, alt: list[tuple[str, str, datetime]]) -> str:
        return "\n".join(
            f"- {repo}: {branch} (letzter Commit {zeit.astimezone(self.zone):%d.%m.%Y}, "
            f"vor {(self._uhr() - zeit).days} Tagen)"
            for repo, branch, zeit in alt
        )

    async def alte_branches_anzeigen(self) -> str:
        alt = await self.alte_branches()
        if not alt:
            return f"Keine Branches ohne Commit seit {self.alte_tage} Tagen."
        return f"Branches ohne Commit seit {self.alte_tage} Tagen ({len(alt)}):\n" + (
            self._branches_text(alt)
        )

    async def woechentlich_pruefen(self) -> None:
        """Wöchentliche Routine: alte Branches als ``info`` melden."""
        if self.alarme is None:
            return
        alt = await self.alte_branches()
        if not alt:
            return
        woche = self._uhr().strftime("%G-W%V")
        self.alarme.melden(
            AlarmStufe.INFO,
            f"{len(alt)} alte Branches auf dem Git-Server",
            f"Kein Commit seit {self.alte_tage} Tagen – aufräumen oder weiterarbeiten?\n"
            + self._branches_text(alt),
            schluessel=f"git:alte-branches:{woche}",
            quelle="git",
        )


class GitHubWaechter:
    """Meldet fehlgeschlagene Builds, neue Releases und neue Issues der eigenen GitHub-Repos."""

    def __init__(
        self,
        leser: GitHubLeser,
        datenbank: Path,
        zone: ZoneInfo,
        alarme: Alarme | None = None,
        *,
        repos: tuple[str, ...] = (),
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.leser = leser
        self.datei = datenbank
        self.zone = zone
        self.alarme = alarme
        self.feste_repos = repos
        self._uhr = uhr or (lambda: datetime.now(zone))
        with verbinden(datenbank) as db:
            db.execute(TABELLE)

    async def repos(self) -> list[str]:
        return list(self.feste_repos) or await self.leser.repos()

    def _gesehen(self, art: str) -> set[str]:
        with verbinden(self.datei) as db:
            zeilen = db.execute("SELECT schluessel FROM github_gesehen WHERE art = ?", (art,))
            return {z["schluessel"] for z in zeilen}

    def _merken(self, art: str, schluessel: list[str]) -> None:
        jetzt = self._uhr().isoformat(timespec="seconds")
        with verbinden(self.datei) as db:
            db.executemany(
                "INSERT OR IGNORE INTO github_gesehen (art, schluessel, zeit) VALUES (?, ?, ?)",
                [(art, s, jetzt) for s in schluessel],
            )

    async def _neues(self, repo: str) -> list[tuple[AlarmStufe, str, str, str]]:
        """(Stufe, Titel, Text, Schlüssel) für alles Neue in einem Repo."""
        ereignisse: list[tuple[AlarmStufe, str, str, str]] = []
        for lauf in await self.leser.fehlgeschlagene_builds(repo):
            name = lauf.get("name") or lauf.get("display_title") or "Workflow"
            ereignisse.append(
                (
                    AlarmStufe.WICHTIG,
                    f"GitHub-Build fehlgeschlagen: {repo}",
                    f"{name} auf {lauf.get('head_branch', '?')} "
                    f"({lauf.get('display_title') or lauf.get('event', '')}) – "
                    f"{lauf.get('html_url', '')}",
                    f"build:{lauf.get('id')}",
                )
            )
        for release in await self.leser.releases(repo):
            ereignisse.append(
                (
                    AlarmStufe.INFO,
                    f"Neues Release in {repo}: {release.get('tag_name', '?')}",
                    f"{release.get('name') or release.get('tag_name', '')} – "
                    f"{release.get('html_url', '')}",
                    f"release:{release.get('id')}",
                )
            )
        for issue in await self.leser.issues(repo):
            ereignisse.append(
                (
                    AlarmStufe.INFO,
                    f"Neues Issue in {repo}: #{issue.get('number')}",
                    f"{issue.get('title', '?')} (von "
                    f"{(issue.get('user') or {}).get('login', '?')}) – "
                    f"{issue.get('html_url', '')}",
                    f"issue:{issue.get('id')}",
                )
            )
        return ereignisse

    async def pruefen(self) -> None:
        """Regelmäßiger Blick auf GitHub (vom Zeitplaner)."""
        try:
            repos = await self.repos()
        except QuellenFehler as fehler:
            log.warning("GitHub: %s", fehler)
            self._fehler_melden(str(fehler))
            return
        bekannt = self._gesehen("repo")
        for repo in repos:
            try:
                ereignisse = await self._neues(repo)
            except QuellenFehler as fehler:
                log.warning("GitHub %s: %s", repo, fehler)
                self._fehler_melden(str(fehler))
                continue
            gesehen = self._gesehen(repo)
            neu = [e for e in ereignisse if e[3] not in gesehen]
            if repo in bekannt and self.alarme is not None:
                for stufe, titel, text, schluessel in neu:
                    self.alarme.melden(
                        stufe,
                        titel,
                        text,
                        schluessel=f"github:{repo}:{schluessel}",
                        quelle="github",
                    )
            self._merken(repo, [e[3] for e in neu])
            if repo not in bekannt:
                self._merken("repo", [repo])

    def _fehler_melden(self, grund: str) -> None:
        if self.alarme is not None and ("Token" in grund or "verweigert" in grund):
            self.alarme.melden(
                AlarmStufe.WICHTIG,
                "GitHub-Überblick klappt nicht",
                grund,
                schluessel="github:fehler",
                quelle="github",
                ruhefenster=timedelta(days=1),
            )

    async def ueberblick(self) -> str:
        """Fehlgeschlagene Builds (7 Tage), neueste Releases und offene Issues pro Repo."""
        try:
            repos = await self.repos()
        except QuellenFehler as fehler:
            raise WerkzeugFehler(str(fehler)) from fehler
        grenze = self._uhr() - timedelta(days=7)
        zeilen = [f"GitHub ({self.leser.konto or 'eigene Repos'}, {len(repos)} Repos):"]
        for repo in repos:
            try:
                builds = await self.leser.fehlgeschlagene_builds(repo)
                releases = await self.leser.releases(repo)
                issues = await self.leser.issues(repo)
            except QuellenFehler as fehler:
                zeilen.append(f"- {repo}: nicht abrufbar ({fehler})")
                continue
            teile: list[str] = []
            frisch = [b for b in builds if (z := zeit_lesen(b.get("created_at"))) and z >= grenze]
            if frisch:
                teile.append(
                    f"❌ {len(frisch)} fehlgeschlagene Builds in 7 Tagen (zuletzt "
                    f"{frisch[0].get('name', '?')} auf {frisch[0].get('head_branch', '?')})"
                )
            if releases:
                teile.append(f"neuestes Release {releases[0].get('tag_name', '?')}")
            teile.append(f"{len(issues)} offene Issues" if issues else "keine offenen Issues")
            zeilen.append(f"- {repo}: " + ", ".join(teile))
        return "\n".join(zeilen)


def git_werkzeuge(git: GitUeberblick) -> list[Werkzeug]:
    return [
        Werkzeug(
            "git_issue_anlegen",
            "Legt auf dem eigenen Git-Server ein Issue an („Leg im Repo X ein Issue an: …“).",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repo-Name oder Besitzer/Repo"},
                    "titel": {"type": "string"},
                    "text": {"type": "string", "description": "Beschreibung (Markdown)"},
                },
                "required": ["repo", "titel"],
            },
            git.issue_anlegen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "git_ueberblick",
            "Zeigt den eigenen Git-Server (Gitea/Forgejo), die Repos und fasst offene Pull "
            "Requests zusammen.",
            {"type": "object", "properties": {}},
            git.ueberblick,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "git_alte_branches",
            "Zeigt Branches auf dem Git-Server, die seit Wochen keinen Commit mehr hatten.",
            {"type": "object", "properties": {}},
            git.alte_branches_anzeigen,
            stufe=Stufe.LESEN,
        ),
    ]


def github_werkzeuge(github: GitHubWaechter) -> list[Werkzeug]:
    return [
        Werkzeug(
            "github_ueberblick",
            "Zeigt die eigenen GitHub-Repos: fehlgeschlagene Builds, neueste Releases, "
            "offene Issues (nur lesend).",
            {"type": "object", "properties": {}},
            github.ueberblick,
            stufe=Stufe.LESEN,
        ),
    ]
