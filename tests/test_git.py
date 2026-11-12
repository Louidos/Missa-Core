"""Git-Server (Gitea/Forgejo), GitHub nur lesend und Claude-Code-Prompts nach Vorlage."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.config import Einstellungen, KonfigurationsFehler
from missa.core.rechte import Aktionsprotokoll, Rechte
from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.github import GitHubLeser
from missa.integrationen.gitserver import GitServer
from missa.integrationen.monitoring import QuellenFehler
from missa.tools import git_bauen, github_bauen, prompts_bauen, werkzeuge_laden
from missa.tools.git import GitHubWaechter, GitUeberblick, git_werkzeuge, github_werkzeuge
from missa.tools.prompts import ABSCHNITT, REGELN, ClaudePrompts, PromptVorlage, prompt_werkzeuge
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

VORLAGE = Path(__file__).resolve().parent.parent / "config" / "prompts" / "claude-code.md"
JETZT = datetime(2026, 11, 12, 9, 0, tzinfo=ZONE)


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(JETZT)


@pytest.fixture
def alarme(tmp_path: Path, uhr) -> Alarme:
    planer = Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([FakeKanal()]), uhr=uhr)
    return Alarme(planer.datei, ZONE, planer)


def kasten_mit(werkzeuge, tmp_path: Path) -> Werkzeugkasten:
    return Werkzeugkasten(werkzeuge, Rechte(Aktionsprotokoll(tmp_path / "protokoll.db", ZONE)))


# ----------------------------------------------------------------------------- Git-Server


def vor(tage: int) -> str:
    return (JETZT - timedelta(days=tage)).isoformat()


class FakeGitea:
    """Gitea/Forgejo-API im Speicher (für httpx.MockTransport)."""

    def __init__(self, art: str = "Forgejo") -> None:
        self.art = art
        self.anfragen: list[httpx.Request] = []
        self.issues: list[dict] = []
        self.repos = [
            {
                "name": "Missa-Core",
                "full_name": "louis/Missa-Core",
                "owner": {"login": "louis"},
                "default_branch": "main",
                "open_pr_counter": 1,
            },
            {
                "name": "homelab",
                "full_name": "louis/homelab",
                "owner": {"login": "louis"},
                "default_branch": "main",
                "open_pr_counter": 0,
            },
            {
                "name": "alt",
                "full_name": "louis/alt",
                "owner": {"login": "louis"},
                "archived": True,
            },
        ]
        self.branches = {
            "louis/Missa-Core": [
                {"name": "main", "commit": {"timestamp": vor(90)}},
                {"name": "feat/neu", "commit": {"timestamp": vor(3)}},
                {"name": "feat/vergessen", "commit": {"timestamp": vor(20)}},
            ],
            "louis/homelab": [{"name": "experiment", "commit": {"timestamp": vor(40)}}],
        }

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        if anfrage.headers.get("authorization") != "token geheim":
            return httpx.Response(401)
        pfad = anfrage.url.path
        if pfad == "/api/forgejo/v1/version":
            if self.art == "Forgejo":
                return httpx.Response(200, json={"version": "9.0.3+gitea-1.22.0"})
            return httpx.Response(404)
        if pfad == "/api/v1/version":
            return httpx.Response(200, json={"version": "1.22.6"})
        if pfad == "/api/v1/user/repos":
            return httpx.Response(200, json=self.repos)
        if pfad == "/api/v1/repos/louis/Missa-Core/pulls":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "title": "Caddy-Subdomains",
                        "user": {"login": "louis"},
                        "head": {"ref": "feat/caddy"},
                        "base": {"ref": "main"},
                        "updated_at": vor(2),
                    }
                ],
            )
        if pfad.endswith("/branches"):
            repo = pfad.removeprefix("/api/v1/repos/").removesuffix("/branches")
            return httpx.Response(200, json=self.branches.get(repo, []))
        if pfad.endswith("/issues") and anfrage.method == "POST":
            daten = json.loads(anfrage.content)
            nummer = len(self.issues) + 12
            self.issues.append({"pfad": pfad, **daten})
            return httpx.Response(
                201,
                json={"number": nummer, "html_url": f"https://git.test{pfad[7:]}/{nummer}"},
            )
        return httpx.Response(404)


@pytest.fixture
def gitea() -> FakeGitea:
    return FakeGitea()


@pytest.fixture
def git(gitea, alarme, uhr) -> GitUeberblick:
    server = GitServer("https://git.test", "geheim", transport=httpx.MockTransport(gitea))
    return GitUeberblick(server, ZONE, alarme, uhr=uhr)


@pytest.mark.parametrize(
    ("art", "erwartet"), [("Forgejo", "Forgejo 9.0.3+gitea-1.22.0"), ("Gitea", "Gitea 1.22.6")]
)
def test_gitea_oder_forgejo_erkennen(art, erwartet):
    server = GitServer("https://git.test/", "geheim", transport=httpx.MockTransport(FakeGitea(art)))
    assert asyncio.run(server.erkennen()) == erwartet


def test_altes_forgejo_nur_ueber_api_v1():
    def antwort(anfrage: httpx.Request) -> httpx.Response:
        if anfrage.url.path == "/api/v1/version":
            return httpx.Response(200, json={"version": "7.0.0+gitea-1.21.0"})
        return httpx.Response(404)

    server = GitServer("https://git.test", "x", transport=httpx.MockTransport(antwort))
    assert asyncio.run(server.erkennen()) == "Forgejo 7.0.0+gitea-1.21.0"


def test_kein_git_server():
    server = GitServer(
        "https://git.test", "x", transport=httpx.MockTransport(lambda _a: httpx.Response(404))
    )
    with pytest.raises(QuellenFehler, match="weder Gitea noch Forgejo"):
        asyncio.run(server.erkennen())


def test_issue_anlegen_im_repo(git, gitea, tmp_path):
    kasten = kasten_mit(git_werkzeuge(git), tmp_path)
    antwort = asyncio.run(
        kasten.ausfuehren(
            "git_issue_anlegen",
            {"repo": "missa-core", "titel": "Backup prüfen", "text": "Bitte  testen"},
            "a1",
        )
    )
    assert antwort.startswith("Issue #12 in louis/Missa-Core angelegt: Backup prüfen")
    assert gitea.issues == [
        {
            "pfad": "/api/v1/repos/louis/Missa-Core/issues",
            "title": "Backup prüfen",
            "body": "Bitte  testen",
        }
    ]


def test_issue_unbekanntes_oder_ungueltiges_repo(git, gitea):
    with pytest.raises(WerkzeugFehler, match=r"finde ich nicht\. Vorhanden: louis/Missa-Core"):
        asyncio.run(git.issue_anlegen("gibtsnicht", "x"))
    with pytest.raises(QuellenFehler):
        asyncio.run(git.issue_anlegen("../etc", "x"))
    with pytest.raises(WerkzeugFehler, match="Titel"):
        asyncio.run(git.issue_anlegen("homelab", "   "))
    assert gitea.issues == []


def test_falsches_token():
    server = GitServer("https://git.test", "falsch", transport=httpx.MockTransport(FakeGitea()))
    with pytest.raises(QuellenFehler, match="lehnt das Token ab"):
        asyncio.run(server.repos())


def test_ueberblick_fasst_offene_prs_zusammen(git, gitea):
    antwort = asyncio.run(git.ueberblick())
    assert antwort.startswith("Git-Server: Forgejo 9.0.3+gitea-1.22.0, 2 Repos.")
    assert "- louis/Missa-Core #7: Caddy-Subdomains (feat/caddy → main, von louis" in antwort
    assert "Offene Pull Requests (1):" in antwort
    # Für homelab (0 offene PRs) fragt Missa gar nicht erst nach.
    assert not any("homelab/pulls" in str(a.url) for a in gitea.anfragen)


def test_alte_branches(git):
    alt = asyncio.run(git.alte_branches())
    assert [(r, b) for r, b, _z in alt] == [
        ("louis/homelab", "experiment"),
        ("louis/Missa-Core", "feat/vergessen"),
    ]
    text = asyncio.run(git.alte_branches_anzeigen())
    assert "Branches ohne Commit seit 14 Tagen (2):" in text
    assert "- louis/Missa-Core: feat/vergessen (letzter Commit 23.10.2026, vor 20 Tagen)" in text


def test_alte_branches_woechentlich_als_info(git, alarme):
    asyncio.run(git.woechentlich_pruefen())
    alarm = alarme.zuletzt("git:alte-branches:2026-W46")
    assert alarm is not None and alarm.stufe is AlarmStufe.INFO
    assert "feat/vergessen" in alarm.text and "experiment" in alarm.text
    asyncio.run(git.woechentlich_pruefen())
    assert len([a for a in alarme.liste() if a.schluessel.startswith("git:")]) == 1


# ----------------------------------------------------------------------------- GitHub


class FakeGitHub:
    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []
        self.laeufe = [
            {
                "id": 1,
                "name": "CI",
                "head_branch": "main",
                "display_title": "feat: alt",
                "created_at": "2026-11-01T10:00:00Z",
                "html_url": "https://github.com/Louidos/Missa-Core/actions/runs/1",
            }
        ]
        self.releases = [{"id": 10, "tag_name": "v0.7.0", "html_url": "https://x/r/10"}]
        self.issues = [
            {"id": 100, "number": 50, "title": "Alt", "user": {"login": "louis"}},
            {"id": 101, "number": 51, "title": "Ein PR", "pull_request": {}},
        ]
        self.status = 200

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        if self.status != 200:
            return httpx.Response(self.status)
        pfad = anfrage.url.path
        if pfad == "/user/repos":
            return httpx.Response(
                200,
                json=[
                    {"full_name": "Louidos/Missa-Core", "owner": {"login": "Louidos"}},
                    {
                        "full_name": "Louidos/Archiv",
                        "owner": {"login": "Louidos"},
                        "archived": True,
                    },
                    {"full_name": "jemand/fremd", "owner": {"login": "jemand"}},
                ],
            )
        if pfad == "/repos/Louidos/Missa-Core/actions/runs":
            assert anfrage.url.params["status"] == "failure"
            return httpx.Response(200, json={"workflow_runs": self.laeufe})
        if pfad == "/repos/Louidos/Missa-Core/releases":
            return httpx.Response(200, json=self.releases)
        if pfad == "/repos/Louidos/Missa-Core/issues":
            return httpx.Response(200, json=self.issues)
        return httpx.Response(404)


@pytest.fixture
def github_api() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def github(github_api, alarme, tmp_path, uhr) -> GitHubWaechter:
    leser = GitHubLeser(
        "ghp_test", "Louidos", api_url="https://api.github.test",
        transport=httpx.MockTransport(github_api),
    )  # fmt: skip
    return GitHubWaechter(leser, tmp_path / "missa.db", ZONE, alarme, uhr=uhr)


def github_alarme(alarme: Alarme) -> list:
    return [a for a in alarme.liste() if a.schluessel.startswith("github:")]


def test_github_erster_blick_merkt_nur_den_stand(github, alarme):
    asyncio.run(github.pruefen())
    assert github_alarme(alarme) == []


def test_fehlgeschlagener_build_wird_gemeldet(github, github_api, alarme):
    asyncio.run(github.pruefen())
    github_api.laeufe.insert(
        0,
        {
            "id": 2,
            "name": "CI",
            "head_branch": "claude/neu",
            "display_title": "feat: Git-Server",
            "created_at": "2026-11-12T07:55:00Z",
            "html_url": "https://github.com/Louidos/Missa-Core/actions/runs/2",
        },
    )
    github_api.releases.insert(0, {"id": 11, "tag_name": "v0.8.0", "html_url": "https://x/r/11"})
    github_api.issues.insert(0, {"id": 102, "number": 60, "title": "Neu", "user": {"login": "a"}})
    asyncio.run(github.pruefen())
    meldungen = {a.titel: a for a in github_alarme(alarme)}
    build = meldungen["GitHub-Build fehlgeschlagen: Louidos/Missa-Core"]
    assert build.stufe is AlarmStufe.WICHTIG
    assert "CI auf claude/neu" in build.text and "actions/runs/2" in build.text
    assert meldungen["Neues Release in Louidos/Missa-Core: v0.8.0"].stufe is AlarmStufe.INFO
    assert "Neu (von a)" in meldungen["Neues Issue in Louidos/Missa-Core: #60"].text
    assert len(meldungen) == 3
    # Nichts doppelt.
    asyncio.run(github.pruefen())
    assert len(github_alarme(alarme)) == 3


def test_github_nur_get_und_nur_eigene_repos(github, github_api):
    asyncio.run(github.pruefen())
    assert {a.method for a in github_api.anfragen} == {"GET"}
    assert all(a.headers["authorization"] == "Bearer ghp_test" for a in github_api.anfragen)
    assert asyncio.run(github.repos()) == ["Louidos/Missa-Core"]


def test_github_token_abgelehnt(github, github_api, alarme):
    github_api.status = 401
    asyncio.run(github.pruefen())
    alarm = alarme.zuletzt("github:fehler")
    assert alarm is not None and alarm.stufe is AlarmStufe.WICHTIG
    assert "GITHUB_TOKEN" in alarm.text


def test_github_ueberblick(github, github_api, tmp_path):
    kasten = kasten_mit(github_werkzeuge(github), tmp_path)
    antwort = asyncio.run(kasten.ausfuehren("github_ueberblick", {}, "a1"))
    assert "GitHub (Louidos, 1 Repos):" in antwort
    assert "- Louidos/Missa-Core: ❌ 1 fehlgeschlagene Builds in 7 Tagen" not in antwort
    assert "neuestes Release v0.7.0, 1 offene Issues" in antwort
    github_api.laeufe[0]["created_at"] = "2026-11-11T10:00:00Z"
    antwort = asyncio.run(github.ueberblick())
    assert "❌ 1 fehlgeschlagene Builds in 7 Tagen (zuletzt CI auf main)" in antwort


def test_feste_repos_ohne_liste(github_api, alarme, tmp_path):
    leser = GitHubLeser(
        "t", "Louidos", api_url="https://api.github.test", transport=httpx.MockTransport(github_api)
    )
    waechter = GitHubWaechter(
        leser, tmp_path / "missa.db", ZONE, alarme, repos=("Louidos/Missa-Core",)
    )
    asyncio.run(waechter.pruefen())
    assert all(a.url.path != "/user/repos" for a in github_api.anfragen)


# ----------------------------------------------------------------------------- Prompts


GRUPPE = {
    "titel": "Git-Überblick",
    "ziel": "Missa kennt die eigenen Repos.",
    "aufgaben": ["Gitea erkennen", "Issues anlegen"],
    "fertig_wenn": ["Issue landet auf dem Git-Server"],
    "changelog": "Hinzugefügt – Überblick über eigene Repos",
    "commit": "feat: Git-Server und GitHub-Überblick",
}


@pytest.fixture
def prompts(git, tmp_path) -> ClaudePrompts:
    return ClaudePrompts(PromptVorlage.laden(VORLAGE), tmp_path / "missa.db", ZONE, git)


def test_vorlage_im_repo_hat_pflicht_abschnitt():
    text = VORLAGE.read_text(encoding="utf-8")
    abschnitt = text.split(ABSCHNITT, 1)[1].split("\n## ", 1)[0]
    for regel in REGELN:
        assert regel in abschnitt
    for feld in ("Ziel", "Aufgaben", "Fertig, wenn", "CHANGELOG-Zeile", "Commit"):
        assert f"**{feld}:**" in text
    PromptVorlage(text)


@pytest.mark.parametrize(
    ("aenderung", "fehler"),
    [
        (lambda t: t.replace(ABSCHNITT, "## Sonstiges"), "fehlt"),
        (lambda t: t.replace("SemVer", "Versionen"), "SemVer"),
        (lambda t: t.replace("{{commit}}", ""), "commit"),
        (lambda t: t.replace("<!-- /gruppe -->", ""), "gruppe"),
        (lambda t: t + "\n{{geheim}}\n", "unbekannte"),
    ],
)
def test_kaputte_vorlage_wird_abgelehnt(aenderung, fehler):
    with pytest.raises(ValueError, match=fehler):
        PromptVorlage(aenderung(VORLAGE.read_text(encoding="utf-8")))


def test_grobe_idee_wird_prompt_nach_vorlage(prompts, git, gitea, tmp_path):
    kasten = kasten_mit(prompt_werkzeuge(prompts), tmp_path)
    antwort = asyncio.run(
        kasten.ausfuehren(
            "claude_prompt_erstellen",
            {
                "repo": "Missa-Core",
                "titel": "Git-Überblick",
                "idee": "Missa soll meine Repos kennen {{repo}}",
                "gruppen": [GRUPPE, {**GRUPPE, "titel": "Prompts", "commit": "feat: Prompts"}],
            },
            "a1",
        )
    )
    assert antwort.startswith("Entwurf 1 für Missa-Core:")
    assert "# Auftrag: Missa-Core – Git-Überblick" in antwort
    assert ABSCHNITT in antwort and "Branch pro Gruppe" in antwort
    assert "## Gruppe 1: Git-Überblick" in antwort and "## Gruppe 2: Prompts" in antwort
    assert "**Aufgaben:**\n\n- Gitea erkennen\n- Issues anlegen" in antwort
    assert "**CHANGELOG-Zeile:** Hinzugefügt – Überblick über eigene Repos" in antwort
    assert "**Commit:** `feat: Prompts`" in antwort
    assert "{{repo}}" in antwort  # Text aus der Idee wird nicht als Platzhalter ersetzt
    assert "<!--" not in antwort
    assert "NOCH KEIN ISSUE" in antwort
    assert gitea.issues == []


def test_prompt_erst_nach_ja_als_issue(prompts, gitea, tmp_path):
    asyncio.run(prompts.erstellen("Missa-Core", "Git-Überblick", "Idee", [GRUPPE]))
    kasten = kasten_mit(prompt_werkzeuge(prompts), tmp_path)
    rueckfrage = asyncio.run(kasten.ausfuehren("claude_prompt_als_issue", {"entwurf": 1}, "a1"))
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and "Missa-Core" in rueckfrage
    assert gitea.issues == []

    antwort = asyncio.run(
        kasten.ausfuehren("claude_prompt_als_issue", {"entwurf": 1}, "a2", nachricht="Ja")
    )
    assert "Issue #12 in louis/Missa-Core angelegt: Claude-Code: Git-Überblick" in antwort
    assert len(gitea.issues) == 1
    assert gitea.issues[0]["title"] == "Claude-Code: Git-Überblick"
    assert ABSCHNITT in gitea.issues[0]["body"]
    # Ein zweites Mal legt sie kein doppeltes Issue an.
    nochmal = asyncio.run(prompts.als_issue(1))
    assert "schon ein Issue" in nochmal and len(gitea.issues) == 1


@pytest.mark.parametrize(
    ("aenderung", "fehler"),
    [
        ({"commit": "Git-Server einbauen"}, "kein Conventional Commit"),
        ({"changelog": "Neu: Git"}, "Rubrik"),
        ({"aufgaben": []}, "mindestens einen Punkt"),
        ({"ziel": ""}, "Ziel"),
    ],
)
def test_ungueltige_gruppen(prompts, aenderung, fehler):
    with pytest.raises(WerkzeugFehler, match=fehler):
        asyncio.run(prompts.erstellen("Missa-Core", "T", "", [{**GRUPPE, **aenderung}]))


def test_ohne_git_server_nur_entwurf(tmp_path):
    prompts = ClaudePrompts(PromptVorlage.laden(VORLAGE), tmp_path / "missa.db", ZONE)
    namen = [w.name for w in prompt_werkzeuge(prompts)]
    assert namen == ["claude_prompt_erstellen"]
    antwort = asyncio.run(prompts.erstellen("Missa-Core", "T", "", [GRUPPE]))
    assert "kein Git-Server eingerichtet" in antwort


# ----------------------------------------------------------------------------- Einstellungen


def test_git_und_github_nur_mit_konfiguration(tmp_path):
    basis = {"MISSA_API_KEY": "k", "MISSA_DATEN": str(tmp_path)}
    ohne = Einstellungen.aus_umgebung(basis)
    assert git_bauen(ohne, None) is None and github_bauen(ohne, None) is None
    assert prompts_bauen(ohne, None) is not None  # Vorlage aus dem Repo
    mit = Einstellungen.aus_umgebung(
        {
            **basis,
            "GIT_SERVER_URL": "https://git.louis-degiorgi.ch/",
            "GIT_SERVER_TOKEN": "gt",
            "GITHUB_TOKEN": "ghp",
            "GITHUB_KONTO": "Louidos",
            "GITHUB_REPOS": "Louidos/Missa-Core",
        }
    )
    mit.pruefen()
    assert mit.git_server_url == "https://git.louis-degiorgi.ch"
    assert {"gt", "ghp"} <= set(mit.geheimnisse)
    assert git_bauen(mit, None) is not None
    waechter = github_bauen(mit, None)
    assert waechter is not None and waechter.feste_repos == ("louidos/missa-core",)
    kasten = werkzeuge_laden(
        mit, git=git_bauen(mit, None), github=waechter, prompts=prompts_bauen(mit, None)
    )
    for name in ("git_issue_anlegen", "git_ueberblick", "github_ueberblick"):
        assert name in kasten


def test_falsche_einstellungen(tmp_path):
    basis = {"MISSA_API_KEY": "k", "MISSA_DATEN": str(tmp_path)}
    with pytest.raises(KonfigurationsFehler, match="GITHUB_REPOS"):
        Einstellungen.aus_umgebung({**basis, "GITHUB_REPOS": "nur-ein-name"}).pruefen()
    with pytest.raises(KonfigurationsFehler, match="GIT_SERVER_URL"):
        Einstellungen.aus_umgebung({**basis, "GIT_SERVER_URL": "git.example.ch"}).pruefen()


def test_prompts_aus_ohne_gueltige_vorlage(tmp_path):
    kaputt = tmp_path / "vorlage.md"
    kaputt.write_text("# nur ein Titel\n", encoding="utf-8")
    einstellungen = Einstellungen.aus_umgebung(
        {"MISSA_API_KEY": "k", "MISSA_DATEN": str(tmp_path), "PROMPT_VORLAGE": str(kaputt)}
    )
    assert prompts_bauen(einstellungen, None) is None


def test_app_verdrahtet_git_github_und_prompts(tmp_path, gitea, github_api):
    """Voll eingerichtet: alle Werkzeuge da, GitHub nur lesend, Routinen eingeplant."""
    from missa.core.app import create_app
    from missa.core.werkzeuge import Stufe

    def weiche(anfrage: httpx.Request) -> httpx.Response:
        return github_api(anfrage) if anfrage.url.host == "api.github.test" else gitea(anfrage)

    e = Einstellungen(
        api_key="x",
        daten=tmp_path,
        git_server_url="https://git.test",
        git_server_token="geheim",
        github_token="ghp_test",
        github_konto="Louidos",
        github_api_url="https://api.github.test",
    )
    app = create_app(e, git_transport=httpx.MockTransport(weiche))
    kasten: Werkzeugkasten = app.state.dienst.werkzeuge
    stufen = {w.name: w.stufe for w in kasten}
    assert stufen["git_issue_anlegen"] is Stufe.SCHREIBEN
    assert stufen["github_ueberblick"] is Stufe.LESEN
    assert stufen["claude_prompt_als_issue"] is Stufe.ZERSTOERERISCH
    github_namen = [n for n in stufen if n.startswith("github")]
    assert all(stufen[n] is Stufe.LESEN for n in github_namen)
    assert app.state.git is not None and app.state.github is not None
