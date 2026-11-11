"""Werkzeuge für die Nextcloud-App „Aufgaben“ (CalDAV-VTODO)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
from caldav.lib import error as caldav_fehler

from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen
from missa.tools.nextcloud import (
    NextcloudVerbindung,
    als_ical_text,
    als_zeitpunkt,
    datum_kurz,
    in_zone,
    jetzt_utc,
    komponenten,
    kurz_id,
    neue_uid,
    neuer_kalender,
    passt_zu_id,
    text_feld,
    zeitpunkt_lesen,
)

#: iCalendar-PRIORITY: 1 = höchste, 9 = niedrigste (so zeigt es auch Nextcloud an).
PRIORITAETEN = {"hoch": 1, "mittel": 5, "niedrig": 9}


def prioritaet_lesen(wert: Any) -> int | None:
    """``hoch``/``mittel``/``niedrig`` oder 1–9; leer = keine Priorität."""
    if wert is None or str(wert).strip() == "":
        return None
    text = str(wert).strip().casefold()
    if text in PRIORITAETEN:
        return PRIORITAETEN[text]
    if text.isdigit() and 1 <= int(text) <= 9:
        return int(text)
    raise WerkzeugFehler("prioritaet muss hoch, mittel, niedrig oder eine Zahl von 1 bis 9 sein.")


def prioritaet_text(wert: int) -> str:
    if 1 <= wert <= 4:
        return "hoch"
    if wert == 5:
        return "mittel"
    if 6 <= wert <= 9:
        return "niedrig"
    return ""


def ist_erledigt(komponente: Any) -> bool:
    status = text_feld(komponente, "status").upper()
    return status == "COMPLETED" or komponente.get("completed") is not None


class Aufgaben:
    """Aufgaben in einer Nextcloud-Aufgabenliste (Anzeigename ``name``)."""

    def __init__(
        self,
        verbindung: NextcloudVerbindung,
        name: str,
        zone: ZoneInfo,
        bestaetigungen: Bestaetigungen,
    ) -> None:
        self.verbindung = verbindung
        self.name = name
        self.zone = zone
        self.bestaetigungen = bestaetigungen

    def _liste(self) -> Any:
        return self.verbindung.sammlung(self.name)

    def _faellig(self, komponente: Any) -> date | datetime | None:
        due = komponente.get("due")
        return in_zone(due.dt, self.zone) if due is not None else None

    def _beschreiben(self, komponente: Any) -> tuple[tuple, str]:
        """Sortierschlüssel und eine Zeile Text für eine Aufgabe."""
        erledigt = ist_erledigt(komponente)
        titel = text_feld(komponente, "summary") or "(ohne Titel)"
        zeile = f"- [{'x' if erledigt else ' '}] {titel}"
        faellig = self._faellig(komponente)
        if faellig is not None:
            zeile += f" | fällig: {datum_kurz(faellig)}"
            if isinstance(faellig, datetime):
                zeile += f" {faellig:%H:%M}"
            if not erledigt and als_zeitpunkt(faellig, self.zone) < datetime.now(self.zone):
                zeile += " (überfällig)"
        prioritaet = int(komponente.get("priority") or 0)
        if text := prioritaet_text(prioritaet):
            zeile += f" | Priorität: {text}"
        if notiz := text_feld(komponente, "description"):
            zeile += f" | Notiz: {notiz[:200]}"
        zeile += f" [ID {kurz_id(text_feld(komponente, 'uid'))}]"
        sortierung = (
            erledigt,
            faellig is None,
            als_zeitpunkt(faellig, self.zone) if faellig is not None else datetime.min,
            prioritaet or 10,
            titel.casefold(),
        )
        return sortierung, zeile

    def _alle(self, mit_erledigten: bool) -> list[tuple[Any, Any]]:
        """Alle Aufgaben als Paare (CalDAV-Objekt, VTODO-Komponente)."""
        objekte = self._liste().todos(include_completed=mit_erledigten)
        return [
            (objekt, komponente)
            for objekt in objekte
            for komponente in komponenten(objekt, "VTODO")
        ]

    # ------------------------------------------------------------------ Werkzeuge

    def aufgaben_anzeigen(self, nur_offene: bool = True) -> str:
        with self.verbindung.fehler_abfangen("Aufgaben lesen"):
            paare = self._alle(mit_erledigten=not nur_offene)
        eintraege = sorted(
            (self._beschreiben(komponente) for _, komponente in paare),
            key=lambda eintrag: eintrag[0],
        )
        if not eintraege:
            art = "offenen Aufgaben" if nur_offene else "Aufgaben"
            return f"Keine {art} in der Liste „{self.name}“."
        anzahl = len(eintraege)
        art = ("offene " if nur_offene else "") + ("Aufgabe" if anzahl == 1 else "Aufgaben")
        zeilen = "\n".join(zeile for _, zeile in eintraege)
        return f"{anzahl} {art} in der Liste „{self.name}“:\n{zeilen}"

    def aufgabe_erstellen(
        self,
        titel: str,
        faellig: str | None = None,
        prioritaet: str | int | None = None,
        notiz: str | None = None,
    ) -> str:
        titel = str(titel).strip()
        if not titel:
            raise WerkzeugFehler("Die Aufgabe braucht einen Titel.")
        stufe = prioritaet_lesen(prioritaet)
        termin = zeitpunkt_lesen(faellig, self.zone, "faellig") if faellig else None

        aufgabe = icalendar.Todo()
        aufgabe.add("uid", neue_uid())
        aufgabe.add("dtstamp", jetzt_utc())
        aufgabe.add("created", jetzt_utc())
        aufgabe.add("last-modified", jetzt_utc())
        aufgabe.add("summary", titel)
        aufgabe.add("status", "NEEDS-ACTION")
        if termin is not None:
            aufgabe.add("due", termin)
        if stufe is not None:
            aufgabe.add("priority", stufe)
        if notiz:
            aufgabe.add("description", str(notiz).strip())
        kalender = neuer_kalender()
        kalender.add_component(aufgabe)

        with self.verbindung.fehler_abfangen("Aufgabe erstellen"):
            self._liste().save_todo(als_ical_text(kalender))
        _, zeile = self._beschreiben(aufgabe)
        return f"Neue Aufgabe in der Liste „{self.name}“:\n{zeile}"

    def _finden(self, aufgabe_id: str) -> tuple[Any, Any]:
        """Sucht über UID, kurze ID oder – falls eindeutig – den genauen Titel."""
        gesucht = str(aufgabe_id).strip()
        if not gesucht:
            raise WerkzeugFehler("Bitte eine aufgabe_id angeben.")
        with self.verbindung.fehler_abfangen("Aufgabe suchen"):
            try:
                objekt = self._liste().todo_by_uid(gesucht)
                return objekt, komponenten(objekt, "VTODO")[0]
            except caldav_fehler.NotFoundError:
                pass
            paare = self._alle(mit_erledigten=True)
        for objekt, komponente in paare:
            if passt_zu_id(text_feld(komponente, "uid"), gesucht):
                return objekt, komponente
        gleicher_titel = [
            (objekt, komponente)
            for objekt, komponente in paare
            if text_feld(komponente, "summary").casefold() == gesucht.casefold()
        ]
        offene = [paar for paar in gleicher_titel if not ist_erledigt(paar[1])]
        treffer = offene or gleicher_titel
        if len(treffer) == 1:
            return treffer[0]
        if len(treffer) > 1:
            raise WerkzeugFehler(
                f"Es gibt mehrere Aufgaben „{gesucht}“. Zeig die Liste mit aufgaben_anzeigen "
                "und nimm die ID."
            )
        raise WerkzeugFehler(
            f"Keine Aufgabe mit der ID {gesucht} gefunden. Zeig die Liste mit "
            "aufgaben_anzeigen an und nimm die ID von dort."
        )

    def aufgabe_erledigt(self, aufgabe_id: str) -> str:
        objekt, komponente = self._finden(aufgabe_id)
        _, zeile = self._beschreiben(komponente)
        if ist_erledigt(komponente):
            return f"Diese Aufgabe ist schon erledigt:\n{zeile}"
        with self.verbindung.fehler_abfangen("Aufgabe abhaken"):
            if komponente.get("rrule") is not None:
                # Wiederkehrende Aufgabe: caldav setzt das nächste Fälligkeitsdatum.
                objekt.complete(handle_rrule=True)
            else:
                for feld in ("status", "completed", "percent-complete", "last-modified"):
                    komponente.pop(feld, None)
                komponente.add("status", "COMPLETED")
                komponente.add("completed", jetzt_utc())
                komponente.add("percent-complete", 100)
                komponente.add("last-modified", jetzt_utc())
                objekt.save()
        return f"Abgehakt in der Liste „{self.name}“:\n{zeile.replace('- [ ]', '- [x]', 1)}"

    def aufgabe_loeschen(self, aufgabe_id: str, bestaetigt: bool = False) -> str:
        objekt, komponente = self._finden(aufgabe_id)
        uid = text_feld(komponente, "uid")
        _, zeile = self._beschreiben(komponente)
        sperre = loeschen_pruefen(
            self.bestaetigungen,
            f"aufgabe:{uid}",
            bool(bestaetigt),
            zeile,
            "aufgabe_loeschen",
            f"aufgabe_id={kurz_id(uid)}",
        )
        if sperre:
            return sperre
        with self.verbindung.fehler_abfangen("Aufgabe löschen"):
            objekt.delete()
        self.bestaetigungen.erledigt(f"aufgabe:{uid}")
        return f"Gelöscht aus der Liste „{self.name}“:\n{zeile}"


def aufgaben_werkzeuge(
    verbindung: NextcloudVerbindung,
    name: str,
    zone: ZoneInfo,
    bestaetigungen: Bestaetigungen,
) -> list[Werkzeug]:
    aufgaben = Aufgaben(verbindung, name, zone, bestaetigungen)
    id_beschreibung = "ID der Aufgabe aus aufgaben_anzeigen (oder ihr genauer Titel)"
    return [
        Werkzeug(
            "aufgaben_anzeigen",
            "Zeigt Louis' Aufgabenliste aus Nextcloud (standardmäßig nur offene Aufgaben).",
            {
                "type": "object",
                "properties": {
                    "nur_offene": {
                        "type": "boolean",
                        "description": "false, um auch erledigte Aufgaben zu zeigen",
                    }
                },
            },
            aufgaben.aufgaben_anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "aufgabe_erstellen",
            "Legt eine neue Aufgabe in Louis' Nextcloud-Aufgabenliste an.",
            {
                "type": "object",
                "properties": {
                    "titel": {"type": "string", "description": "Was zu tun ist"},
                    "faellig": {
                        "type": "string",
                        "description": "Fällig am, ISO-Format (2026-11-13 oder "
                        "2026-11-13T17:00), optional",
                    },
                    "prioritaet": {
                        "type": "string",
                        "enum": ["hoch", "mittel", "niedrig"],
                        "description": "Priorität, optional",
                    },
                    "notiz": {"type": "string", "description": "Notiz, optional"},
                },
                "required": ["titel"],
            },
            aufgaben.aufgabe_erstellen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "aufgabe_erledigt",
            "Hakt eine Aufgabe in Nextcloud als erledigt ab.",
            {
                "type": "object",
                "properties": {"aufgabe_id": {"type": "string", "description": id_beschreibung}},
                "required": ["aufgabe_id"],
            },
            aufgaben.aufgabe_erledigt,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "aufgabe_loeschen",
            "Löscht eine Aufgabe. Zuerst ohne bestaetigt aufrufen: dann wird nur gemeldet, "
            "was gelöscht würde. Frag Louis und rufe erst nach seinem ausdrücklichen Ja "
            "nochmals mit bestaetigt=true auf.",
            {
                "type": "object",
                "properties": {
                    "aufgabe_id": {"type": "string", "description": id_beschreibung},
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true, wenn Louis das Löschen bestätigt hat",
                    },
                },
                "required": ["aufgabe_id"],
            },
            aufgaben.aufgabe_loeschen,
            stufe=Stufe.ZERSTOERERISCH,
        ),
    ]
