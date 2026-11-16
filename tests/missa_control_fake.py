"""Nachbau von Missa Control (Panel auf ai01) für Tests: MCP-Gateway unter ``/mcp``.

Antwortet wie das echte Gateway auf JSON-RPC ``tools/list`` und ``tools/call`` – wahlweise
als JSON oder als Server-Sent Events – und verlangt ``Authorization: Bearer <Token>``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

TOKEN = "panel-token"
URL = "http://panel.test:8090"

Ergebnis = dict[str, Any] | str | Callable[[dict[str, Any]], dict[str, Any] | str]


def funktion(
    name: str, beschreibung: str = "", nur_lesen: bool = True, **eigenschaften: Any
) -> dict[str, Any]:
    """Eine Funktion so, wie sie in ``tools/list`` steht."""
    return {
        "name": name,
        "description": beschreibung or f"Panel-Funktion {name}",
        "inputSchema": {"type": "object", "properties": eigenschaften},
        "annotations": {"readOnlyHint": nur_lesen},
    }


def text_ergebnis(text: str, fehler: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": fehler}


class FakePanel:
    """Ersetzt Missa Control. ``ergebnisse`` legt fest, was ein Aufruf liefert."""

    def __init__(self) -> None:
        self.token = TOKEN
        self.funktionen: list[dict[str, Any]] = []
        self.ergebnisse: dict[str, Ergebnis] = {}
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []
        self.listen_abrufe = 0
        self.erreichbar = True
        self.sse = False
        self.warten = 0.0
        #: Wird bei der nächsten Anfrage ausgelöst (z. B. ``httpx.ReadTimeout``).
        self.ausfall: Exception | None = None
        self.kopfzeilen: list[httpx.Headers] = []
        self.transport = httpx.MockTransport(self._bearbeiten)

    async def _bearbeiten(self, anfrage: httpx.Request) -> httpx.Response:
        if not self.erreichbar:
            raise httpx.ConnectError("Verbindung abgelehnt", request=anfrage)
        if self.ausfall is not None:
            raise self.ausfall
        self.kopfzeilen.append(anfrage.headers)
        if anfrage.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(
                401, json={"fehler": "Kein gültiger Client-Token", "hinweis": "Clients"}
            )
        if anfrage.url.path != "/mcp":
            return httpx.Response(404, json={"fehler": "nicht gefunden"})
        nachricht = json.loads(anfrage.content)
        methode = nachricht.get("method")
        parameter = nachricht.get("params") or {}
        if methode == "tools/list":
            self.listen_abrufe += 1
            ergebnis: dict[str, Any] = {"tools": list(self.funktionen)}
        elif methode == "tools/call":
            ergebnis = await self._aufruf(parameter.get("name", ""), parameter.get("arguments"))
        else:
            return self._antwort(
                {
                    "jsonrpc": "2.0",
                    "id": nachricht.get("id"),
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
        return self._antwort({"jsonrpc": "2.0", "id": nachricht.get("id"), "result": ergebnis})

    async def _aufruf(self, name: str, argumente: dict[str, Any] | None) -> dict[str, Any]:
        argumente = argumente or {}
        if self.warten:
            await asyncio.sleep(self.warten)
        if name not in {f["name"] for f in self.funktionen}:
            return text_ergebnis(
                f"Die Funktion „{name}“ gibt es nicht oder sie ist für Missa nicht "
                "freigegeben. Die aktuell verfügbaren Funktionen liefert tools/list.",
                fehler=True,
            )
        self.aufrufe.append((name, argumente))
        ergebnis = self.ergebnisse.get(name, f"{name} erledigt")
        if callable(ergebnis):
            ergebnis = ergebnis(argumente)
        return text_ergebnis(ergebnis) if isinstance(ergebnis, str) else ergebnis

    def _antwort(self, daten: dict[str, Any]) -> httpx.Response:
        if self.sse:
            text = f"event: message\ndata: {json.dumps(daten, ensure_ascii=False)}\n\n"
            return httpx.Response(
                200, content=text.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=daten)
