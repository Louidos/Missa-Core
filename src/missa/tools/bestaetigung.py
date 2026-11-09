"""Bestätigungsregel für Lösch-Werkzeuge.

Missa darf nichts löschen, ohne Louis vorher zu fragen. Darum gilt:

1. Ein Aufruf ohne ``bestaetigt=True`` löscht nichts, sondern meldet, was
   gelöscht würde, und merkt die Rückfrage vor.
2. Ein Aufruf mit ``bestaetigt=True`` löscht nur, wenn die Rückfrage in einer
   **früheren** Chat-Anfrage gestellt wurde – Louis also inzwischen
   geantwortet hat – und noch nicht abgelaufen ist.

So kann das Modell nicht im selben Zug fragen und löschen.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from missa.core.werkzeuge import aktuelle_anfrage

#: So lange (Sekunden) bleibt eine Rückfrage gültig.
GUELTIGKEIT = 30 * 60


class Bestaetigungen:
    """Merkt sich offene Rückfragen zum Löschen (nur im Arbeitsspeicher)."""

    def __init__(
        self, gueltigkeit: float = GUELTIGKEIT, uhr: Callable[[], float] = time.monotonic
    ) -> None:
        self._gueltigkeit = gueltigkeit
        self._uhr = uhr
        self._offen: dict[str, tuple[str, float]] = {}
        self._sperre = threading.Lock()

    def _aufraeumen(self) -> None:
        grenze = self._uhr() - self._gueltigkeit
        for schluessel in [s for s, (_, zeit) in self._offen.items() if zeit < grenze]:
            del self._offen[schluessel]

    def vormerken(self, schluessel: str) -> None:
        """Merkt die Rückfrage vor. Eine schon offene Rückfrage bleibt unverändert."""
        with self._sperre:
            self._aufraeumen()
            self._offen.setdefault(schluessel, (aktuelle_anfrage.get(), self._uhr()))

    def erlaubt(self, schluessel: str) -> bool:
        """``True``, wenn die Rückfrage in einer früheren Anfrage gestellt wurde."""
        with self._sperre:
            self._aufraeumen()
            eintrag = self._offen.get(schluessel)
            return eintrag is not None and eintrag[0] != aktuelle_anfrage.get()

    def erledigt(self, schluessel: str) -> None:
        with self._sperre:
            self._offen.pop(schluessel, None)


def loeschen_pruefen(
    bestaetigungen: Bestaetigungen,
    schluessel: str,
    bestaetigt: bool,
    beschreibung: str,
    werkzeug: str,
    parameter: str,
) -> str | None:
    """Wendet die Bestätigungsregel an.

    Gibt ``None`` zurück, wenn gelöscht werden darf; sonst den Text, den das
    Modell bekommt (mit der Bitte, Louis zu fragen).
    """
    if bestaetigt and bestaetigungen.erlaubt(schluessel):
        return None
    bestaetigungen.vormerken(schluessel)
    grund = (
        "NICHT GELÖSCHT, Louis hat noch nicht zugestimmt." if bestaetigt else "NOCH NICHT GELÖSCHT."
    )
    return (
        f"{grund} Frag Louis zuerst, ob das wirklich gelöscht werden soll:\n"
        f"{beschreibung}\n"
        f"Erst wenn er in seiner nächsten Nachricht ausdrücklich Ja sagt, rufe {werkzeug} "
        f"mit {parameter} und bestaetigt=true auf."
    )
