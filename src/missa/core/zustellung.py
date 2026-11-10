"""Zustellung von Nachrichten, die Missa von sich aus schickt (z. B. Erinnerungen).

Ein Kanal ist alles mit einem ``name`` und einer async-Methode ``senden(text)``.
Heute gibt es Nextcloud Talk; Telefon oder eine App kommen später einfach als
weiterer Kanal dazu. Die Kanäle werden der Reihe nach probiert, bis einer klappt.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

log = logging.getLogger(__name__)


class ZustellFehler(Exception):
    """Keiner der Kanäle konnte die Nachricht zustellen."""


class Kanal(Protocol):
    """Ein Weg, Louis eine Nachricht zu schicken."""

    name: str

    async def senden(self, text: str) -> None:
        """Schickt ``text``; wirft :class:`ZustellFehler`, wenn es nicht klappt."""
        ...


class Zustellung:
    """Alle Kanäle, über die Missa von sich aus schreiben kann."""

    def __init__(self, kanaele: Iterable[Kanal] = ()) -> None:
        self._kanaele: list[Kanal] = list(kanaele)

    def hinzufuegen(self, kanal: Kanal) -> None:
        self._kanaele.append(kanal)

    @property
    def namen(self) -> list[str]:
        return [k.name for k in self._kanaele]

    @property
    def verfuegbar(self) -> bool:
        return bool(self._kanaele)

    async def senden(self, text: str, kanal: str | None = None) -> str:
        """Stellt ``text`` zu und gibt den Namen des benutzten Kanals zurück."""
        kandidaten = [k for k in self._kanaele if kanal in (None, "", k.name)]
        if not kandidaten:
            raise ZustellFehler(
                "Es ist kein Kanal eingerichtet, über den ich dir schreiben kann "
                "(z. B. NC_TALK_NOTIFY_CONVERSATION für Nextcloud Talk)."
            )
        gruende = []
        for kandidat in kandidaten:
            try:
                await kandidat.senden(text)
            except ZustellFehler as fehler:
                log.warning("Zustellung über %s fehlgeschlagen: %s", kandidat.name, fehler)
                gruende.append(f"{kandidat.name}: {fehler}")
                continue
            return kandidat.name
        raise ZustellFehler("; ".join(gruende))
