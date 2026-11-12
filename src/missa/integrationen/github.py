"""GitHub, nur lesend: fehlgeschlagene Actions-Builds, Releases und Issues der eigenen Repos.

Der Zugang ist ein fein abgestuftes Token (fine-grained) nur mit Leserechten
(``GITHUB_TOKEN``, Konto ``GITHUB_KONTO``). Diese Klasse kennt nur GET-Anfragen. GitHub ist
die einzige Verbindung nach außen dieser Funktion.
"""

from __future__ import annotations

from typing import Any

import httpx

from missa.integrationen.gitserver import name_pruefen
from missa.integrationen.monitoring import QuellenFehler

API_URL = "https://api.github.com"
TIMEOUT = 15.0
MAX_SEITEN = 5


def repo_pruefen(voller_name: object) -> str:
    """``Louidos/Missa-Core`` – Besitzer und Name, beide streng geprüft."""
    besitzer, _, name = str(voller_name or "").strip().partition("/")
    return f"{name_pruefen(besitzer, 'Konto')}/{name_pruefen(name)}"


class GitHubLeser:
    """Liest aus der GitHub-REST-API, ausschließlich per GET."""

    def __init__(
        self,
        token: str,
        konto: str,
        *,
        api_url: str = API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.konto = konto.strip()
        self.api_url = api_url.rstrip("/")
        self._kopf = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "missa-core",
        }
        self.transport = transport
        self.timeout = timeout

    async def lesen(self, pfad: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``<api>/<pfad>``. Es gibt bewusst nur GET."""
        adresse = f"{self.api_url}/{pfad.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout, trust_env=True
            ) as client:
                antwort = await client.get(adresse, params=params, headers=self._kopf)
        except httpx.HTTPError as fehler:
            raise QuellenFehler(
                f"GitHub ist nicht erreichbar: {fehler or type(fehler).__name__}"
            ) from fehler
        if antwort.status_code == 401:
            raise QuellenFehler("GitHub lehnt das Token ab (HTTP 401) – GITHUB_TOKEN prüfen")
        if antwort.status_code == 403:
            if antwort.headers.get("x-ratelimit-remaining") == "0":
                raise QuellenFehler("GitHub: Abfrage-Limit erreicht, später wieder")
            raise QuellenFehler(f"GitHub verweigert {pfad} (HTTP 403) – Token-Rechte prüfen")
        if antwort.status_code == 404:
            raise QuellenFehler(
                f"GitHub: {pfad} nicht gefunden (oder das Token darf das Repo nicht lesen)"
            )
        if antwort.status_code >= 400:
            raise QuellenFehler(f"GitHub antwortet mit HTTP {antwort.status_code} auf {pfad}")
        try:
            return antwort.json()
        except ValueError as fehler:
            raise QuellenFehler(f"GitHub liefert kein gültiges JSON für {pfad}") from fehler

    async def repos(self) -> list[str]:
        """Eigene Repos des Kontos, die das Token lesen darf (ohne archivierte)."""
        namen: list[str] = []
        for seite in range(1, MAX_SEITEN + 1):
            daten = await self.lesen(
                "user/repos", {"affiliation": "owner", "per_page": 100, "page": seite}
            )
            if not isinstance(daten, list):
                raise QuellenFehler("GitHub: user/repos liefert keine Liste")
            for repo in daten:
                if not isinstance(repo, dict) or repo.get("archived"):
                    continue
                besitzer = str((repo.get("owner") or {}).get("login", ""))
                if self.konto and besitzer.casefold() != self.konto.casefold():
                    continue
                if repo.get("full_name"):
                    namen.append(str(repo["full_name"]))
            if len(daten) < 100:
                break
        return sorted(namen, key=str.casefold)

    async def fehlgeschlagene_builds(self, repo: str) -> list[dict]:
        daten = await self.lesen(
            f"repos/{repo_pruefen(repo)}/actions/runs", {"status": "failure", "per_page": 10}
        )
        laeufe = daten.get("workflow_runs") if isinstance(daten, dict) else None
        return [r for r in laeufe or [] if isinstance(r, dict)]

    async def releases(self, repo: str) -> list[dict]:
        daten = await self.lesen(f"repos/{repo_pruefen(repo)}/releases", {"per_page": 5})
        return [r for r in daten or [] if isinstance(r, dict) and not r.get("draft")]

    async def issues(self, repo: str) -> list[dict]:
        """Offene Issues, neueste zuerst – ohne Pull Requests (die liefert GitHub mit)."""
        daten = await self.lesen(
            f"repos/{repo_pruefen(repo)}/issues",
            {"state": "open", "sort": "created", "direction": "desc", "per_page": 20},
        )
        return [i for i in daten or [] if isinstance(i, dict) and "pull_request" not in i]
