"""Gemeinsame Test-Hilfen: gefälschter llama-server, Einstellungen, Test-Client."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten

API_KEY = "test-schluessel"
JETZT = datetime(2026, 11, 9, 19, 45, tzinfo=ZoneInfo("Europe/Zurich"))

Antwort = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


def text(inhalt: str) -> dict[str, Any]:
    """Antwort von llama-server mit reinem Text."""
    return {"role": "assistant", "content": inhalt}


def werkzeug_aufruf(name: str, argumente: dict[str, Any], kennung: str = "call-1") -> dict:
    """Antwort von llama-server mit einem Werkzeug-Aufruf."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": kennung,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(argumente)},
            }
        ],
    }


class FakeLlama:
    """Ersetzt llama-server. Antworten werden der Reihe nach aus ``antworten`` genommen."""

    def __init__(self) -> None:
        self.antworten: list[Antwort] = []
        self.anfragen: list[dict[str, Any]] = []
        self.kopfzeilen: list[httpx.Headers] = []
        self.gesund = True
        self.erreichbar = True
        self.transport = httpx.MockTransport(self._bearbeiten)

    def _bearbeiten(self, request: httpx.Request) -> httpx.Response:
        if not self.erreichbar:
            raise httpx.ConnectError("Verbindung abgelehnt", request=request)
        if request.url.path == "/health":
            return httpx.Response(200 if self.gesund else 503, json={"status": "ok"})

        nutzlast = json.loads(request.content)
        self.anfragen.append(nutzlast)
        self.kopfzeilen.append(request.headers)
        antwort = self.antworten.pop(0) if self.antworten else text("Hallo, ich bin Missa.")
        if callable(antwort):
            antwort = antwort(nutzlast)
        if nutzlast.get("stream"):
            return httpx.Response(
                200,
                content=self._stream(antwort).encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": antwort, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    @staticmethod
    def _stream(antwort: dict[str, Any]) -> str:
        def chunk(delta: dict[str, Any], ende: str | None = None) -> str:
            daten = {"choices": [{"index": 0, "delta": delta, "finish_reason": ende}]}
            return f"data: {json.dumps(daten)}\n\n"

        teile = [chunk({"role": "assistant"})]
        inhalt = antwort.get("content") or ""
        mitte = len(inhalt) // 2
        for stueck in (inhalt[:mitte], inhalt[mitte:]):
            if stueck:
                teile.append(chunk({"content": stueck}))
        for index, aufruf in enumerate(antwort.get("tool_calls") or []):
            argumente = aufruf["function"]["arguments"]
            teile.append(
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": aufruf["id"],
                                "type": "function",
                                "function": {"name": aufruf["function"]["name"], "arguments": ""},
                            }
                        ]
                    }
                )
            )
            for stueck in (argumente[: len(argumente) // 2], argumente[len(argumente) // 2 :]):
                teile.append(
                    chunk({"tool_calls": [{"index": index, "function": {"arguments": stueck}}]})
                )
        teile.append(chunk({}, "tool_calls" if antwort.get("tool_calls") else "stop"))
        teile.append("data: [DONE]\n\n")
        return "".join(teile)

    def system(self, nummer: int = -1) -> str:
        """Systemprompt der Anfrage Nummer ``nummer``."""
        nachrichten = self.anfragen[nummer]["messages"]
        assert nachrichten[0]["role"] == "system"
        return nachrichten[0]["content"]


@pytest.fixture
def charakter_datei(tmp_path: Path) -> Path:
    datei = tmp_path / "charakter.md"
    datei.write_text("Du bist Missa. Du sagst gern 'Na klar!'.", encoding="utf-8")
    return datei


@pytest.fixture
def einstellungen(tmp_path: Path, charakter_datei: Path) -> Einstellungen:
    return Einstellungen(
        api_key=API_KEY,
        charakter_datei=charakter_datei,
        daten=tmp_path / "daten",
        llama_url="http://llama.test",
        llama_api_key_file=tmp_path / "llama.key",
        backup_ordner_einstellung=tmp_path / "backup",
    )


@pytest.fixture
def llama() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def werkzeuge() -> Werkzeugkasten:
    return Werkzeugkasten()


@pytest.fixture
def client(einstellungen: Einstellungen, llama: FakeLlama, werkzeuge: Werkzeugkasten):
    app = create_app(
        einstellungen, werkzeuge=werkzeuge, llama_transport=llama.transport, uhr=lambda: JETZT
    )
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {API_KEY}"
        yield test_client


def sse_lesen(antwort: httpx.Response) -> list[dict[str, Any] | str]:
    """Zerlegt eine SSE-Antwort in JSON-Objekte (und das Schlusswort ``[DONE]``)."""
    ereignisse: list[dict[str, Any] | str] = []
    for zeile in antwort.text.splitlines():
        if zeile.startswith("data: "):
            daten = zeile.removeprefix("data: ")
            ereignisse.append(daten if daten == "[DONE]" else json.loads(daten))
    return ereignisse
