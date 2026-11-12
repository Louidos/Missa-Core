"""Eigener Git-Server (Gitea oder Forgejo, z. B. git.louis-degiorgi.ch) über seine API.

Missa erkennt selbst, ob Gitea oder Forgejo läuft: Forgejo beantwortet zusätzlich
``/api/forgejo/v1/version``, Gitea nur ``/api/v1/version``. Der Zugang ist ein Token aus
der .env (``GIT_SERVER_TOKEN``). Lesen geht immer; das einzige Schreiben ist
:meth:`GitServer.issue_anlegen`.
"""

from __future__ import annotations

import re
import ssl
from pathlib import Path
from typing import Any

import httpx

from missa.integrationen.monitoring import QuellenFehler

TIMEOUT = 15.0
#: Gültige Namen für Besitzer und Repos (wie Gitea sie erlaubt).
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
#: Höchstens so viele Seiten à 50 Einträge werden gelesen.
MAX_SEITEN = 10


def name_pruefen(name: object, was: str = "Repo") -> str:
    text = str(name or "").strip()
    if not NAME.match(text) or text in (".", ".."):
        raise QuellenFehler(f"„{name}“ ist kein gültiger Name für ein {was}.")
    return text


class GitServer:
    """Gitea/Forgejo-API (``/api/v1``) mit einem Token."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        ca_datei: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/").removesuffix("/api/v1")
        self._kopf = {"Authorization": f"token {token}", "Accept": "application/json"}
        self.transport = transport
        self.timeout = timeout
        self.verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(ca_datei)) if ca_datei is not None else True
        )
        self.art = ""
        self.version = ""

    async def anfrage(
        self,
        methode: str,
        pfad: str,
        *,
        params: dict[str, Any] | None = None,
        daten: dict[str, Any] | None = None,
        fehlt_ok: bool = False,
    ) -> Any:
        adresse = f"{self.url}/{pfad.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout, verify=self.verify, trust_env=False
            ) as client:
                antwort = await client.request(
                    methode, adresse, params=params, json=daten, headers=self._kopf
                )
        except httpx.HTTPError as fehler:
            raise QuellenFehler(
                f"Der Git-Server ({self.url}) ist nicht erreichbar: "
                f"{fehler or type(fehler).__name__}"
            ) from fehler
        if antwort.status_code == 404 and fehlt_ok:
            return None
        if antwort.status_code in (401, 403):
            raise QuellenFehler(
                f"Der Git-Server lehnt das Token ab (HTTP {antwort.status_code}) – "
                "GIT_SERVER_TOKEN und seine Rechte prüfen"
            )
        if antwort.status_code == 404:
            raise QuellenFehler(f"Gibt es auf dem Git-Server nicht: {pfad}")
        if antwort.status_code >= 400:
            raise QuellenFehler(
                f"Der Git-Server antwortet mit HTTP {antwort.status_code} auf {pfad}"
            )
        if not antwort.content:
            return None
        try:
            return antwort.json()
        except ValueError as fehler:
            raise QuellenFehler(f"Der Git-Server liefert kein gültiges JSON für {pfad}") from fehler

    async def erkennen(self) -> str:
        """``Forgejo 9.0.3`` oder ``Gitea 1.22.6`` – sonst ein Fehler."""
        forgejo = await self.anfrage("GET", "api/forgejo/v1/version", fehlt_ok=True)
        if isinstance(forgejo, dict) and forgejo.get("version"):
            self.art, self.version = "Forgejo", str(forgejo["version"])
            return f"{self.art} {self.version}"
        gitea = await self.anfrage("GET", "api/v1/version", fehlt_ok=True)
        if not isinstance(gitea, dict) or not gitea.get("version"):
            raise QuellenFehler(f"Unter {self.url} finde ich weder Gitea noch Forgejo.")
        version = str(gitea["version"])
        # Ältere Forgejo-Versionen melden sich nur über /api/v1 („7.0.0+gitea-1.21.0“).
        self.art = "Forgejo" if "+gitea" in version or "forgejo" in version.casefold() else "Gitea"
        self.version = version
        return f"{self.art} {self.version}"

    async def _seiten(self, pfad: str, params: dict[str, Any] | None = None) -> list[dict]:
        alle: list[dict] = []
        for seite in range(1, MAX_SEITEN + 1):
            daten = await self.anfrage(
                "GET", pfad, params={**(params or {}), "limit": 50, "page": seite}
            )
            if not isinstance(daten, list):
                raise QuellenFehler(f"Der Git-Server liefert für {pfad} keine Liste")
            alle += [d for d in daten if isinstance(d, dict)]
            if len(daten) < 50:
                break
        return alle

    async def repos(self) -> list[dict]:
        """Alle Repos, auf die das Token Zugriff hat."""
        return await self._seiten("api/v1/user/repos")

    async def pulls(self, besitzer: str, repo: str) -> list[dict]:
        besitzer, repo = name_pruefen(besitzer, "Konto"), name_pruefen(repo)
        return await self._seiten(f"api/v1/repos/{besitzer}/{repo}/pulls", {"state": "open"})

    async def branches(self, besitzer: str, repo: str) -> list[dict]:
        besitzer, repo = name_pruefen(besitzer, "Konto"), name_pruefen(repo)
        return await self._seiten(f"api/v1/repos/{besitzer}/{repo}/branches")

    async def issue_anlegen(self, besitzer: str, repo: str, titel: str, text: str) -> dict:
        """Legt ein Issue an – das einzige, was Missa auf dem Git-Server schreibt."""
        besitzer, repo = name_pruefen(besitzer, "Konto"), name_pruefen(repo)
        daten = await self.anfrage(
            "POST",
            f"api/v1/repos/{besitzer}/{repo}/issues",
            daten={"title": titel, "body": text},
        )
        if not isinstance(daten, dict) or "number" not in daten:
            raise QuellenFehler("Der Git-Server hat das Issue nicht bestätigt.")
        return daten
