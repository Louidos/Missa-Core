"""Gesprächslogik: Systemprompt, Verlauf, Werkzeug-Schleife, Weiterleitung an llama-server."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from missa.core.config import Einstellungen
from missa.core.llama import ANFRAGE_ZU_GROSS, LlamaClient, LlamaFehler, unerwartet
from missa.core.prompt import charakter_lesen, systemprompt_bauen
from missa.core.verlauf import Verlauf
from missa.core.werkzeuge import Werkzeugkasten

log = logging.getLogger(__name__)

MODELL = "missa"
#: Höchstzahl der Runden, in denen das Modell Werkzeuge aufrufen darf.
MAX_RUNDEN = 5
#: So viele gespeicherte Nachrichten einer Sitzung werden mitgeschickt.
VERLAUF_LAENGE = 20
#: Diese Felder einer Client-Anfrage gehen unverändert an llama-server.
DURCHGEREICHT = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
)
#: Beim gekürzten zweiten Versuch wird jedes Werkzeug-Ergebnis auf so viele Zeichen gekürzt.
KURZ_WERKZEUG_ERGEBNIS = 1500
#: Feste Codes für Fehler, die nicht vom llama-server kommen.
INTERNER_FEHLER = "interner_fehler"
INTERNER_FEHLER_TEXT = (
    "Bei der Bearbeitung ist in Missa-Core ein unerwarteter Fehler passiert. Bitte nochmal "
    "versuchen; bleibt es so, steht der genaue Fehler im Log von missa-core "
    "(journalctl -u missa-core) unter der Anfrage-ID."
)
KEIN_ERGEBNIS = (
    "Entschuldige, ich bin mit meinen Werkzeugen gerade nicht zu einem Ergebnis gekommen."
)

#: Liefert zu einer Nutzernachricht einen zusätzlichen Block für den Systemprompt.
KontextQuelle = Callable[[str], str | None]


class AnfrageFehler(ValueError):
    """Die Anfrage des Clients ist ungültig (HTTP 400)."""


def text_aus_inhalt(inhalt: Any) -> str:
    """Macht aus ``content`` (Text oder Liste von Teilen) einen reinen Text."""
    if inhalt is None:
        return ""
    if isinstance(inhalt, str):
        return inhalt
    if isinstance(inhalt, list):
        teile = []
        for teil in inhalt:
            if isinstance(teil, dict) and teil.get("type") == "text":
                teile.append(str(teil.get("text", "")))
            elif isinstance(teil, dict):
                teile.append(f"[{teil.get('type', 'Anhang')}]")
            else:
                teile.append(str(teil))
        return "\n".join(t for t in teile if t)
    return str(inhalt)


@dataclass
class Vorbereitet:
    """Eine vorbereitete Anfrage, bereit für die Werkzeug-Schleife."""

    nachrichten: list[dict[str, Any]]
    parameter: dict[str, Any]
    sitzung: str | None
    frage: str
    anfrage_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: Text der neuen Nachricht von Louis – nur, wenn die Anfrage mit einer
    #: user-Nachricht endet (nie Text des Modells oder eines Werkzeugs).
    neue_nachricht: str | None = None
    #: Woher die Anfrage kommt (``api``, ``talk``, ``hintergrund``) – fürs Aktionsprotokoll.
    kanal: str = "api"
    #: Wer schreibt (Talk-Benutzer, ``user`` der API oder leer).
    account: str = ""
    #: ``True``, sobald Verlauf und Werkzeug-Ergebnisse einmal gekürzt wurden, weil
    #: llama-server die Anfrage als zu gross abgelehnt hat (höchstens einmal je Anfrage).
    gekuerzt: bool = False


#: Läuft vor dem Modell mit der neuen Nachricht von Louis; ein zurückgegebener Text
#: kommt als Hinweis in den Systemprompt (z. B. „Mail wurde gesendet“).
EingangsPruefer = Callable[[Vorbereitet], Awaitable[str | None]]
#: Läuft nach der fertigen Antwort; ein zurückgegebener Text wird unten angehängt
#: (z. B. die genaue Anzeige eines E-Mail-Entwurfs).
Nachbearbeiter = Callable[[Vorbereitet], str | None]


def kuerzen(vorbereitet: Vorbereitet) -> bool:
    """Kürzt Verlauf und Werkzeug-Ergebnisse, damit die Anfrage ins Modell passt.

    Bleibt: der Systemprompt, die letzte Nachricht von Louis und alles danach (die laufende
    Werkzeug-Schleife). Weg fällt der ältere Verlauf; Werkzeug-Ergebnisse werden auf
    ``KURZ_WERKZEUG_ERGEBNIS`` Zeichen gekürzt (mit Hinweis). ``True``, wenn sich etwas
    geändert hat.
    """
    nachrichten = vorbereitet.nachrichten
    letzte_frage = next(
        (i for i in range(len(nachrichten) - 1, 0, -1) if nachrichten[i].get("role") == "user"),
        None,
    )
    geaendert = False
    if letzte_frage is not None and letzte_frage > 1:
        nachrichten[1:letzte_frage] = []
        geaendert = True
    for nachricht in nachrichten:
        inhalt = nachricht.get("content")
        if (
            nachricht.get("role") == "tool"
            and isinstance(inhalt, str)
            and len(inhalt) > KURZ_WERKZEUG_ERGEBNIS
        ):
            nachricht["content"] = (
                inhalt[:KURZ_WERKZEUG_ERGEBNIS]
                + f"\n[… gekürzt, {len(inhalt) - KURZ_WERKZEUG_ERGEBNIS} Zeichen ausgelassen]"
            )
            geaendert = True
    vorbereitet.gekuerzt = True
    return geaendert


def _nutzung_addieren(summe: dict[str, int], neu: dict[str, Any] | None) -> None:
    for schluessel in ("prompt_tokens", "completion_tokens", "total_tokens"):
        wert = (neu or {}).get(schluessel)
        if isinstance(wert, int):
            summe[schluessel] = summe.get(schluessel, 0) + wert


def fehler_ereignis(nachricht: str, typ: str, code: str, anfrage_id: str) -> str:
    """Ein Fehler als Server-Sent Event im OpenAI-Format (mit Code und Anfrage-ID)."""
    daten = {"error": {"message": nachricht, "type": typ, "code": code, "anfrage_id": anfrage_id}}
    return f"data: {json.dumps(daten, ensure_ascii=False)}\n\n"


class ChatDienst:
    """Beantwortet Chat-Anfragen als Missa."""

    def __init__(
        self,
        einstellungen: Einstellungen,
        llama: LlamaClient,
        werkzeuge: Werkzeugkasten,
        verlauf: Verlauf,
        uhr: Callable[[], datetime] | None = None,
        kontext_quellen: Iterable[KontextQuelle] = (),
        eingangs_pruefer: Iterable[EingangsPruefer] = (),
        nachbearbeiter: Iterable[Nachbearbeiter] = (),
    ) -> None:
        self.einstellungen = einstellungen
        self.llama = llama
        self.werkzeuge = werkzeuge
        self.verlauf = verlauf
        self._uhr = uhr or (lambda: datetime.now(einstellungen.zone))
        self.kontext_quellen = list(kontext_quellen)
        self.eingangs_pruefer = list(eingangs_pruefer)
        self.nachbearbeiter = list(nachbearbeiter)

    # ------------------------------------------------------------------ Vorbereitung

    def systemprompt(self, frage: str, client_system: str | None = None) -> str:
        zusatz = []
        for quelle in self.kontext_quellen:
            try:
                block = quelle(frage)
            except Exception:
                log.exception("Kontextquelle fehlgeschlagen")
                continue
            if block:
                zusatz.append(block)
        return systemprompt_bauen(
            charakter=charakter_lesen(self.einstellungen.charakter_datei),
            jetzt=self._uhr(),
            werkzeuge=self.werkzeuge.beschreibung_fuer_prompt(),
            zusatz=zusatz,
            client_system=client_system,
        )

    def vorbereiten(self, anfrage: dict[str, Any], sitzung: str | None) -> Vorbereitet:
        """Baut aus der Client-Anfrage die Nachrichten für llama-server.

        - Ganz vorne steht immer Missas Systemprompt; system-Nachrichten des
          Clients werden hinten angehängt und ersetzen den Charakter nie.
        - Mit Sitzung und genau einer neuen Nachricht wird der gespeicherte
          Verlauf (letzte 20 Nachrichten) davor eingefügt. Schickt die App
          ihren Verlauf selbst mit, wird nichts doppelt eingefügt.
        """
        nachrichten = anfrage.get("messages") or []
        if not isinstance(nachrichten, list) or not nachrichten:
            raise AnfrageFehler("messages darf nicht leer sein")

        client_system: list[str] = []
        gespraech: list[dict[str, Any]] = []
        for nachricht in nachrichten:
            if not isinstance(nachricht, dict) or "role" not in nachricht:
                raise AnfrageFehler("Jede Nachricht braucht ein Feld role")
            if nachricht["role"] in ("system", "developer"):
                client_system.append(text_aus_inhalt(nachricht.get("content")))
            else:
                gespraech.append(dict(nachricht))
        if not gespraech:
            raise AnfrageFehler("Es fehlt eine Nachricht von role user")

        frage = next(
            (text_aus_inhalt(n.get("content")) for n in reversed(gespraech) if n["role"] == "user"),
            "",
        )

        frueher: list[dict[str, Any]] = []
        if sitzung and len(gespraech) == 1:
            frueher = self.verlauf.laden(sitzung, VERLAUF_LAENGE)

        system = self.systemprompt(frage, "\n\n".join(t for t in client_system if t) or None)
        parameter = {k: anfrage[k] for k in DURCHGEREICHT if anfrage.get(k) is not None}
        letzte = gespraech[-1]
        return Vorbereitet(
            nachrichten=[{"role": "system", "content": system}, *frueher, *gespraech],
            parameter=parameter,
            sitzung=sitzung,
            frage=frage,
            neue_nachricht=text_aus_inhalt(letzte.get("content"))
            if letzte.get("role") == "user"
            else None,
        )

    async def _eingang(self, vorbereitet: Vorbereitet) -> None:
        """Lässt die Eingangsprüfer laufen und hängt ihre Hinweise an den Systemprompt."""
        hinweise = []
        for pruefer in self.eingangs_pruefer:
            try:
                if hinweis := await pruefer(vorbereitet):
                    hinweise.append(hinweis)
            except Exception:
                log.exception("Eingangsprüfer fehlgeschlagen")
        if hinweise:
            vorbereitet.nachrichten[0]["content"] += "\n\n## Gerade passiert\n" + "\n".join(
                hinweise
            )

    def _nachgang(self, vorbereitet: Vorbereitet) -> str:
        zusaetze = []
        for bearbeiter in self.nachbearbeiter:
            try:
                if zusatz := bearbeiter(vorbereitet):
                    zusaetze.append(zusatz.strip())
            except Exception:
                log.exception("Nachbearbeiter fehlgeschlagen")
        return "\n\n".join(zusaetze)

    def _nutzlast(self, vorbereitet: Vorbereitet, mit_werkzeugen: bool) -> dict[str, Any]:
        nutzlast: dict[str, Any] = {
            **vorbereitet.parameter,
            "model": MODELL,
            "messages": vorbereitet.nachrichten,
        }
        if mit_werkzeugen and len(self.werkzeuge):
            nutzlast["tools"] = self.werkzeuge.schemas()
        return nutzlast

    def _melden(self, vorbereitet: Vorbereitet, fehler: LlamaFehler) -> LlamaFehler:
        """Schreibt den Fehler mit Anfrage-ID und Originaltext ins Log."""
        fehler.anfrage_id = vorbereitet.anfrage_id
        log.error(
            "Anfrage %s: llama-server-Fehler %s (%s) – Original: %s",
            vorbereitet.anfrage_id,
            fehler.code,
            fehler,
            fehler.original or "-",
        )
        return fehler

    def _nochmal_gekuerzt(self, vorbereitet: Vorbereitet, fehler: LlamaFehler) -> bool:
        """``True``, wenn ein gekürzter zweiter Versuch sinnvoll ist (und gekürzt wurde)."""
        if fehler.code != ANFRAGE_ZU_GROSS or vorbereitet.gekuerzt:
            return False
        if not kuerzen(vorbereitet):
            return False
        log.warning(
            "Anfrage %s: llama-server lehnt die Anfrage als zu gross ab (%s) – "
            "versuche es einmal mit gekürztem Verlauf und gekürzten Werkzeug-Ergebnissen",
            vorbereitet.anfrage_id,
            fehler.original or fehler,
        )
        return True

    async def _llama_chat(self, vorbereitet: Vorbereitet, mit_werkzeugen: bool) -> dict[str, Any]:
        """Eine Runde ohne Streaming; bei „zu gross“ einmal gekürzt wiederholen."""
        try:
            return await self.llama.chat(self._nutzlast(vorbereitet, mit_werkzeugen))
        except LlamaFehler as fehler:
            if not self._nochmal_gekuerzt(vorbereitet, fehler):
                raise self._melden(vorbereitet, fehler) from fehler
        try:
            return await self.llama.chat(self._nutzlast(vorbereitet, mit_werkzeugen))
        except LlamaFehler as fehler:
            raise self._melden(vorbereitet, fehler) from fehler

    async def _llama_stream(
        self, vorbereitet: Vorbereitet, mit_werkzeugen: bool
    ) -> AsyncIterator[dict[str, Any]]:
        """Eine Runde mit Streaming; bei „zu gross“ (vor dem ersten Stück) einmal gekürzt."""
        erhalten = False
        try:
            async for stueck in self.llama.chat_stream(self._nutzlast(vorbereitet, mit_werkzeugen)):
                erhalten = True
                yield stueck
            return
        except LlamaFehler as fehler:
            if erhalten or not self._nochmal_gekuerzt(vorbereitet, fehler):
                raise self._melden(vorbereitet, fehler) from fehler
        try:
            async for stueck in self.llama.chat_stream(self._nutzlast(vorbereitet, mit_werkzeugen)):
                yield stueck
        except LlamaFehler as fehler:
            raise self._melden(vorbereitet, fehler) from fehler

    async def _werkzeuge_ausfuehren(
        self, vorbereitet: Vorbereitet, inhalt: str, aufrufe: list[dict[str, Any]]
    ) -> None:
        vorbereitet.nachrichten.append(
            {"role": "assistant", "content": inhalt, "tool_calls": aufrufe}
        )
        for nummer, aufruf in enumerate(aufrufe):
            funktion = aufruf.get("function") or {}
            name = str(funktion.get("name", ""))
            log.info("Werkzeug-Aufruf: %s(%s)", name, funktion.get("arguments"))
            ergebnis = await self.werkzeuge.ausfuehren(
                name,
                funktion.get("arguments"),
                vorbereitet.anfrage_id,
                nachricht=vorbereitet.neue_nachricht,
                kanal=vorbereitet.kanal,
                account=vorbereitet.account,
            )
            vorbereitet.nachrichten.append(
                {
                    "role": "tool",
                    "tool_call_id": aufruf.get("id") or f"aufruf-{nummer}",
                    "name": name,
                    "content": ergebnis,
                }
            )

    def _merken(self, vorbereitet: Vorbereitet, antwort: str) -> None:
        if vorbereitet.sitzung and vorbereitet.frage and antwort:
            self.verlauf.austausch_speichern(vorbereitet.sitzung, vorbereitet.frage, antwort)

    async def formulieren(self, auftrag: str, ersatz: str) -> str:
        """Lässt Missa eine Nachricht in ihrem Charakter formulieren (ohne Werkzeuge).

        Für Nachrichten, die Missa von sich aus schickt. Klappt es nicht, kommt ``ersatz``.
        """
        system = systemprompt_bauen(
            charakter=charakter_lesen(self.einstellungen.charakter_datei),
            jetzt=self._uhr(),
            werkzeuge="",
        )
        nutzlast = {
            "model": MODELL,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "(Interner Auftrag des Systems, nicht von Louis.) "
                    f"{auftrag}\nAntworte nur mit der fertigen Nachricht an Louis.",
                },
            ],
            "max_tokens": 400,
        }
        try:
            antwort = await self.llama.chat(nutzlast)
            text = text_aus_inhalt(antwort["choices"][0]["message"].get("content")).strip()
        except (LlamaFehler, KeyError, IndexError, TypeError) as fehler:
            log.warning("Formulieren fehlgeschlagen, nutze Ersatztext: %s", fehler)
            return ersatz
        return text or ersatz

    async def hintergrund_ausfuehren(self, auftrag: str) -> str:
        """Lässt Missa einen Auftrag mit Werkzeugen erledigen, ohne dass Louis dabei ist.

        Für Routinen und die Nachtschicht. Die Anweisung ist **nicht** Louis' neue
        Nachricht: Eingangsprüfer (z. B. die Senden-Sperre für Mails) sehen nichts,
        und es wird kein Verlauf gespeichert.
        """
        vorbereitet = self.vorbereiten({"messages": [{"role": "user", "content": auftrag}]}, None)
        vorbereitet.neue_nachricht = None
        vorbereitet.kanal = "hintergrund"
        vorbereitet.account = "missa"
        antwort = await self.antworten(vorbereitet)
        return text_aus_inhalt(antwort["choices"][0]["message"].get("content"))

    # ------------------------------------------------------------------ ohne Streaming

    async def antworten(self, vorbereitet: Vorbereitet) -> dict[str, Any]:
        """Beantwortet die Anfrage vollständig und liefert eine OpenAI-Antwort."""
        await self._eingang(vorbereitet)
        nutzung: dict[str, int] = {}
        text = ""
        for runde in range(MAX_RUNDEN + 1):
            mit_werkzeugen = runde < MAX_RUNDEN
            antwort = await self._llama_chat(vorbereitet, mit_werkzeugen)
            _nutzung_addieren(nutzung, antwort.get("usage"))
            try:
                nachricht = antwort["choices"][0]["message"]
                if not isinstance(nachricht, dict):
                    raise TypeError("message ist kein Objekt")
            except (KeyError, IndexError, TypeError) as fehler:
                original = json.dumps(antwort, ensure_ascii=False)[:2000]
                raise self._melden(vorbereitet, unerwartet(original)) from fehler
            text = text_aus_inhalt(nachricht.get("content"))
            aufrufe = nachricht.get("tool_calls") or []
            if not aufrufe or not mit_werkzeugen:
                break
            await self._werkzeuge_ausfuehren(vorbereitet, text, aufrufe)
        text = text.strip() or KEIN_ERGEBNIS
        if zusatz := self._nachgang(vorbereitet):
            text = f"{text}\n\n{zusatz}"

        self._merken(vorbereitet, text)
        return {
            "id": f"chatcmpl-{vorbereitet.anfrage_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODELL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": nutzung or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # ------------------------------------------------------------------ mit Streaming

    async def antworten_stream(self, vorbereitet: Vorbereitet) -> AsyncIterator[str]:
        """Beantwortet die Anfrage als Server-Sent Events im OpenAI-Format.

        Text des Modells wird sofort weitergegeben. Werkzeug-Aufrufe werden
        gesammelt, ausgeführt, und danach läuft die nächste Runde im selben
        Stream weiter.
        """
        kennung = f"chatcmpl-{vorbereitet.anfrage_id}"
        erstellt = int(time.time())

        def ereignis(delta: dict[str, Any], ende: str | None = None) -> str:
            stueck = {
                "id": kennung,
                "object": "chat.completion.chunk",
                "created": erstellt,
                "model": MODELL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": ende}],
            }
            return f"data: {json.dumps(stueck, ensure_ascii=False)}\n\n"

        yield ereignis({"role": "assistant", "content": ""})
        await self._eingang(vorbereitet)
        gesamt = ""
        try:
            for runde in range(MAX_RUNDEN + 1):
                mit_werkzeugen = runde < MAX_RUNDEN
                runden_text = ""
                aufrufe: dict[int, dict[str, Any]] = {}
                async for stueck in self._llama_stream(vorbereitet, mit_werkzeugen):
                    auswahl = stueck.get("choices") or []
                    if not auswahl:
                        continue
                    delta = auswahl[0].get("delta") or {}
                    inhalt = delta.get("content")
                    if inhalt:
                        if not runden_text and gesamt and not gesamt[-1].isspace():
                            inhalt = "\n\n" + inhalt.lstrip()
                        runden_text += inhalt
                        gesamt += inhalt
                        yield ereignis({"content": inhalt})
                    for teil in delta.get("tool_calls") or []:
                        eintrag = aufrufe.setdefault(
                            int(teil.get("index", 0)),
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if teil.get("id"):
                            eintrag["id"] = teil["id"]
                        funktion = teil.get("function") or {}
                        if funktion.get("name"):
                            eintrag["function"]["name"] = funktion["name"]
                        if funktion.get("arguments"):
                            eintrag["function"]["arguments"] += funktion["arguments"]
                if not aufrufe or not mit_werkzeugen:
                    break
                await self._werkzeuge_ausfuehren(
                    vorbereitet, runden_text, [aufrufe[i] for i in sorted(aufrufe)]
                )
        except LlamaFehler as fehler:
            yield fehler_ereignis(str(fehler), "llama_fehler", fehler.code, vorbereitet.anfrage_id)
            yield "data: [DONE]\n\n"
            return
        except Exception:
            log.exception("Anfrage %s: unerwarteter Fehler beim Streaming", vorbereitet.anfrage_id)
            yield fehler_ereignis(
                INTERNER_FEHLER_TEXT, "server_error", INTERNER_FEHLER, vorbereitet.anfrage_id
            )
            yield "data: [DONE]\n\n"
            return

        if not gesamt.strip():
            gesamt = KEIN_ERGEBNIS
            yield ereignis({"content": gesamt})
        if zusatz := self._nachgang(vorbereitet):
            zusatz = f"\n\n{zusatz}"
            gesamt += zusatz
            yield ereignis({"content": zusatz})
        # Vor dem Abschluss speichern, falls der Client direkt danach trennt.
        self._merken(vorbereitet, gesamt.strip())
        yield ereignis({}, ende="stop")
        yield "data: [DONE]\n\n"
