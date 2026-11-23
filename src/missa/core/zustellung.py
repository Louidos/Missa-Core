"""Zustellung von Nachrichten, die Missa von sich aus schickt (z. B. Erinnerungen).

Ein Kanal ist alles mit einem ``name`` und einer async-Methode ``senden(text)``.
Heute gibt es Nextcloud Talk; Telefon oder eine App kommen später einfach als
weiterer Kanal dazu. Die Kanäle werden der Reihe nach probiert, bis einer klappt.

Dazu kommt optional der Weg **„geraet“** (angeschlossene Rechner, ``MISSA_GERAETE_MELDUNGEN``):

- ``zusaetzlich`` (Standard): wie bisher über die Kanäle *und* an jedes Gerät, das online ist,
- ``bevorzugt``: nur ans Gerät, wenn eines online ist – sonst wie bisher,
- ``aus``: nie ans Gerät.

Ruhezeit, Ruhefenster und Stufen entscheiden vorher (Alarme, Zeitplaner); hier wird nur
zugestellt. Eine Nachricht gilt als zugestellt, sobald ein Weg geklappt hat – ein neuer
Versuch käme sonst doppelt auf dem Weg an, der schon geklappt hat.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

log = logging.getLogger(__name__)


#: Erlaubte Werte für ``MISSA_GERAETE_MELDUNGEN``.
GERAETE_MODI = ("aus", "zusaetzlich", "bevorzugt")


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
        self._geraet: Kanal | None = None
        self.geraete_modus = "aus"

    def hinzufuegen(self, kanal: Kanal) -> None:
        self._kanaele.append(kanal)

    def geraet_setzen(self, kanal: Kanal, modus: str = "zusaetzlich") -> None:
        """Richtet den Weg „geraet“ ein (``zusaetzlich``, ``bevorzugt`` oder ``aus``)."""
        if modus not in GERAETE_MODI:
            raise ValueError(f"Unbekannter Modus für Meldungen ans Gerät: {modus}")
        self._geraet = kanal
        self.geraete_modus = modus

    @property
    def _geraet_aktiv(self) -> Kanal | None:
        return self._geraet if self.geraete_modus != "aus" else None

    @property
    def namen(self) -> list[str]:
        namen = [k.name for k in self._kanaele]
        if (geraet := self._geraet_aktiv) is not None:
            namen.append(geraet.name)
        return namen

    @property
    def verfuegbar(self) -> bool:
        return bool(self._kanaele) or self._geraet_aktiv is not None

    async def senden(self, text: str, kanal: str | None = None) -> str:
        """Stellt ``text`` zu und gibt den benutzten Weg zurück (z. B. ``talk+geraet``)."""
        geraet = self._geraet_aktiv
        if geraet is not None and kanal == geraet.name:
            return await self._der_reihe_nach([geraet], text, kanal)
        if geraet is None or kanal not in (None, "") or not getattr(geraet, "online", True):
            return await self._der_reihe_nach(self._kanaele, text, kanal)
        if self.geraete_modus == "bevorzugt":
            try:
                await geraet.senden(text)
            except ZustellFehler as fehler:
                log.warning("Zustellung ans Gerät fehlgeschlagen, nehme die anderen: %s", fehler)
                return await self._der_reihe_nach(self._kanaele, text, kanal)
            return geraet.name
        # zusaetzlich: wie bisher und dazu ans Gerät.
        name: str | None = None
        fehler_normal: ZustellFehler | None = None
        try:
            name = await self._der_reihe_nach(self._kanaele, text, kanal)
        except ZustellFehler as fehler:
            fehler_normal = fehler
        try:
            await geraet.senden(text)
        except ZustellFehler as fehler:
            log.warning("Zustellung ans Gerät fehlgeschlagen: %s", fehler)
            if fehler_normal is not None:
                raise ZustellFehler(f"{fehler_normal}; {geraet.name}: {fehler}") from fehler
            assert name is not None  # noqa: S101 – Typ-Eingrenzung
            return name
        return f"{name}+{geraet.name}" if name else geraet.name

    async def _der_reihe_nach(self, kanaele: Iterable[Kanal], text: str, kanal: str | None) -> str:
        kandidaten = [k for k in kanaele if kanal in (None, "", k.name)]
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
