"""Werkzeuge für Routinen: wiederkehrende Aufträge an Missa.

Eine Routine ist eine Text-Anweisung mit einer Wiederholung („jeden Freitag um
17 Uhr: Wochenrückblick“). Zur Zeit übergibt der Zeitplaner die Anweisung an
Missa (im Hintergrund, mit allen Werkzeugen), und ihre Antwort geht als
Nachricht an Louis. Die Zeitangabe versteht der Code, nicht das Modell.
"""

from __future__ import annotations

from typing import Any

from missa.core.hintergrund import Hintergrund
from missa.core.werkzeuge import Werkzeug, WerkzeugFehler, im_hintergrund
from missa.core.zeitangaben import Rueckfrage, zeit_verstehen
from missa.core.zeitplaner import Erinnerung, Zeitplaner, abstand_text, zeitpunkt_text
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen


def _id_lesen(wert: Any) -> int:
    try:
        return int(str(wert).strip().lstrip("#"))
    except ValueError as fehler:
        raise WerkzeugFehler(f"routine_id „{wert}“ ist keine Zahl.") from fehler


def _kein_hintergrund(was: str) -> None:
    if im_hintergrund.get():
        raise WerkzeugFehler(
            f"Im Hintergrund kann ich {was} nicht – schlag es Louis stattdessen vor."
        )


class RoutinenWerkzeuge:
    def __init__(
        self,
        zeitplaner: Zeitplaner,
        hintergrund: Hintergrund,
        bestaetigungen: Bestaetigungen | None = None,
    ) -> None:
        self.zeitplaner = zeitplaner
        self.hintergrund = hintergrund
        self.bestaetigungen = bestaetigungen or Bestaetigungen()

    def _routine(self, routine_id: Any) -> Erinnerung:
        nummer = _id_lesen(routine_id)
        routine = self.zeitplaner.holen(nummer, "routine")
        if routine is None:
            raise WerkzeugFehler(
                f"Eine Routine #{nummer} gibt es nicht (mehr). Zeig sie mit routinen_anzeigen an."
            )
        return routine

    def _beschreibung(self, routine: Erinnerung, vorsatz: str) -> str:
        jetzt = self.zeitplaner.jetzt()
        zeilen = [f"{vorsatz} #{routine.id}: „{routine.text}“."]
        if routine.wiederholung:
            zeilen.append(
                f"Läuft {routine.wiederholung.text()} um {routine.uhrzeit:%H:%M} Uhr "
                "(auch über die Zeitumstellung)."
            )
        if routine.pausiert:
            zeilen.append("Sie ist PAUSIERT und läuft erst wieder nach routine_fortsetzen.")
        else:
            zeilen.append(
                f"Nächster Lauf: {zeitpunkt_text(routine.faellig)} "
                f"({abstand_text(jetzt, routine.faellig)})."
            )
        zeilen.append(f"Anweisung: {routine.anweisung}")
        if not self.zeitplaner.zustellung.verfuegbar:
            zeilen.append(
                "ACHTUNG: Gerade ist kein Kanal eingerichtet, über den ich Louis das Ergebnis "
                "schicken kann (NC_TALK_NOTIFY_CONVERSATION fehlt) – sag ihm das ehrlich."
            )
        return "\n".join(zeilen)

    # -------------------------------------------------------------- Werkzeuge

    def anlegen(self, anweisung: str, wann: str, name: str | None = None) -> str:
        _kein_hintergrund("keine neuen Routinen anlegen")
        try:
            angabe = zeit_verstehen(wann, self.zeitplaner.jetzt())
        except Rueckfrage as frage:
            return (
                "NOCH NICHT GESPEICHERT – die Zeitangabe ist mehrdeutig. Frag Louis: "
                f"{frage.frage}\nRufe danach das Werkzeug mit der genauen Angabe erneut auf."
            )
        if angabe.wiederholung is None:
            return (
                "NOCH NICHT GESPEICHERT – eine Routine wiederholt sich, aber „"
                f"{wann}“ ist ein einzelner Zeitpunkt. Frag Louis, wie oft sie laufen soll "
                "(z. B. „jeden Freitag um 17 Uhr“, „werktags um 7“). Für etwas Einmaliges "
                "nimm erinnerung_anlegen oder nachtaufgabe_anlegen."
            )
        titel = " ".join(str(name or "").split()) or " ".join(str(anweisung).split())[:60]
        routine = self.zeitplaner.anlegen(
            titel,
            angabe.zeitpunkt,
            angabe.wiederholung,
            art="routine",
            anweisung=anweisung,
        )
        return self._beschreibung(routine, "Routine gespeichert") + (
            "\nSag Louis genau, wann sie läuft; das Ergebnis bekommt er jeweils als Nachricht."
        )

    def anzeigen(self) -> str:
        routinen = self.zeitplaner.offene("routine")
        if not routinen:
            return "Es sind keine Routinen eingerichtet."
        return "Routinen:\n" + "\n".join(r.zeile() for r in routinen)

    def pausieren(self, routine_id: int | str) -> str:
        routine = self._routine(routine_id)
        if routine.pausiert:
            return f"Routine #{routine.id} „{routine.text}“ ist schon pausiert."
        return self._beschreibung(self.zeitplaner.pausieren(routine.id, True), "Routine pausiert")

    def fortsetzen(self, routine_id: int | str) -> str:
        routine = self._routine(routine_id)
        if not routine.pausiert:
            return self._beschreibung(routine, "Routine läuft bereits")
        return self._beschreibung(
            self.zeitplaner.pausieren(routine.id, False), "Routine fortgesetzt"
        )

    def loeschen(self, routine_id: int | str, bestaetigt: bool = False) -> str:
        routine = self._routine(routine_id)
        if rueckfrage := loeschen_pruefen(
            self.bestaetigungen,
            f"routine:{routine.id}",
            bool(bestaetigt),
            routine.zeile(),
            "routine_loeschen",
            f"routine_id={routine.id}",
        ):
            return rueckfrage
        self.zeitplaner.loeschen(routine.id, "routine")
        self.bestaetigungen.erledigt(f"routine:{routine.id}")
        return f"Routine gelöscht:\n{routine.zeile()}"

    def jetzt_ausfuehren(self, routine_id: int | str) -> str:
        _kein_hintergrund("keine Routine starten")
        routine = self._routine(routine_id)
        auftrag = self.hintergrund.einreihen(
            routine.text,
            routine.anweisung or routine.text,
            art="sofort",
            herkunft="routine",
            routine_id=routine.id,
            zustellen=True,
        )
        return (
            f"Routine #{routine.id} „{routine.text}“ läuft jetzt im Hintergrund (Auftrag "
            f"#{auftrag.id}). Das Modell braucht auf der CPU etwas Zeit – das Ergebnis kommt als "
            "Nachricht, Louis kann so lange weiter mit dir reden. Der reguläre Zeitplan bleibt."
        )


def routinen_werkzeuge(
    zeitplaner: Zeitplaner,
    hintergrund: Hintergrund,
    bestaetigungen: Bestaetigungen | None = None,
) -> list[Werkzeug]:
    w = RoutinenWerkzeuge(zeitplaner, hintergrund, bestaetigungen)
    id_schema = {
        "type": "object",
        "properties": {"routine_id": {"type": "integer", "description": "ID der Routine"}},
        "required": ["routine_id"],
    }
    return [
        Werkzeug(
            "routine_anlegen",
            "Legt eine Routine an: eine Anweisung an dich selbst, die du regelmäßig ausführst "
            "(z. B. „jeden Freitag um 17 Uhr einen Wochenrückblick schreiben“). Zur Zeit "
            "erledigst du sie mit deinen Werkzeugen, das Ergebnis geht als Nachricht an Louis.",
            {
                "type": "object",
                "properties": {
                    "anweisung": {
                        "type": "string",
                        "description": "Was du dann tun sollst, so genau wie Louis es gesagt hat",
                    },
                    "wann": {
                        "type": "string",
                        "description": "Wiederholung so, wie Louis sie gesagt hat, z. B. „jeden "
                        "Freitag um 17 Uhr“, „werktags um 7“, „täglich um 21 Uhr“ – nicht "
                        "selbst umrechnen",
                    },
                    "name": {"type": "string", "description": "Kurzer Name, z. B. Wochenrückblick"},
                },
                "required": ["anweisung", "wann"],
            },
            w.anlegen,
        ),
        Werkzeug(
            "routinen_anzeigen",
            "Listet alle Routinen mit ID, nächstem Lauf, Wiederholung und ob sie pausiert sind.",
            {"type": "object", "properties": {}},
            w.anzeigen,
        ),
        Werkzeug(
            "routine_pausieren", "Pausiert eine Routine (läuft nicht mehr).", id_schema, w.pausieren
        ),
        Werkzeug(
            "routine_fortsetzen",
            "Setzt eine pausierte Routine fort (ab dem nächsten regulären Zeitpunkt).",
            id_schema,
            w.fortsetzen,
        ),
        Werkzeug(
            "routine_loeschen",
            "Löscht eine Routine. Zuerst ohne bestaetigt aufrufen: dann wird nur gemeldet, was "
            "gelöscht würde, und du fragst Louis. Erst nach seinem ausdrücklichen Ja nochmals "
            "mit bestaetigt=true aufrufen.",
            {
                "type": "object",
                "properties": {
                    "routine_id": {"type": "integer", "description": "ID der Routine"},
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true nach Louis' ausdrücklichem Ja zum Löschen",
                    },
                },
                "required": ["routine_id"],
            },
            w.loeschen,
        ),
        Werkzeug(
            "routine_jetzt_ausfuehren",
            "Führt eine Routine sofort einmal im Hintergrund aus (z. B. zum Ausprobieren).",
            id_schema,
            w.jetzt_ausfuehren,
        ),
    ]
