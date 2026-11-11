"""Bestätigungsregel für Lösch-Werkzeuge.

Missa darf nichts löschen, ohne Louis vorher zu fragen. Darum gilt:

1. Ein Aufruf ohne ``bestaetigt=True`` löscht nichts, sondern meldet, was
   gelöscht würde, und merkt die Rückfrage vor.
2. Ein Aufruf mit ``bestaetigt=True`` löscht nur, wenn die Rückfrage in einer
   **früheren** Chat-Anfrage gestellt wurde – Louis also inzwischen
   geantwortet hat – und noch nicht abgelaufen ist.

So kann das Modell nicht im selben Zug fragen und löschen. Läuft Missa im
Hintergrund (Routine, Nachtschicht), gilt nie eine Zustimmung.

Darüber liegt die zentrale Rechte-Schicht (:mod:`missa.core.rechte`): Werkzeuge der
Stufe ``zerstoererisch`` laufen nur nach Louis' ausdrücklichem Ja in seiner neuen
Nachricht und innerhalb von 10 Minuten. Hat sie freigegeben, gilt das auch hier.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from missa.core.werkzeuge import aktuelle_anfrage, im_hintergrund, zentral_freigegeben

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
        """``True``, wenn die Rückfrage in einer früheren Anfrage gestellt wurde.

        Im Hintergrund (Routine, Nachtschicht) nie: Louis kann dort nicht Ja sagen.
        """
        if im_hintergrund.get():
            return False
        if zentral_freigegeben.get():
            return True  # Louis hat genau diese Aktion über die Rechte-Schicht bestätigt
        with self._sperre:
            self._aufraeumen()
            eintrag = self._offen.get(schluessel)
            return eintrag is not None and eintrag[0] != aktuelle_anfrage.get()

    def erledigt(self, schluessel: str) -> None:
        with self._sperre:
            self._offen.pop(schluessel, None)


def zustimmung_pruefen(
    bestaetigungen: Bestaetigungen,
    schluessel: str,
    bestaetigt: bool,
    beschreibung: str,
    werkzeug: str,
    parameter: str,
    frage: str,
) -> str | None:
    """Wie :func:`loeschen_pruefen`, aber für beliebige Aktionen (z. B. Einladungen).

    ``frage`` ist der Satz, mit dem Missa Louis fragen soll.
    """
    if bestaetigt and bestaetigungen.erlaubt(schluessel):
        return None
    bestaetigungen.vormerken(schluessel)
    grund = (
        "NICHT AUSGEFÜHRT, Louis hat noch nicht zugestimmt."
        if bestaetigt
        else "NOCH NICHT AUSGEFÜHRT."
    )
    return (
        f"{grund} {frage}\n{beschreibung}\n"
        f"Erst wenn er in seiner nächsten Nachricht ausdrücklich Ja sagt, rufe {werkzeug} "
        f"mit denselben Angaben ({parameter}) und bestaetigt=true auf."
    )


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
