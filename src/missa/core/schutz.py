"""Schutz vor dem eigentlichen Programm: Anmeldung, Grössengrenze, Fehlversuche.

Läuft als reine ASGI-Middleware *vor* FastAPI. Damit gilt:

- Ohne gültigen Bearer-Token wird der Inhalt einer Anfrage gar nicht erst gelesen
  (ausser für ``/health`` und den Talk-Webhook, der seine eigene Signatur prüft).
- Kein Body ist grösser als ``max_bytes`` – auch nicht ohne ``Content-Length``
  (chunked), der Rest wird dann nicht mehr gelesen.
- Zu viele Fehlversuche von derselben Adresse sperren sie für eine Weile (429).
- Die Endpunkte der Geräte (``geraete_pfade``) verlangen statt des API-Schlüssels einen
  gültigen Geräte-Token (``geraet_pruefen``); Fehlversuche zählen genauso.

Die Prüfung in den Endpunkten (``Depends(anmeldung)``) bleibt zusätzlich bestehen.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)

MAX_ANFRAGE_BYTES = 20_000_000
MAX_FEHLVERSUCHE = 10
FEHLVERSUCH_FENSTER = 300.0
SPERRE_SEKUNDEN = 900.0
MAX_ADRESSEN = 10_000

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _wiedergabe(vorrat: list[dict[str, Any]], receive: Receive) -> Receive:
    """Liefert zuerst die schon gelesenen Nachrichten, danach wieder die echten."""

    async def empfangen() -> dict[str, Any]:
        if vorrat:
            return vorrat.pop(0)
        return await receive()

    return empfangen


async def _antwort(send: Send, status: int, nachricht: str, typ: str, code: str, **kopf: str):
    body = json.dumps(
        {"error": {"message": nachricht, "type": typ, "code": code}}, ensure_ascii=False
    ).encode()
    kopfzeilen = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    kopfzeilen += [(k.lower().encode(), v.encode()) for k, v in kopf.items()]
    await send({"type": "http.response.start", "status": status, "headers": kopfzeilen})
    await send({"type": "http.response.body", "body": body})


class Schutz:
    """ASGI-Middleware: Anmeldung vor dem Lesen, Grössengrenze, Sperre nach Fehlversuchen."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key: str,
        offen: Iterable[str] = ("/health",),
        max_bytes: int = MAX_ANFRAGE_BYTES,
        uhr: Callable[[], float] = time.monotonic,
        geraete_pfade: Iterable[str] = (),
        geraet_pruefen: Callable[[bytes | None], bool] | None = None,
    ) -> None:
        self.app = app
        self.api_key = api_key
        self.offen = frozenset(offen)
        self.geraete_pfade = frozenset(geraete_pfade) if geraet_pruefen is not None else frozenset()
        self.geraet_pruefen = geraet_pruefen
        self.max_bytes = max_bytes
        self.uhr = uhr
        self._fehlversuche: dict[str, deque[float]] = {}
        self._gesperrt_bis: dict[str, float] = {}

    # -------------------------------------------------------------- Anmeldung

    def _token_gueltig(self, scope: Scope) -> bool:
        if not self.api_key:
            return False
        for name, wert in scope.get("headers") or ():
            if name == b"authorization":
                art, _, token = wert.partition(b" ")
                if art.lower() != b"bearer":
                    return False
                return secrets.compare_digest(token.strip(), self.api_key.encode())
        return False

    def _geraet_gueltig(self, scope: Scope) -> bool:
        from missa.core.geraete import bearer_token

        if self.geraet_pruefen is None:
            return False
        for name, wert in scope.get("headers") or ():
            if name == b"authorization":
                try:
                    return self.geraet_pruefen(bearer_token(wert))
                except Exception:
                    log.exception("Geräte-Token konnte nicht geprüft werden")
                    return False
        return False

    def _gesperrt(self, adresse: str) -> bool:
        bis = self._gesperrt_bis.get(adresse)
        if bis is None:
            return False
        if self.uhr() >= bis:
            del self._gesperrt_bis[adresse]
            return False
        return True

    def _fehlversuch(self, adresse: str, pfad: str) -> None:
        jetzt = self.uhr()
        liste = self._fehlversuche.setdefault(adresse, deque())
        liste.append(jetzt)
        while liste and liste[0] < jetzt - FEHLVERSUCH_FENSTER:
            liste.popleft()
        log.warning("Anmeldung abgelehnt: %s %s (%d in 5 min)", adresse, pfad[:100], len(liste))
        if len(liste) >= MAX_FEHLVERSUCHE:
            self._gesperrt_bis[adresse] = jetzt + SPERRE_SEKUNDEN
            self._fehlversuche.pop(adresse, None)
            log.warning("Adresse %s nach %d Fehlversuchen gesperrt", adresse, MAX_FEHLVERSUCHE)
        if len(self._fehlversuche) > MAX_ADRESSEN:
            self._fehlversuche.pop(next(iter(self._fehlversuche)))

    # -------------------------------------------------------------- ASGI

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pfad = scope.get("path", "")
        adresse = (scope.get("client") or ("?", 0))[0]

        if pfad not in self.offen:
            if self._gesperrt(adresse):
                await _antwort(
                    send,
                    429,
                    "Zu viele Fehlversuche – bitte später noch einmal versuchen.",
                    "authentication_error",
                    "zu_viele_fehlversuche",
                    **{"Retry-After": str(int(SPERRE_SEKUNDEN))},
                )
                return
            geraet = pfad in self.geraete_pfade
            if not (self._geraet_gueltig(scope) if geraet else self._token_gueltig(scope)):
                self._fehlversuch(adresse, pfad)
                await _antwort(
                    send,
                    401,
                    "Fehlender, ungültiger oder gesperrter Geräte-Token (Authorization: Bearer …)"
                    if geraet
                    else "Fehlender oder ungültiger API-Schlüssel (Authorization: Bearer …)",
                    "authentication_error",
                    "nicht_angemeldet",
                    **{"WWW-Authenticate": "Bearer"},
                )
                return

        for name, wert in scope.get("headers") or ():
            if name == b"content-length" and (not wert.isdigit() or int(wert) > self.max_bytes):
                await self._zu_gross(send)
                return

        # Body vollständig (aber höchstens max_bytes) lesen, dann an die App weitergeben.
        teile: list[bytes] = []
        gelesen = 0
        while True:
            nachricht = await receive()
            if nachricht["type"] != "http.request":
                await self.app(scope, _wiedergabe([nachricht], receive), send)
                return
            stueck = nachricht.get("body", b"")
            gelesen += len(stueck)
            if gelesen > self.max_bytes:
                await self._zu_gross(send)
                return
            teile.append(stueck)
            if not nachricht.get("more_body", False):
                break
        gesamt = {"type": "http.request", "body": b"".join(teile), "more_body": False}
        await self.app(scope, _wiedergabe([gesamt], receive), send)

    async def _zu_gross(self, send: Send) -> None:
        await _antwort(
            send,
            413,
            f"Anfrage ist zu gross (höchstens {self.max_bytes // 1_000_000} MB).",
            "invalid_request_error",
            "zu_gross",
        )
