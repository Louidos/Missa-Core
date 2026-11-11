"""Werkzeuge (Tools), die das Modell über das OpenAI-tools-Format aufrufen kann."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from missa.core.rechte import Rechte

log = logging.getLogger(__name__)

#: ID der laufenden Chat-Anfrage. Werkzeuge können damit erkennen, ob zwei
#: Aufrufe aus derselben Anfrage stammen (z. B. für Bestätigungen).
aktuelle_anfrage: ContextVar[str] = ContextVar("aktuelle_anfrage", default="")
#: ``True``, solange Missa einen Auftrag im Hintergrund erledigt (Routine,
#: Nachtschicht). Louis ist dann nicht im Gespräch: Rückfragen können nicht
#: bestätigt werden, und Werkzeuge dürfen nichts Zerstörerisches tun.
im_hintergrund: ContextVar[bool] = ContextVar("im_hintergrund", default=False)
#: ``True``, während ein zerstörerisches Werkzeug läuft, das Louis über die zentrale
#: Rechte-Schicht ausdrücklich bestätigt hat (siehe :mod:`missa.core.rechte`).
zentral_freigegeben: ContextVar[bool] = ContextVar("zentral_freigegeben", default=False)


class Stufe(StrEnum):
    """Wie viel ein Werkzeug darf. Jedes Werkzeug braucht genau eine Stufe.

    - ``lesen``: schaut nur nach, ändert nichts.
    - ``schreiben``: legt an oder ändert (rückgängig machbar oder harmlos).
    - ``zerstoererisch``: löscht, versendet oder ist sonst nicht zurückzunehmen –
      läuft nur nach Louis' ausdrücklichem Ja zu genau dieser Aktion.
    """

    LESEN = "lesen"
    SCHREIBEN = "schreiben"
    ZERSTOERERISCH = "zerstoererisch"


class WerkzeugFehler(Exception):
    """Erwarteter Fehler eines Werkzeugs; der Text geht so an das Modell."""


@dataclass(frozen=True)
class Werkzeug:
    """Ein Werkzeug mit Name, Beschreibung, JSON-Schema der Parameter, Funktion und Stufe.

    ``vorschau`` (optional) beschreibt für die Rückfrage, was ein zerstörerisches
    Werkzeug mit diesen Parametern tun würde – ohne etwas zu tun.
    """

    name: str
    beschreibung: str
    parameter: dict[str, Any]
    funktion: Callable[..., Any]
    stufe: Stufe | None = None
    vorschau: Callable[..., Any] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.beschreibung,
                "parameters": self.parameter,
            },
        }


class Werkzeugkasten:
    """Sammlung aller Werkzeuge, die Missa gerade zur Verfügung stehen."""

    def __init__(self, werkzeuge: Iterable[Werkzeug] = (), rechte: Rechte | None = None) -> None:
        from missa.core.rechte import Rechte

        #: Die zentrale Rechte-Schicht: Stufen, Bestätigung, Protokoll, Notfall-Stopp.
        self.rechte: Rechte = rechte if rechte is not None else Rechte()
        self._werkzeuge: dict[str, Werkzeug] = {}
        for werkzeug in werkzeuge:
            self.hinzufuegen(werkzeug)

    def hinzufuegen(self, werkzeug: Werkzeug) -> None:
        if not isinstance(werkzeug.stufe, Stufe):
            raise ValueError(
                f"Werkzeug {werkzeug.name} hat keine Stufe (lesen, schreiben oder "
                "zerstoererisch) – ohne Stufe wird es nicht registriert"
            )
        if werkzeug.name in self._werkzeuge:
            raise ValueError(f"Werkzeug {werkzeug.name} ist schon vorhanden")
        self._werkzeuge[werkzeug.name] = werkzeug

    def get(self, name: str) -> Werkzeug | None:
        return self._werkzeuge.get(name)

    def __len__(self) -> int:
        return len(self._werkzeuge)

    def __iter__(self) -> Iterator[Werkzeug]:
        return iter(self._werkzeuge.values())

    def __contains__(self, name: object) -> bool:
        return name in self._werkzeuge

    def schemas(self) -> list[dict[str, Any]]:
        return [werkzeug.schema() for werkzeug in self]

    def beschreibung_fuer_prompt(self) -> str:
        return "\n".join(f"- {w.name}: {w.beschreibung}" for w in self)

    async def ausfuehren(
        self,
        name: str,
        argumente: str | dict[str, Any] | None,
        anfrage_id: str = "",
        *,
        nachricht: str | None = None,
        kanal: str = "intern",
        account: str = "",
    ) -> str:
        """Führt ein Werkzeug über die Rechte-Schicht aus. Fehler kommen als Text zurück.

        ``nachricht`` ist Louis' neue Nachricht dieser Anfrage (nur sie kann eine
        zerstörerische Aktion bestätigen), ``kanal`` und ``account`` landen im
        Aktionsprotokoll. Jeder Aufruf wird protokolliert – auch abgelehnte.
        """
        if im_hintergrund.get() and kanal == "intern":
            kanal = "hintergrund"
        werkzeug = self._werkzeuge.get(name)

        def protokoll(status: str, ergebnis: str, parameter: Any, bestaetigt: bool = False):
            self.rechte.protokollieren(
                werkzeug=name,
                stufe=str(werkzeug.stufe) if werkzeug is not None else "unbekannt",
                parameter=parameter,
                bestaetigt=bestaetigt,
                status=status,
                ergebnis=ergebnis,
                kanal=kanal,
                account=account,
            )

        if werkzeug is None:
            verfuegbar = ", ".join(self._werkzeuge) or "keine"
            protokoll("ungueltig", "Werkzeug gibt es nicht", argumente)
            return f"Fehler: Das Werkzeug „{name}“ gibt es nicht. Verfügbar: {verfuegbar}."

        if isinstance(argumente, str):
            try:
                argumente = json.loads(argumente) if argumente.strip() else {}
            except ValueError:
                protokoll("ungueltig", "Parameter sind kein gültiges JSON", argumente)
                return f"Fehler: Die Parameter für {name} sind kein gültiges JSON."
        argumente = argumente or {}
        if not isinstance(argumente, dict):
            protokoll("ungueltig", "Parameter sind kein JSON-Objekt", argumente)
            return f"Fehler: Die Parameter für {name} müssen ein JSON-Objekt sein."

        signatur = inspect.signature(werkzeug.funktion)
        try:
            signatur.bind(**argumente)
        except TypeError as fehler:
            protokoll("ungueltig", f"Falsche Parameter: {fehler}", argumente)
            return f"Fehler: Falsche Parameter für {name}: {fehler}."

        if werkzeug.stufe is not Stufe.LESEN and self.rechte.notfall_aktiv():
            protokoll("gesperrt_notfall", "Notfall-Stopp aktiv", argumente)
            return self.rechte.notfall_text(name)

        bestaetigt = False
        if werkzeug.stufe is Stufe.ZERSTOERERISCH:
            if im_hintergrund.get():
                protokoll("abgelehnt", "im Hintergrund nie zerstörerisch", argumente)
                return (
                    f"NICHT AUSGEFÜHRT: {name} ist zerstörerisch und braucht Louis' Ja – "
                    "im Hintergrund (Routine, Nachtschicht) geht das nie. Schlag es ihm "
                    "im nächsten Gespräch vor."
                )
            if self.rechte.freigegeben(name, argumente, anfrage_id, nachricht):
                bestaetigt = True
                if "bestaetigt" in signatur.parameters:
                    argumente = {**argumente, "bestaetigt": True}
            else:
                try:
                    beschreibung = await self._vorschau(werkzeug, argumente, anfrage_id)
                except WerkzeugFehler as fehler:
                    protokoll("fehlgeschlagen", str(fehler), argumente)
                    return f"Fehler: {fehler}"
                except Exception as fehler:
                    log.exception("Vorschau für %s ist fehlgeschlagen", name)
                    protokoll("fehlgeschlagen", f"Vorschau: {fehler}", argumente)
                    return f"Fehler beim Prüfen von {name}: {fehler or type(fehler).__name__}"
                self.rechte.vormerken(name, argumente, anfrage_id)
                protokoll("wartet_auf_bestaetigung", beschreibung, argumente)
                return self.rechte.rueckfrage_text(name, beschreibung)

        marke = aktuelle_anfrage.set(anfrage_id)
        freigabe = zentral_freigegeben.set(bestaetigt)
        try:
            ergebnis = await self._aufrufen(werkzeug, argumente)
        except WerkzeugFehler as fehler:
            protokoll("fehlgeschlagen", str(fehler), argumente, bestaetigt)
            return f"Fehler: {fehler}"
        except Exception as fehler:
            log.exception("Werkzeug %s ist fehlgeschlagen", name)
            protokoll("fehlgeschlagen", str(fehler) or type(fehler).__name__, argumente, bestaetigt)
            return f"Fehler beim Ausführen von {name}: {fehler or type(fehler).__name__}"
        finally:
            zentral_freigegeben.reset(freigabe)
            aktuelle_anfrage.reset(marke)

        if not isinstance(ergebnis, str):
            ergebnis = json.dumps(ergebnis, ensure_ascii=False, default=str)
        protokoll("ausgefuehrt", ergebnis, argumente, bestaetigt)
        return ergebnis

    @staticmethod
    async def _aufrufen(funktion_oder_werkzeug: Werkzeug | Callable[..., Any], argumente) -> Any:
        funktion = (
            funktion_oder_werkzeug.funktion
            if isinstance(funktion_oder_werkzeug, Werkzeug)
            else funktion_oder_werkzeug
        )
        if inspect.iscoroutinefunction(funktion):
            return await funktion(**argumente)
        return await asyncio.to_thread(funktion, **argumente)

    async def _vorschau(
        self, werkzeug: Werkzeug, argumente: dict[str, Any], anfrage_id: str
    ) -> str:
        """Beschreibt, was ein zerstörerisches Werkzeug tun würde – ohne es zu tun.

        Reihenfolge: eigene ``vorschau``; sonst – wenn das Werkzeug selbst einen
        Parameter ``bestaetigt`` hat – ein Aufruf mit ``bestaetigt=False`` (dann
        meldet es nur, was passieren würde); sonst eine allgemeine Beschreibung.
        """
        marke = aktuelle_anfrage.set(anfrage_id)
        try:
            if werkzeug.vorschau is not None:
                text = await self._aufrufen(werkzeug.vorschau, argumente)
            elif "bestaetigt" in inspect.signature(werkzeug.funktion).parameters:
                text = await self._aufrufen(werkzeug, {**argumente, "bestaetigt": False})
            else:
                angaben = ", ".join(f"{k}={v!r}" for k, v in argumente.items()) or "ohne Angaben"
                text = f"{werkzeug.name} ({angaben}): {werkzeug.beschreibung}"
        finally:
            aktuelle_anfrage.reset(marke)
        return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
