"""Werkzeuge für Erinnerungen: anlegen, anzeigen, ändern, löschen, schlummern.

Die Zeitangabe wird nicht vom Sprachmodell umgerechnet, sondern im Code
verstanden (:mod:`missa.core.zeitangaben`) – so stimmen Datum, Uhrzeit und
Sommer-/Winterzeit immer.
"""

from __future__ import annotations

import re
from typing import Any

from missa.core.werkzeuge import Werkzeug, WerkzeugFehler
from missa.core.zeitangaben import Rueckfrage, Wiederholung, zeit_verstehen
from missa.core.zeitplaner import Erinnerung, Zeitplaner, abstand_text, zeitpunkt_text
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen


def _id_lesen(wert: Any) -> int:
    try:
        return int(str(wert).strip().lstrip("#"))
    except ValueError as fehler:
        raise WerkzeugFehler(f"erinnerung_id „{wert}“ ist keine Zahl.") from fehler


def _minuten_lesen(wert: Any) -> float:
    """``10``, ``"10"``, ``"in 10 Minuten"``, ``"1 Stunde"``."""
    if isinstance(wert, int | float):
        return float(wert)
    text = str(wert).strip().lower()
    if treffer := re.fullmatch(r"(?:in\s+)?(\d+(?:[.,]\d+)?)\s*(min\w*|minuten?)?", text):
        return float(treffer.group(1).replace(",", "."))
    if treffer := re.fullmatch(r"(?:in\s+)?(\d+(?:[.,]\d+)?|einer|eine)\s*(h|std|stunden?)", text):
        menge = 1.0 if treffer.group(1) in ("einer", "eine") else float(treffer.group(1))
        return menge * 60
    raise WerkzeugFehler(f"„{wert}“ verstehe ich nicht als Minuten (z. B. 10).")


class ErinnerungsWerkzeuge:
    def __init__(
        self, zeitplaner: Zeitplaner, bestaetigungen: Bestaetigungen | None = None
    ) -> None:
        self.zeitplaner = zeitplaner
        self.bestaetigungen = bestaetigungen or Bestaetigungen()

    def _bestaetigung(self, erinnerung: Erinnerung, vorsatz: str) -> str:
        jetzt = self.zeitplaner.jetzt()
        zeilen = [
            f"{vorsatz} #{erinnerung.id}: „{erinnerung.text}“ am "
            f"{zeitpunkt_text(erinnerung.faellig)} ({abstand_text(jetzt, erinnerung.faellig)})."
        ]
        if erinnerung.wiederholung:
            zeilen.append(
                f"Wiederholt sich {erinnerung.wiederholung.text()} um "
                f"{erinnerung.uhrzeit:%H:%M} Uhr (auch über die Zeitumstellung)."
            )
        if not self.zeitplaner.zustellung.verfuegbar:
            zeilen.append(
                "ACHTUNG: Gerade ist kein Kanal eingerichtet, über den ich Louis schreiben "
                "kann (NC_TALK_NOTIFY_CONVERSATION fehlt) – sag ihm das ehrlich."
            )
        zeilen.append("Nenne Louis Datum und Uhrzeit genau so zur Bestätigung.")
        return "\n".join(zeilen)

    def _verstehen(self, wann: str):
        try:
            return zeit_verstehen(wann, self.zeitplaner.jetzt()), None
        except Rueckfrage as frage:
            return None, (
                "NOCH NICHT GESPEICHERT – die Zeitangabe ist mehrdeutig. Frag Louis: "
                f"{frage.frage}\nRufe danach das Werkzeug mit der genauen Angabe erneut auf."
            )

    # -------------------------------------------------------------- Werkzeuge

    def anlegen(self, was: str, wann: str, nachricht: str | None = None) -> str:
        angabe, rueckfrage = self._verstehen(wann)
        if rueckfrage:
            return rueckfrage
        erinnerung = self.zeitplaner.anlegen(
            was, angabe.zeitpunkt, angabe.wiederholung, nachricht=nachricht
        )
        return self._bestaetigung(erinnerung, "Erinnerung gespeichert")

    def anzeigen(self) -> str:
        offene = self.zeitplaner.offene()
        if not offene:
            return "Es sind keine Erinnerungen geplant."
        return "Geplante Erinnerungen:\n" + "\n".join(e.zeile() for e in offene)

    def aendern(
        self,
        erinnerung_id: int | str,
        wann: str | None = None,
        was: str | None = None,
        nachricht: str | None = None,
        wiederholung_beenden: bool = False,
    ) -> str:
        nummer = _id_lesen(erinnerung_id)
        zeitpunkt = None
        wiederholung: Wiederholung | bool | None = None if wiederholung_beenden else False
        if wann:
            angabe, rueckfrage = self._verstehen(wann)
            if rueckfrage:
                return rueckfrage
            zeitpunkt = angabe.zeitpunkt
            if angabe.wiederholung is not None:
                wiederholung = angabe.wiederholung
        erinnerung = self.zeitplaner.aendern(nummer, was, zeitpunkt, wiederholung, nachricht)
        return self._bestaetigung(erinnerung, "Erinnerung geändert")

    def loeschen(self, erinnerung_id: int | str, bestaetigt: bool = False) -> str:
        nummer = _id_lesen(erinnerung_id)
        erinnerung = self.zeitplaner.holen(nummer)
        if erinnerung is None:
            raise WerkzeugFehler(f"Eine Erinnerung #{nummer} gibt es nicht (mehr).")
        if rueckfrage := loeschen_pruefen(
            self.bestaetigungen,
            f"erinnerung:{nummer}",
            bool(bestaetigt),
            erinnerung.zeile(),
            "erinnerung_loeschen",
            f"erinnerung_id={nummer}",
        ):
            return rueckfrage
        self.zeitplaner.loeschen(nummer)
        self.bestaetigungen.erledigt(f"erinnerung:{nummer}")
        return f"Erinnerung gelöscht:\n{erinnerung.zeile()}"

    def schlummern(self, minuten: Any = 10, erinnerung_id: int | str | None = None) -> str:
        nummer = None if erinnerung_id in (None, "") else _id_lesen(erinnerung_id)
        erinnerung = self.zeitplaner.schlummern(_minuten_lesen(minuten), nummer)
        return self._bestaetigung(erinnerung, "Ich erinnere dich nochmal")

    # -------------------------------------------------------------- Kontext

    def kontext(self, _frage: str) -> str | None:
        """Nennt die zuletzt zugestellte Erinnerung, damit „nochmal in 10 Minuten“ klappt."""
        zuletzt = self.zeitplaner.zuletzt_zugestellt()
        if zuletzt is None or zuletzt.zugestellt is None:
            return None
        return (
            "## Zuletzt geschickte Erinnerung\n"
            f"Um {zuletzt.zugestellt:%H:%M} Uhr hast du Louis an „{zuletzt.text}“ erinnert "
            f"(#{zuletzt.id}). Sagt er „nochmal in 10 Minuten“, nutze erinnerung_schlummern."
        )


def erinnerungs_werkzeuge(
    zeitplaner: Zeitplaner, bestaetigungen: Bestaetigungen | None = None
) -> list[Werkzeug]:
    w = ErinnerungsWerkzeuge(zeitplaner, bestaetigungen)
    wann_beschreibung = (
        "Zeitangabe so, wie Louis sie gesagt hat, z. B. „um 6:30“, „in 20 Minuten“, "
        "„morgen um 8“, „am Freitag um 14 Uhr“, „jeden Montag um 7“, „werktags um 6:30“ "
        "– nicht selbst umrechnen"
    )
    return [
        Werkzeug(
            "erinnerung_anlegen",
            "Erinnert Louis zu einem Zeitpunkt per Nachricht (auch wiederkehrend). "
            "Die Zeitangabe wird im Code genau verstanden; bei Mehrdeutigkeit kommt eine "
            "Rückfrage zurück, die du Louis stellst.",
            {
                "type": "object",
                "properties": {
                    "was": {"type": "string", "description": "Woran erinnern, z. B. Training"},
                    "wann": {"type": "string", "description": wann_beschreibung},
                    "nachricht": {
                        "type": "string",
                        "description": "Optional: die Nachricht, die Louis dann bekommt, in "
                        "deinem Stil (z. B. „⏰ Zeit fürs Training! 💪“)",
                    },
                },
                "required": ["was", "wann"],
            },
            w.anlegen,
        ),
        Werkzeug(
            "erinnerungen_anzeigen",
            "Listet alle geplanten Erinnerungen mit ID, Zeitpunkt und Wiederholung.",
            {"type": "object", "properties": {}},
            w.anzeigen,
        ),
        Werkzeug(
            "erinnerung_aendern",
            "Ändert Text oder Zeitpunkt einer Erinnerung oder beendet ihre Wiederholung.",
            {
                "type": "object",
                "properties": {
                    "erinnerung_id": {"type": "integer", "description": "ID der Erinnerung"},
                    "wann": {"type": "string", "description": "Neue Zeitangabe, optional"},
                    "was": {"type": "string", "description": "Neuer Text, optional"},
                    "nachricht": {"type": "string", "description": "Neue Nachricht, optional"},
                    "wiederholung_beenden": {
                        "type": "boolean",
                        "description": "true = nur noch einmal erinnern",
                    },
                },
                "required": ["erinnerung_id"],
            },
            w.aendern,
        ),
        Werkzeug(
            "erinnerung_loeschen",
            "Löscht eine geplante Erinnerung. Zuerst ohne bestaetigt aufrufen: dann wird nur "
            "gemeldet, was gelöscht würde, und du fragst Louis. Erst nach seinem ausdrücklichen "
            "Ja nochmals mit bestaetigt=true aufrufen.",
            {
                "type": "object",
                "properties": {
                    "erinnerung_id": {"type": "integer", "description": "ID der Erinnerung"},
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true nach Louis' ausdrücklichem Ja zum Löschen",
                    },
                },
                "required": ["erinnerung_id"],
            },
            w.loeschen,
        ),
        Werkzeug(
            "erinnerung_schlummern",
            "Erinnert nochmal nach einigen Minuten („in 10 Minuten nochmal“). Ohne ID gilt "
            "die zuletzt geschickte Erinnerung.",
            {
                "type": "object",
                "properties": {
                    "minuten": {"type": "integer", "description": "Minuten, Standard 10"},
                    "erinnerung_id": {"type": "integer", "description": "Optional"},
                },
            },
            w.schlummern,
        ),
    ]
