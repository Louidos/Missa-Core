"""FastAPI-Anwendung von Missa-Core (OpenAI-kompatible API)."""

# Hinweis: kein ``from __future__ import annotations`` – FastAPI muss die
# Annotationen der lokal definierten Abhängigkeiten (Depends) auswerten können.

import logging
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from missa import __version__
from missa.core.chat import MODELL, AnfrageFehler, ChatDienst
from missa.core.config import Einstellungen
from missa.core.llama import LlamaClient, LlamaFehler
from missa.core.verlauf import Verlauf
from missa.core.werkzeuge import Werkzeugkasten

log = logging.getLogger(__name__)

MAX_SITZUNG_LAENGE = 200


class Nachricht(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None


class ChatAnfrage(BaseModel):
    """Anfrage im OpenAI-Format. Unbekannte Felder werden toleriert."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[Nachricht] = Field(min_length=1)
    stream: bool = False
    user: str | None = None


def _fehler(status: int, nachricht: str, typ: str, code: str | None = None, **kopf: str):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": nachricht, "type": typ, "code": code}},
        headers=kopf or None,
    )


def create_app(
    einstellungen: Einstellungen | None = None,
    *,
    werkzeuge: Werkzeugkasten | None = None,
    llama_transport: httpx.AsyncBaseTransport | None = None,
    uhr: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Baut die App. Parameter außer ``einstellungen`` sind vor allem für Tests da."""
    einstellungen = einstellungen or Einstellungen.aus_umgebung()
    if werkzeuge is None:
        from missa.tools import werkzeuge_laden

        werkzeuge = werkzeuge_laden(einstellungen)

    llama = LlamaClient(
        einstellungen.llama_url,
        einstellungen.llama_api_key_file,
        einstellungen.llama_timeout,
        transport=llama_transport,
    )
    verlauf = Verlauf(einstellungen.datenbank)
    dienst = ChatDienst(einstellungen, llama, werkzeuge, verlauf, uhr=uhr)
    gestartet = int(time.time())

    @asynccontextmanager
    async def lebenszyklus(_app: FastAPI):
        log.info(
            "Missa-Core %s bereit – llama-server: %s, Werkzeuge: %s",
            __version__,
            einstellungen.llama_url,
            ", ".join(w.name for w in werkzeuge) or "keine",
        )
        yield
        await llama.schliessen()

    app = FastAPI(
        title="Missa-Core",
        version=__version__,
        description="OpenAI-kompatible API von Missa",
        lifespan=lebenszyklus,
    )
    app.state.einstellungen = einstellungen
    app.state.dienst = dienst

    # -------------------------------------------------------------- Fehlerformat

    @app.exception_handler(HTTPException)
    async def http_fehler(_request: Request, fehler: HTTPException):
        typ = "authentication_error" if fehler.status_code == 401 else "invalid_request_error"
        return _fehler(fehler.status_code, str(fehler.detail), typ, None, **(fehler.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def eingabe_fehler(_request: Request, fehler: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(str(t) for t in e.get('loc', ()))}: {e.get('msg')}" for e in fehler.errors()
        )
        return _fehler(400, f"Ungültige Anfrage: {details}", "invalid_request_error")

    @app.exception_handler(AnfrageFehler)
    async def anfrage_fehler(_request: Request, fehler: AnfrageFehler):
        return _fehler(400, str(fehler), "invalid_request_error")

    @app.exception_handler(LlamaFehler)
    async def llama_fehler(_request: Request, fehler: LlamaFehler):
        log.error("llama-server: %s", fehler)
        return _fehler(fehler.status, str(fehler), "llama_fehler")

    # -------------------------------------------------------------- Anmeldung

    bearer = HTTPBearer(auto_error=False)

    def anmeldung(
        zugang: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        erwartet = einstellungen.api_key
        if (
            zugang is None
            or not erwartet
            or not secrets.compare_digest(zugang.credentials.encode(), erwartet.encode())
        ):
            raise HTTPException(
                status_code=401,
                detail="Fehlender oder ungültiger API-Schlüssel (Authorization: Bearer …)",
                headers={"WWW-Authenticate": "Bearer"},
            )

    geschuetzt = [Depends(anmeldung)]

    # -------------------------------------------------------------- Endpunkte

    @app.get("/health", tags=["Betrieb"])
    async def health() -> JSONResponse:
        """Ohne Anmeldung. 200, wenn llama-server antwortet, sonst 503."""
        llama_ok = await llama.gesund()
        charakter_ok = einstellungen.charakter_datei.is_file()
        return JSONResponse(
            status_code=200 if llama_ok else 503,
            content={
                "status": "ok" if llama_ok else "gestoert",
                "version": __version__,
                "llama": "ok" if llama_ok else "nicht erreichbar",
                "charakter": "ok" if charakter_ok else "fehlt",
                "werkzeuge": [w.name for w in werkzeuge],
            },
        )

    def modell_info() -> dict[str, Any]:
        return {"id": MODELL, "object": "model", "created": gestartet, "owned_by": "missa"}

    @app.get("/v1/models", dependencies=geschuetzt, tags=["OpenAI"])
    async def modelle() -> dict[str, Any]:
        return {"object": "list", "data": [modell_info()]}

    @app.get("/v1/models/{modell}", dependencies=geschuetzt, tags=["OpenAI"])
    async def modell(modell: str) -> dict[str, Any]:
        if modell != MODELL:
            raise HTTPException(status_code=404, detail=f"Unbekanntes Modell: {modell}")
        return modell_info()

    @app.post("/v1/chat/completions", dependencies=geschuetzt, tags=["OpenAI"])
    async def chat_completions(
        anfrage: ChatAnfrage,
        x_missa_session: Annotated[str | None, Header()] = None,
    ):
        sitzung = (x_missa_session or anfrage.user or "").strip() or None
        if sitzung and len(sitzung) > MAX_SITZUNG_LAENGE:
            raise AnfrageFehler(f"Sitzungs-ID ist länger als {MAX_SITZUNG_LAENGE} Zeichen")

        vorbereitet = dienst.vorbereiten(anfrage.model_dump(exclude_none=True), sitzung)
        if anfrage.stream:
            return StreamingResponse(
                dienst.antworten_stream(vorbereitet),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return await dienst.antworten(vorbereitet)

    return app
