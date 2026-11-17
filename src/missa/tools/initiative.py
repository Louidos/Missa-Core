"""Werkzeuge für die Denkschleife (Missa Initiative): einstellen und nachsehen."""

from __future__ import annotations

from collections import Counter
from datetime import time, timedelta
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
from missa.core.zeitangaben import wandzeit


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


def _punkt_zeile(punkt: dict[str, Any]) -> str:
    zeile = f"  • [{punkt.get('aktion')}"
    if punkt.get("aktion") == "melden":
        zeile += f", {punkt.get('stufe')}"
    zeile += f"] {punkt.get('titel') or punkt.get('schluessel')}"
    if punkt.get("grund"):
        zeile += f" – Grund: {punkt['grund']}"
    if punkt.get("ergebnis"):
        zeile += f" ({punkt['ergebnis']})"
    return zeile


def letzte_meldung_text(initiative: Initiative, tage: int = 7) -> str:
    """„Warum hast du mir das geschrieben?“ – Grund der letzten Meldung der Denkschleife."""
    seit = initiative.jetzt() - timedelta(days=tage)
    gemeldet = initiative.laeufe(1, seit, nur_gemeldete=True)
    if not gemeldet:
        return (
            f"In den letzten {tage} Tagen habe ich dir nichts von selbst aus der Denkschleife "
            "geschrieben. (Alarme aus festen Regeln – Server, Erinnerungen – stehen unter "
            "alarme_anzeigen.)"
        )
    lauf = gemeldet[0]
    zeilen = [f"Meine letzte eigene Meldung kam aus dem Lauf vom {lauf.zeit:%d.%m. um %H:%M} Uhr:"]
    zeilen += [_punkt_zeile(p) for p in lauf.punkte if p.get("aktion") == "melden"]
    if lauf.grund:
        zeilen.append(f"Meine Überlegung dazu: {lauf.grund}")
    return "\n".join(zeilen)


def gedanken_text(initiative: Initiative, tage: int = 1) -> str:
    """„Was hast du heute gedacht?“ – die Läufe seit Mitternacht (oder der letzten Tage)."""
    jetzt = initiative.jetzt()
    seit = wandzeit((jetzt - timedelta(days=tage - 1)).date(), time(0, 0), initiative.zone)
    laeufe = list(reversed(initiative.laeufe(1000, seit)))
    zeitraum = "heute" if tage == 1 else f"in den letzten {tage} Tagen"
    if not laeufe:
        aus = "" if initiative.aktiv() else " (die Denkschleife ist aus)"
        return f"Ich habe {zeitraum} noch nicht von selbst nachgedacht{aus}."
    gedacht = [lauf for lauf in laeufe if not lauf.uebersprungen]
    uebersprungen = Counter(lauf.ursache for lauf in laeufe if lauf.uebersprungen)
    zeilen = [f"So habe ich {zeitraum} nachgedacht ({len(laeufe)} Läufe):"]
    for lauf in gedacht[-20:]:
        zeile = f"- {lauf.zeit:%d.%m. %H:%M}: {lauf.aktion}"
        if lauf.fehler:
            zeile += " (Modell nicht erreichbar)"
        elif lauf.grund and not lauf.punkte:
            zeile += f" – {lauf.grund}"
        zeilen.append(zeile)
        zeilen += [_punkt_zeile(p) for p in lauf.punkte]
    if len(gedacht) > 20:
        zeilen.append(f"(… und {len(gedacht) - 20} frühere Läufe)")
    if uebersprungen:
        teile = ", ".join(f"{n}× {ursache}" for ursache, n in uebersprungen.most_common())
        zeilen.append(f"Übersprungen: {teile}.")
    return "\n".join(zeilen)


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

    def gedanken(frage: str = "heute", tage: int = 1) -> str:
        tage = max(1, min(_zahl(tage, "tage"), initiative.journal_tage))
        if str(frage).strip().casefold() in ("warum", "letzte_meldung", "letzte meldung"):
            return letzte_meldung_text(initiative, max(tage, 7))
        return gedanken_text(initiative, tage)

    return [
        Werkzeug(
            "gedanken_anzeigen",
            "Zeigt dein Gedanken-Journal: was du in der Denkschleife von selbst überlegt und "
            "warum du entschieden hast. frage=warum für „Warum hast du mir das geschrieben?“ "
            "(Grund deiner letzten eigenen Meldung), frage=heute für „Was hast du heute "
            "gedacht?“ (mit tage für mehrere Tage).",
            {
                "type": "object",
                "properties": {
                    "frage": {
                        "type": "string",
                        "enum": ["warum", "heute"],
                        "description": "warum = letzte Meldung, heute = alle Läufe",
                    },
                    "tage": {"type": "integer", "description": "Zeitraum in Tagen (Standard 1)"},
                },
            },
            gedanken,
            stufe=Stufe.LESEN,
        ),
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
