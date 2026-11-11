"""Werkzeuge für die Nachtschicht: lange Aufgaben, die nachts erledigt werden.

Louis sagt z. B. „Mach mir heute Nacht eine Zusammenfassung aller offenen
Aufgaben“. Die Aufgabe wartet bis zum Nachtfenster (Standard 01:00–06:00, im
Gespräch änderbar), läuft dann im Hintergrund, und das Ergebnis kommt am Morgen
als Nachricht und wird im nächsten Gespräch erwähnt.
"""

from __future__ import annotations

from typing import Any

from missa.core.hintergrund import Hintergrund
from missa.core.vorgaben import uhrzeit_lesen
from missa.core.werkzeuge import Werkzeug, WerkzeugFehler, im_hintergrund
from missa.core.zeitplaner import abstand_text, zeitpunkt_text
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen


def _id_lesen(wert: Any) -> int:
    try:
        return int(str(wert).strip().lstrip("#"))
    except ValueError as fehler:
        raise WerkzeugFehler(f"auftrag_id „{wert}“ ist keine Zahl.") from fehler


class NachtschichtWerkzeuge:
    def __init__(self, hintergrund: Hintergrund, bestaetigungen: Bestaetigungen | None = None):
        self.hintergrund = hintergrund
        self.bestaetigungen = bestaetigungen or Bestaetigungen()

    def _nachtauftrag(self, auftrag_id: Any):
        nummer = _id_lesen(auftrag_id)
        auftrag = self.hintergrund.holen(nummer)
        if auftrag is None or auftrag.art != "nacht":
            raise WerkzeugFehler(
                f"Eine Nachtaufgabe #{nummer} gibt es nicht (mehr). "
                "Zeig sie mit nachtaufgaben_anzeigen an."
            )
        return auftrag

    def anlegen(self, anweisung: str, titel: str | None = None) -> str:
        if im_hintergrund.get():
            raise WerkzeugFehler("Im Hintergrund lege ich keine neuen Nachtaufgaben an.")
        auftrag = self.hintergrund.einreihen(titel or "", anweisung, art="nacht")
        h = self.hintergrund
        if h.im_fenster():
            wann = "Die Nachtschicht läuft gerade – ich fange gleich damit an."
        else:
            beginn = h.naechster_fensterbeginn()
            wann = (
                f"Ich erledige sie in der Nachtschicht ({h.fenster_text()}), also ab "
                f"{zeitpunkt_text(beginn)} ({abstand_text(h.jetzt(), beginn)})."
            )
        return (
            f"Nachtaufgabe #{auftrag.id} „{auftrag.titel}“ vorgemerkt. {wann} Das Ergebnis "
            "bekommt Louis am Morgen als Nachricht, und du erwähnst es im nächsten Gespräch."
        )

    def anzeigen(self) -> str:
        auftraege = self.hintergrund.liste("nacht")
        kopf = f"Nachtschicht: {self.hintergrund.fenster_text()}."
        if not auftraege:
            return kopf + " Es sind keine Nachtaufgaben vorgemerkt oder kürzlich erledigt."
        return kopf + "\n" + "\n".join(a.zeile() for a in auftraege)

    def ergebnis(self, auftrag_id: int | str) -> str:
        auftrag = self._nachtauftrag(auftrag_id)
        if auftrag.status == "fertig":
            return f"Ergebnis von #{auftrag.id} „{auftrag.titel}“:\n{auftrag.ergebnis}"
        return auftrag.zeile().removeprefix("- ")

    def loeschen(self, auftrag_id: int | str, bestaetigt: bool = False) -> str:
        auftrag = self._nachtauftrag(auftrag_id)
        if rueckfrage := loeschen_pruefen(
            self.bestaetigungen,
            f"nachtaufgabe:{auftrag.id}",
            bool(bestaetigt),
            auftrag.zeile(),
            "nachtaufgabe_loeschen",
            f"auftrag_id={auftrag.id}",
        ):
            return rueckfrage
        self.hintergrund.loeschen(auftrag.id)
        self.bestaetigungen.erledigt(f"nachtaufgabe:{auftrag.id}")
        return f"Nachtaufgabe gelöscht:\n{auftrag.zeile()}"

    def einstellen(self, von: str, bis: str) -> str:
        if im_hintergrund.get():
            raise WerkzeugFehler("Im Hintergrund ändere ich keine Einstellungen.")
        beginn, ende = uhrzeit_lesen(von), uhrzeit_lesen(bis)
        self.hintergrund.fenster_einstellen(beginn, ende)
        return (
            f"Nachtschicht gespeichert: {self.hintergrund.fenster_text()} (gilt ab sofort, "
            "auch nach einem Neustart). Den Morgenbericht schicke ich am Ende des Fensters."
        )


def nachtschicht_werkzeuge(
    hintergrund: Hintergrund, bestaetigungen: Bestaetigungen | None = None
) -> list[Werkzeug]:
    w = NachtschichtWerkzeuge(hintergrund, bestaetigungen)
    id_schema = {
        "type": "object",
        "properties": {"auftrag_id": {"type": "integer", "description": "ID der Nachtaufgabe"}},
        "required": ["auftrag_id"],
    }
    return [
        Werkzeug(
            "nachtaufgabe_anlegen",
            "Merkt eine lange Aufgabe für die Nachtschicht vor („mach das heute Nacht“, "
            "„erledige das nachts“). Sie läuft im Nachtfenster im Hintergrund, das Ergebnis "
            "bekommt Louis am Morgen. Gut für alles, was auf der CPU lange dauert.",
            {
                "type": "object",
                "properties": {
                    "anweisung": {
                        "type": "string",
                        "description": "Was du in der Nacht genau tun sollst",
                    },
                    "titel": {"type": "string", "description": "Kurzer Titel, optional"},
                },
                "required": ["anweisung"],
            },
            w.anlegen,
        ),
        Werkzeug(
            "nachtaufgaben_anzeigen",
            "Zeigt das Nachtfenster, vorgemerkte Nachtaufgaben und Ergebnisse der letzten Tage.",
            {"type": "object", "properties": {}},
            w.anzeigen,
        ),
        Werkzeug(
            "nachtaufgabe_ergebnis",
            "Liefert das vollständige Ergebnis einer Nachtaufgabe.",
            id_schema,
            w.ergebnis,
        ),
        Werkzeug(
            "nachtaufgabe_loeschen",
            "Löscht eine Nachtaufgabe. Zuerst ohne bestaetigt aufrufen und Louis fragen; erst "
            "nach seinem ausdrücklichen Ja nochmals mit bestaetigt=true aufrufen.",
            {
                "type": "object",
                "properties": {
                    "auftrag_id": {"type": "integer", "description": "ID der Nachtaufgabe"},
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true nach Louis' ausdrücklichem Ja zum Löschen",
                    },
                },
                "required": ["auftrag_id"],
            },
            w.loeschen,
        ),
        Werkzeug(
            "nachtschicht_einstellen",
            "Ändert das Zeitfenster der Nachtschicht (Standard 01:00–06:00) dauerhaft, z. B. "
            "„Nachtschicht von 2 bis 5 Uhr“.",
            {
                "type": "object",
                "properties": {
                    "von": {"type": "string", "description": "Beginn, z. B. 02:00"},
                    "bis": {"type": "string", "description": "Ende, z. B. 05:00"},
                },
                "required": ["von", "bis"],
            },
            w.einstellen,
        ),
    ]
