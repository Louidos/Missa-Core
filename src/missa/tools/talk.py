"""Werkzeug: Missa schreibt von sich aus eine Nachricht in Nextcloud Talk.

Ziel ist immer genau eine Unterhaltung (``NC_TALK_NOTIFY_CONVERSATION``); das
Modell kann keine andere wählen. Ein Rate-Limit verhindert Nachrichtenfluten.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from missa.channels.nextcloud_talk import TalkClient, TalkFehler, aufteilen
from missa.core.werkzeuge import Werkzeug, WerkzeugFehler

#: Standard: höchstens so viele Nachrichten pro Minute.
STANDARD_RATE = 10


class Ratenbegrenzer:
    """Erlaubt höchstens ``anzahl`` Ereignisse je gleitendem 60-Sekunden-Fenster."""

    def __init__(self, anzahl: int, uhr: Callable[[], float] = time.monotonic) -> None:
        self.anzahl = max(1, anzahl)
        self._uhr = uhr
        self._zeiten: deque[float] = deque()
        self._sperre = threading.Lock()

    def _aufraeumen(self, jetzt: float) -> None:
        while self._zeiten and jetzt - self._zeiten[0] >= 60:
            self._zeiten.popleft()

    def versuchen(self, menge: int = 1) -> bool:
        """Bucht ``menge`` Plätze, wenn noch genug frei sind."""
        with self._sperre:
            jetzt = self._uhr()
            self._aufraeumen(jetzt)
            if len(self._zeiten) + menge > self.anzahl:
                return False
            self._zeiten.extend([jetzt] * menge)
            return True

    def wartezeit(self) -> int:
        """Sekunden, bis wieder ein Platz frei wird."""
        with self._sperre:
            jetzt = self._uhr()
            self._aufraeumen(jetzt)
            if len(self._zeiten) < self.anzahl:
                return 0
            return max(1, math.ceil(60 - (jetzt - self._zeiten[0])))


class TalkMelder:
    """Schickt Nachrichten von Missa in die eingestellte Talk-Unterhaltung."""

    def __init__(self, client: TalkClient | None, token: str, begrenzer: Ratenbegrenzer) -> None:
        self.client = client
        self.token = token.strip()
        self.begrenzer = begrenzer

    async def senden(self, text: str) -> str:
        if self.client is None:
            raise WerkzeugFehler(
                "Nextcloud Talk ist nicht eingerichtet – ich kann gerade nicht in Talk schreiben."
            )
        if not self.token:
            raise WerkzeugFehler(
                "Es ist keine Talk-Unterhaltung für meine Nachrichten eingestellt "
                "(NC_TALK_NOTIFY_CONVERSATION ist leer) – ich kann dir darum nicht von mir "
                "aus in Talk schreiben."
            )
        text = str(text).strip()
        if not text:
            raise WerkzeugFehler("Die Nachricht ist leer.")
        teile = aufteilen(text)
        if not self.begrenzer.versuchen(len(teile)):
            raise WerkzeugFehler(
                f"Nicht gesendet: höchstens {self.begrenzer.anzahl} Talk-Nachrichten pro Minute. "
                f"Wieder möglich in etwa {self.begrenzer.wartezeit()} Sekunden."
            )
        try:
            for teil in teile:
                await self.client.nachricht_senden(self.token, teil)
        except TalkFehler as fehler:
            raise WerkzeugFehler(
                f"Die Talk-Nachricht konnte nicht gesendet werden: {fehler}"
            ) from fehler
        return (
            "Nachricht in Talk gesendet."
            if len(teile) == 1
            else f"Nachricht in Talk gesendet ({len(teile)} Teile)."
        )


def talk_werkzeuge(melder: TalkMelder) -> list[Werkzeug]:
    return [
        Werkzeug(
            "talk_nachricht_senden",
            "Schreibt Louis von dir aus eine Nachricht in seine Talk-Unterhaltung mit dir "
            "(z. B. eine Erinnerung, einen Hinweis oder eine Zusammenfassung). Nur nutzen, "
            "wenn Louis darum bittet oder etwas wirklich wichtig ist. Markdown ist erlaubt.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Die Nachricht an Louis"},
                },
                "required": ["text"],
            },
            melder.senden,
        )
    ]
