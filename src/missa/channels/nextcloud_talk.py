"""Nextcloud Talk als Kanal für Missa (offizielle Webhook-Bot-Schnittstelle).

Ablauf einer Nachricht:

1. Talk schickt jede Nachricht als POST (ActivityStreams-JSON) an
   ``/channels/nextcloud-talk/webhook``.
2. Signatur prüfen (HMAC-SHA256 über Random + Body) und Backend abgleichen –
   sonst 401.
3. **Zugriffsprüfung:** Nur ``users/<id>`` aus ``NC_TALK_ALLOWED_USERS``.
   Alle anderen werden still ignoriert (nur 200 und ein Log-Eintrag ohne Inhalt).
4. Nur Text-Nachrichten (``Create`` / ``message``) werden verarbeitet.
5. Sofort 200 an Talk (Talk wartet nur 5 Sekunden); im Hintergrund: Reaktion 👀,
   Missa-Pipeline, Antwort als ``replyTo``, Reaktion wieder entfernen.

Doku der Schnittstelle: https://nextcloud-talk.readthedocs.io/en/latest/bots/
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from missa.core.chat import ChatDienst
from missa.core.config import Einstellungen
from missa.core.llama import LlamaFehler

log = logging.getLogger(__name__)

KANAL = "nextcloud-talk"
WEBHOOK_PFAD = "/channels/nextcloud-talk/webhook"
API_PFAD = "/ocs/v2.php/apps/spreed/api/v1/bot"
#: Höchstlänge einer Talk-Nachricht (Zeichen).
MAX_LAENGE = 32_000
#: Größere Webhook-Anfragen werden gar nicht erst gelesen.
MAX_BODY = 1_000_000
ARBEITET = "👀"

FEHLER_ZEITLIMIT = (
    "Puh, das hat zu lange gedauert – nach {sekunden} Sekunden habe ich aufgegeben. "
    "Magst du es nochmal versuchen, vielleicht etwas kürzer gefragt?"
)
FEHLER_MODELL = (
    "Entschuldige, ich komme gerade nicht an mein Sprachmodell heran. "
    "Versuch es bitte in ein paar Minuten nochmal."
)
FEHLER_ALLGEMEIN = (
    "Entschuldige, da ist bei mir gerade etwas schiefgelaufen. Versuch es bitte nochmal."
)


class TalkFehler(Exception):
    """Nextcloud hat eine Anfrage des Bots abgelehnt oder war nicht erreichbar."""


# ---------------------------------------------------------------------- Hilfen


def signatur(secret: str, random: str, inhalt: bytes | str) -> str:
    """HMAC-SHA256 über ``random + inhalt`` als Hex – so signieren Talk und der Bot."""
    if isinstance(inhalt, str):
        inhalt = inhalt.encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"), random.encode("utf-8") + inhalt, hashlib.sha256
    ).hexdigest()


def url_normalisieren(url: str) -> str:
    """Für den Vergleich: Schema und Host klein, Standard-Port und ``/`` am Ende weg."""
    teile = urlsplit(url.strip())
    schema = teile.scheme.lower()
    host = (teile.hostname or "").lower()
    port = teile.port
    if port and not ((schema == "https" and port == 443) or (schema == "http" and port == 80)):
        host = f"{host}:{port}"
    return f"{schema}://{host}{teile.path.rstrip('/')}"


def text_aus_nachricht(inhalt: str) -> str:
    """Macht aus ``object.content`` (JSON mit ``message`` und ``parameters``) lesbaren Text."""
    try:
        daten = json.loads(inhalt)
    except (TypeError, ValueError):
        return str(inhalt or "")
    if not isinstance(daten, dict):
        return str(daten)
    text = str(daten.get("message", ""))
    parameter = daten.get("parameters") or {}
    if isinstance(parameter, dict):
        for schluessel, wert in parameter.items():
            if not isinstance(wert, dict):
                continue
            name = str(wert.get("name", schluessel))
            ersatz = f"@{name}" if str(wert.get("type", "")) in ("user", "call", "guest") else name
            text = text.replace("{" + str(schluessel) + "}", ersatz)
    return text


def aufteilen(text: str, grenze: int = MAX_LAENGE) -> list[str]:
    """Teilt lange Antworten in Stücke ≤ ``grenze`` – möglichst an Absätzen und Zeilen."""
    if len(text) <= grenze:
        return [text]
    markierung = "\n\n*(Teil 99/99)*"
    platz = grenze - len(markierung)
    teile: list[str] = []
    rest = text
    while len(rest) > platz:
        schnitt = -1
        for trenner in ("\n\n", "\n", " "):
            schnitt = rest.rfind(trenner, platz // 2, platz)
            if schnitt > 0:
                break
        if schnitt <= 0:
            schnitt = platz
        teile.append(rest[:schnitt].rstrip())
        rest = rest[schnitt:].lstrip()
    if rest:
        teile.append(rest)
    anzahl = len(teile)
    return [f"{teil}\n\n*(Teil {nummer}/{anzahl})*" for nummer, teil in enumerate(teile, 1)]


# ---------------------------------------------------------------------- Bot-API von Nextcloud


class TalkClient:
    """Schickt Nachrichten und Reaktionen über die Bot-API von Nextcloud Talk."""

    def __init__(
        self,
        api_url: str,
        secret: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret = secret
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/") + API_PFAD,
            timeout=timeout,
            transport=transport,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )

    def _kopf(self, signiert: str) -> dict[str, str]:
        random = secrets.token_hex(32)
        return {
            "X-Nextcloud-Talk-Bot-Random": random,
            "X-Nextcloud-Talk-Bot-Signature": signatur(self._secret, random, signiert),
        }

    async def _senden(self, methode: str, pfad: str, signiert: str, daten: dict[str, Any]) -> int:
        try:
            antwort = await self._client.request(
                methode, pfad, json=daten, headers=self._kopf(signiert)
            )
        except httpx.HTTPError as fehler:
            raise TalkFehler(f"Nextcloud nicht erreichbar: {fehler}") from fehler
        if antwort.status_code >= 400:
            raise TalkFehler(f"Nextcloud antwortete mit {antwort.status_code} auf {methode} {pfad}")
        return antwort.status_code

    async def nachricht_senden(
        self, token: str, text: str, antwort_auf: int | None = None, leise: bool = False
    ) -> None:
        daten: dict[str, Any] = {
            "message": text,
            "referenceId": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        }
        if antwort_auf is not None:
            daten["replyTo"] = antwort_auf
        if leise:
            daten["silent"] = True
        await self._senden("POST", f"/{token}/message", text, daten)

    async def reaktion_setzen(self, token: str, nachricht_id: int | str, emoji: str) -> None:
        await self._senden("POST", f"/{token}/reaction/{nachricht_id}", emoji, {"reaction": emoji})

    async def reaktion_entfernen(self, token: str, nachricht_id: int | str, emoji: str) -> None:
        await self._senden(
            "DELETE", f"/{token}/reaction/{nachricht_id}", emoji, {"reaction": emoji}
        )

    async def schliessen(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------- Kanal


class TalkKanal:
    """Nimmt Webhooks von Talk entgegen und beantwortet erlaubte Nachrichten."""

    def __init__(
        self, einstellungen: Einstellungen, dienst: ChatDienst, client: TalkClient
    ) -> None:
        self.einstellungen = einstellungen
        self.dienst = dienst
        self.client = client
        self.erlaubt = frozenset(einstellungen.nc_talk_allowed_users)
        self._backend = url_normalisieren(einstellungen.nc_talk_base_url)
        self._gesehen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._sperren: dict[str, asyncio.Lock] = {}

    # -------------------------------------------------------------- Prüfungen

    def signatur_gueltig(self, random: str, signatur_kopf: str, body: bytes) -> bool:
        if len(random) < 32 or not signatur_kopf:
            return False
        erwartet = signatur(self.einstellungen.nc_talk_bot_secret, random, body)
        return hmac.compare_digest(erwartet, signatur_kopf.strip().lower())

    def backend_gueltig(self, backend: str) -> bool:
        return bool(backend) and hmac.compare_digest(url_normalisieren(backend), self._backend)

    def absender_erlaubt(self, akteur: Any) -> bool:
        """Nur echte Nextcloud-Benutzer (``users/<id>``) aus der Erlaubnisliste – exakt."""
        if not isinstance(akteur, dict) or akteur.get("type") != "Person":
            return False
        kennung = str(akteur.get("id", ""))
        if not kennung.startswith("users/"):
            return False
        return kennung.removeprefix("users/") in self.erlaubt

    def _schon_gesehen(self, token: str, nachricht_id: str) -> bool:
        schluessel = (token, nachricht_id)
        if schluessel in self._gesehen:
            return True
        self._gesehen[schluessel] = None
        while len(self._gesehen) > 1000:
            self._gesehen.popitem(last=False)
        return False

    # -------------------------------------------------------------- Webhook

    async def webhook(self, request: Request, hintergrund: BackgroundTasks) -> JSONResponse:
        groesse = request.headers.get("content-length", "")
        if groesse.isdigit() and int(groesse) > MAX_BODY:
            return JSONResponse({"status": "zu gross"}, status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"status": "zu gross"}, status_code=413)

        if not self.signatur_gueltig(
            request.headers.get("x-nextcloud-talk-random", ""),
            request.headers.get("x-nextcloud-talk-signature", ""),
            body,
        ):
            log.warning("Talk: Webhook mit ungültiger Signatur abgelehnt (%s)", _client_ip(request))
            return JSONResponse({"status": "ungültige Signatur"}, status_code=401)
        backend = request.headers.get("x-nextcloud-talk-backend", "")
        if not self.backend_gueltig(backend):
            log.warning("Talk: Webhook von unbekanntem Backend %r abgelehnt", backend[:200])
            return JSONResponse({"status": "unbekanntes Backend"}, status_code=401)

        try:
            ereignis = json.loads(body)
        except ValueError:
            log.warning("Talk: Webhook ohne gültiges JSON")
            return JSONResponse({"status": "kein JSON"}, status_code=400)
        if not isinstance(ereignis, dict):
            return JSONResponse({"status": "kein JSON-Objekt"}, status_code=400)

        akteur = ereignis.get("actor") or {}
        ziel = ereignis.get("target") or ereignis.get("object") or {}
        token = str(ziel.get("id", "")) if isinstance(ziel, dict) else ""
        absender = str(akteur.get("id", "?")) if isinstance(akteur, dict) else "?"

        # Zugriffsprüfung als Erstes: Nicht erlaubte Absender werden still ignoriert.
        if not self.absender_erlaubt(akteur):
            log.info(
                "Talk: ignoriert – Absender %s ist nicht erlaubt (Unterhaltung %s, %s, Typ %s)",
                absender,
                token or "?",
                datetime.now(self.einstellungen.zone).isoformat(timespec="seconds"),
                str(ereignis.get("type", "?"))[:20],
            )
            return JSONResponse({"status": "ok"})

        objekt = ereignis.get("object") or {}
        if (
            ereignis.get("type") != "Create"
            or not isinstance(objekt, dict)
            or objekt.get("type") != "Note"
            or objekt.get("name") != "message"
            or not token
        ):
            log.debug("Talk: Ereignis %s von %s übersprungen", ereignis.get("type"), absender)
            return JSONResponse({"status": "ok"})

        nachricht_id = str(objekt.get("id", ""))
        text = text_aus_nachricht(objekt.get("content", "")).strip()
        if not text or self._schon_gesehen(token, nachricht_id):
            return JSONResponse({"status": "ok"})

        log.info(
            "Talk: Nachricht von %s in Unterhaltung %s angenommen (%d Zeichen)",
            absender,
            token,
            len(text),
        )
        hintergrund.add_task(
            self.verarbeiten,
            token,
            nachricht_id,
            text,
            absender.removeprefix("users/"),
            str(akteur.get("name", "")),
            str(ziel.get("name", "")),
        )
        return JSONResponse({"status": "angenommen"})

    # -------------------------------------------------------------- Verarbeitung

    async def verarbeiten(
        self,
        token: str,
        nachricht_id: str,
        text: str,
        benutzer: str,
        anzeigename: str,
        unterhaltung: str,
    ) -> None:
        """Läuft im Hintergrund: 👀, Missa fragen, antworten, 👀 entfernen."""
        beginn = time.monotonic()
        antwort_auf = int(nachricht_id) if nachricht_id.isdigit() else None
        reagiert = await self._reaktion(token, nachricht_id, setzen=True)
        teile = 0
        try:
            sperre = self._sperren.setdefault(token, asyncio.Lock())
            async with sperre:
                try:
                    antwort = await asyncio.wait_for(
                        self._missa_fragen(token, text, benutzer, anzeigename, unterhaltung),
                        timeout=self.einstellungen.nc_talk_timeout,
                    )
                except TimeoutError:
                    log.warning(
                        "Talk: Zeitlimit (%.0f s) für %s überschritten",
                        self.einstellungen.nc_talk_timeout,
                        token,
                    )
                    antwort = FEHLER_ZEITLIMIT.format(
                        sekunden=f"{self.einstellungen.nc_talk_timeout:g}"
                    )
                except LlamaFehler as fehler:
                    log.error("Talk: llama-server-Fehler: %s", fehler)
                    antwort = FEHLER_MODELL
                except Exception:
                    log.exception("Talk: Fehler bei der Verarbeitung")
                    antwort = FEHLER_ALLGEMEIN
                for teil in aufteilen(antwort):
                    await self.client.nachricht_senden(token, teil, antwort_auf=antwort_auf)
                    teile += 1
        except TalkFehler as fehler:
            log.error("Talk: Antwort konnte nicht gesendet werden: %s", fehler)
        finally:
            if reagiert:
                await self._reaktion(token, nachricht_id, setzen=False)
            log.info(
                "Talk: Antwort an users/%s in %s fertig nach %.1f s (%d Nachricht(en))",
                benutzer,
                token,
                time.monotonic() - beginn,
                teile,
            )

    async def _missa_fragen(
        self, token: str, text: str, benutzer: str, anzeigename: str, unterhaltung: str
    ) -> str:
        hinweis = (
            f"Kanal: Nextcloud Talk (Unterhaltung „{unterhaltung or token}“). "
            f"Die Nachricht ist von {anzeigename or benutzer} (Nextcloud-Benutzer „{benutzer}“). "
            "Talk zeigt Markdown an."
        )
        anfrage = {
            "messages": [
                {"role": "system", "content": hinweis},
                {"role": "user", "content": text},
            ]
        }
        vorbereitet = self.dienst.vorbereiten(anfrage, sitzung=f"{KANAL}:{token}")
        ergebnis = await self.dienst.antworten(vorbereitet)
        return str(ergebnis["choices"][0]["message"]["content"])

    async def _reaktion(self, token: str, nachricht_id: str, setzen: bool) -> bool:
        if not nachricht_id:
            return False
        try:
            if setzen:
                await self.client.reaktion_setzen(token, nachricht_id, ARBEITET)
            else:
                await self.client.reaktion_entfernen(token, nachricht_id, ARBEITET)
        except TalkFehler as fehler:
            log.warning(
                "Talk: Reaktion %s konnte nicht %s: %s",
                ARBEITET,
                "gesetzt werden" if setzen else "entfernt werden",
                fehler,
            )
            return False
        return True


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------------- Einrichten


def talk_einrichten(
    app: FastAPI,
    einstellungen: Einstellungen,
    dienst: ChatDienst,
    client: TalkClient | None,
) -> TalkKanal | None:
    """Registriert den Webhook – nur wenn Talk eingeschaltet und vollständig konfiguriert ist."""
    if not einstellungen.nc_talk_enabled:
        log.info("Nextcloud Talk ist aus (NC_TALK_ENABLED)")
        return None
    if not einstellungen.talk_aktiv or client is None:
        log.warning(
            "NC_TALK_ENABLED ist an, aber NC_TALK_BASE_URL oder NC_TALK_BOT_SECRET fehlt – "
            "Nextcloud Talk bleibt aus"
        )
        return None
    if len(einstellungen.nc_talk_bot_secret) < 40:
        log.warning("NC_TALK_BOT_SECRET ist kürzer als 40 Zeichen – Talk verlangt 40 bis 128")
    if not einstellungen.nc_talk_allowed_users:
        log.warning(
            "NC_TALK_ALLOWED_USERS ist leer – Missa ignoriert alle Nachrichten aus Nextcloud Talk"
        )

    kanal = TalkKanal(einstellungen, dienst, client)

    @app.post(WEBHOOK_PFAD, tags=["Kanäle"], include_in_schema=False)
    async def talk_webhook(request: Request, hintergrund: BackgroundTasks) -> JSONResponse:
        return await kanal.webhook(request, hintergrund)

    log.info(
        "Nextcloud Talk ist an: Webhook %s, erlaubte Benutzer: %s",
        WEBHOOK_PFAD,
        ", ".join(einstellungen.nc_talk_allowed_users) or "keine",
    )
    return kanal
