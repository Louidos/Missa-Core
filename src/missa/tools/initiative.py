"""Werkzeuge für die Denkschleife (Missa Initiative): einstellen und nachsehen."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from missa.core.initiative import (
    AKTIV,
    BUDGET,
    INTERVALL,
    MIN_INTERVALL,
    PAUSE_BIS,
    SAMMELZEIT,
    Initiative,
)
from missa.core.vorgaben import uhrzeit_lesen
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler


def _zahl(wert: Any, name: str) -> int:
    try:
        return int(wert)
    except (TypeError, ValueError) as fehler:
        raise WerkzeugFehler(f"{name} muss eine ganze Zahl sein.") from fehler


def status_text(initiative: Initiative) -> str:
    status = initiative.status()
    zeilen = [
        f"Denkschleife: {'an' if status['aktiv'] else 'aus'}",
        f"Ich denke alle {status['intervall_minuten']} Minuten nach.",
        f"Heute gemeldet: {status['heute_verbraucht']} von {status['budget_pro_tag']} "
        "(Alarme aus festen Regeln zählen nicht mit).",
        f"Sammelmeldung mit offenen Notizen um {status['sammelzeit']} Uhr "
        f"(gerade offen: {status['offene_notizen']}).",
    ]
    if status["pausiert_bis"]:
        bis = initiative.pausiert_bis()
        zeilen.append(f"Pausiert bis {bis:%d.%m. %H:%M} Uhr." if bis else "")
    letzter = initiative.laeufe(1)
    if letzter:
        lauf = letzter[0]
        zeile = f"Letzter Lauf: {lauf.zeit:%d.%m. %H:%M} Uhr – "
        if lauf.uebersprungen:
            zeile += f"übersprungen ({lauf.ursache})"
        else:
            zeile += lauf.aktion + (f" ({lauf.grund})" if lauf.grund else "")
        zeilen.append(zeile)
    else:
        zeilen.append("Bisher gab es noch keinen Lauf.")
    return "\n".join(z for z in zeilen if z)


def initiative_werkzeuge(initiative: Initiative) -> list[Werkzeug]:
    def einstellen(
        aktiv: bool | None = None,
        intervall_minuten: int | None = None,
        budget_pro_tag: int | None = None,
        sammelzeit: str | None = None,
        pause_minuten: int | None = None,
    ) -> str:
        vorgaben = initiative.vorgaben
        geaendert = []
        if aktiv is not None:
            vorgaben.setzen(AKTIV, "ja" if aktiv else "nein")
            geaendert.append("an/aus")
        if intervall_minuten is not None:
            minuten = _zahl(intervall_minuten, "intervall_minuten")
            if minuten < MIN_INTERVALL:
                raise WerkzeugFehler(
                    f"Öfter als alle {MIN_INTERVALL} Minuten denke ich nicht nach."
                )
            vorgaben.setzen(INTERVALL, str(minuten))
            geaendert.append("Intervall")
        if budget_pro_tag is not None:
            budget = _zahl(budget_pro_tag, "budget_pro_tag")
            if budget < 0:
                raise WerkzeugFehler("Das Budget kann nicht negativ sein (0 = nie von selbst).")
            vorgaben.setzen(BUDGET, str(budget))
            geaendert.append("Budget")
        if sammelzeit:
            vorgaben.setzen(SAMMELZEIT, f"{uhrzeit_lesen(sammelzeit):%H:%M}")
            geaendert.append("Sammelzeit")
        if pause_minuten is not None:
            minuten = _zahl(pause_minuten, "pause_minuten")
            if minuten <= 0:
                vorgaben.setzen(PAUSE_BIS, "")
            else:
                bis = initiative.jetzt() + timedelta(minutes=minuten)
                vorgaben.setzen(PAUSE_BIS, bis.isoformat(timespec="seconds"))
            geaendert.append("Pause")
        if not geaendert:
            raise WerkzeugFehler(
                "Was soll ich ändern? Möglich: aktiv, intervall_minuten, budget_pro_tag, "
                "sammelzeit, pause_minuten."
            )
        if initiative.zeitplaner is not None:
            initiative.einplanen(initiative.zeitplaner)
        return "Gespeichert.\n" + status_text(initiative)

    return [
        Werkzeug(
            "initiative_einstellen",
            "Stellt deine Denkschleife ein (wie oft du von selbst nachdenkst und dich meldest): "
            "an/aus, Intervall in Minuten, wie oft pro Tag du höchstens von selbst störst "
            "(„Stör mich höchstens dreimal am Tag“ → budget_pro_tag=3), Uhrzeit der abendlichen "
            "Sammelmeldung, oder eine Pause („Denk mal eine Stunde nicht nach“ → "
            "pause_minuten=60; 0 hebt sie auf). Alarme aus festen Regeln betrifft das nie.",
            {
                "type": "object",
                "properties": {
                    "aktiv": {"type": "boolean", "description": "Denkschleife an oder aus"},
                    "intervall_minuten": {
                        "type": "integer",
                        "description": f"Abstand zwischen Läufen (mindestens {MIN_INTERVALL})",
                    },
                    "budget_pro_tag": {
                        "type": "integer",
                        "description": "Höchstens so viele Meldungen pro Tag (0 = nie)",
                    },
                    "sammelzeit": {"type": "string", "description": "z. B. 19:30"},
                    "pause_minuten": {
                        "type": "integer",
                        "description": "So lange nicht nachdenken (0 = Pause aufheben)",
                    },
                },
            },
            einstellen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "initiative_status",
            "Zeigt, ob deine Denkschleife an ist, wie oft sie läuft, wie viele Meldungen heute "
            "schon verbraucht sind und wann der letzte Lauf war.",
            {"type": "object", "properties": {}},
            lambda: status_text(initiative),
            stufe=Stufe.LESEN,
        ),
    ]
