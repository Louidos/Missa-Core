"""Werkzeuge (Tools), die das Modell über das OpenAI-tools-Format aufrufen kann."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: ID der laufenden Chat-Anfrage. Werkzeuge können damit erkennen, ob zwei
#: Aufrufe aus derselben Anfrage stammen (z. B. für Bestätigungen).
aktuelle_anfrage: ContextVar[str] = ContextVar("aktuelle_anfrage", default="")
#: ``True``, solange Missa einen Auftrag im Hintergrund erledigt (Routine,
#: Nachtschicht). Louis ist dann nicht im Gespräch: Rückfragen können nicht
#: bestätigt werden, und Werkzeuge dürfen nichts Zerstörerisches tun.
im_hintergrund: ContextVar[bool] = ContextVar("im_hintergrund", default=False)


class WerkzeugFehler(Exception):
    """Erwarteter Fehler eines Werkzeugs; der Text geht so an das Modell."""


@dataclass(frozen=True)
class Werkzeug:
    """Ein Werkzeug mit Name, Beschreibung, JSON-Schema der Parameter und Funktion."""

    name: str
    beschreibung: str
    parameter: dict[str, Any]
    funktion: Callable[..., Any]

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

    def __init__(self, werkzeuge: Iterable[Werkzeug] = ()) -> None:
        self._werkzeuge: dict[str, Werkzeug] = {}
        for werkzeug in werkzeuge:
            self.hinzufuegen(werkzeug)

    def hinzufuegen(self, werkzeug: Werkzeug) -> None:
        if werkzeug.name in self._werkzeuge:
            raise ValueError(f"Werkzeug {werkzeug.name} ist schon vorhanden")
        self._werkzeuge[werkzeug.name] = werkzeug

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
        self, name: str, argumente: str | dict[str, Any] | None, anfrage_id: str = ""
    ) -> str:
        """Führt ein Werkzeug aus. Fehler werden als Text zurückgegeben, nie geworfen."""
        werkzeug = self._werkzeuge.get(name)
        if werkzeug is None:
            verfuegbar = ", ".join(self._werkzeuge) or "keine"
            return f"Fehler: Das Werkzeug „{name}“ gibt es nicht. Verfügbar: {verfuegbar}."

        if isinstance(argumente, str):
            try:
                argumente = json.loads(argumente) if argumente.strip() else {}
            except ValueError:
                return f"Fehler: Die Parameter für {name} sind kein gültiges JSON."
        argumente = argumente or {}
        if not isinstance(argumente, dict):
            return f"Fehler: Die Parameter für {name} müssen ein JSON-Objekt sein."

        try:
            inspect.signature(werkzeug.funktion).bind(**argumente)
        except TypeError as fehler:
            return f"Fehler: Falsche Parameter für {name}: {fehler}."

        marke = aktuelle_anfrage.set(anfrage_id)
        try:
            if inspect.iscoroutinefunction(werkzeug.funktion):
                ergebnis = await werkzeug.funktion(**argumente)
            else:
                ergebnis = await asyncio.to_thread(werkzeug.funktion, **argumente)
        except WerkzeugFehler as fehler:
            return f"Fehler: {fehler}"
        except Exception as fehler:
            log.exception("Werkzeug %s ist fehlgeschlagen", name)
            return f"Fehler beim Ausführen von {name}: {fehler or type(fehler).__name__}"
        finally:
            aktuelle_anfrage.reset(marke)

        if isinstance(ergebnis, str):
            return ergebnis
        return json.dumps(ergebnis, ensure_ascii=False, default=str)
