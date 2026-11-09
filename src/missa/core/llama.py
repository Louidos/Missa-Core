"""Client für llama-server (OpenAI-kompatible Schnittstelle)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LlamaFehler(Exception):
    """llama-server ist nicht erreichbar oder hat mit einem Fehler geantwortet."""

    def __init__(self, nachricht: str, status: int = 502) -> None:
        super().__init__(nachricht)
        self.status = status


class LlamaClient:
    """Spricht mit llama-server über ``/v1/chat/completions``."""

    def __init__(
        self,
        basis_url: str,
        api_key_datei: Path | None = None,
        timeout: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key_datei = api_key_datei
        self._client = httpx.AsyncClient(
            base_url=basis_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    def _kopfzeilen(self) -> dict[str, str]:
        """Liest den API-Key von llama-server bei jedem Aufruf neu (falls vorhanden)."""
        if self._api_key_datei is None:
            return {}
        try:
            schluessel = self._api_key_datei.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {}
        except OSError as fehler:
            log.warning(
                "API-Key für llama-server nicht lesbar (%s): %s", self._api_key_datei, fehler
            )
            return {}
        return {"Authorization": f"Bearer {schluessel}"} if schluessel else {}

    async def chat(self, nutzlast: dict[str, Any]) -> dict[str, Any]:
        """Eine Anfrage ohne Streaming; gibt die JSON-Antwort zurück."""
        try:
            antwort = await self._client.post(
                "/v1/chat/completions",
                json={**nutzlast, "stream": False},
                headers=self._kopfzeilen(),
            )
        except httpx.TimeoutException as fehler:
            raise LlamaFehler("llama-server hat nicht rechtzeitig geantwortet", 504) from fehler
        except httpx.HTTPError as fehler:
            raise LlamaFehler(f"llama-server ist nicht erreichbar: {fehler}") from fehler
        if antwort.status_code >= 400:
            raise LlamaFehler(
                f"llama-server antwortete mit {antwort.status_code}: {antwort.text[:500]}"
            )
        try:
            return antwort.json()
        except ValueError as fehler:
            raise LlamaFehler("llama-server lieferte kein gültiges JSON") from fehler

    async def chat_stream(self, nutzlast: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Eine Anfrage mit Streaming; liefert die einzelnen Chunks als dict."""
        try:
            async with self._client.stream(
                "POST",
                "/v1/chat/completions",
                json={**nutzlast, "stream": True},
                headers=self._kopfzeilen(),
            ) as antwort:
                if antwort.status_code >= 400:
                    text = (await antwort.aread()).decode("utf-8", "replace")
                    raise LlamaFehler(
                        f"llama-server antwortete mit {antwort.status_code}: {text[:500]}"
                    )
                async for zeile in antwort.aiter_lines():
                    zeile = zeile.strip()
                    if not zeile.startswith("data:"):
                        continue
                    daten = zeile.removeprefix("data:").strip()
                    if daten == "[DONE]":
                        break
                    try:
                        yield json.loads(daten)
                    except ValueError:
                        log.warning("Ungültiger Stream-Chunk von llama-server: %.200s", daten)
        except httpx.TimeoutException as fehler:
            raise LlamaFehler("llama-server hat nicht rechtzeitig geantwortet", 504) from fehler
        except httpx.HTTPError as fehler:
            raise LlamaFehler(f"llama-server ist nicht erreichbar: {fehler}") from fehler

    async def gesund(self) -> bool:
        """``True``, wenn llama-server auf ``/health`` mit 200 antwortet."""
        try:
            antwort = await self._client.get("/health", headers=self._kopfzeilen(), timeout=5.0)
        except httpx.HTTPError:
            return False
        return antwort.status_code == 200

    async def schliessen(self) -> None:
        await self._client.aclose()
