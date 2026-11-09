"""Werkzeuge für den Nextcloud-Kalender: Termine anzeigen, eintragen, löschen."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
from caldav.lib import error as caldav_fehler

from missa.core.prompt import datum_lang
from missa.core.werkzeuge import Werkzeug, WerkzeugFehler
from missa.tools.bestaetigung import Bestaetigungen, loeschen_pruefen
from missa.tools.nextcloud import (
    NextcloudVerbindung,
    als_ical_text,
    als_zeitpunkt,
    datum_kurz,
    ende_berechnen,
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

#: Längster Zeitraum, den termine_anzeigen auf einmal liefert.
MAX_TAGE = 366
#: Zeitfenster, in dem termin_loeschen einen Termin über seine kurze ID sucht.
SUCHFENSTER_ZURUECK = timedelta(days=366)
SUCHFENSTER_VOR = timedelta(days=2 * 366)


def zeitraum_text(start: date | datetime, ende: date | datetime) -> str:
    """z. B. ``Fr 13.11.2026 14:00–15:00`` oder ``Fr 13.11.2026 (ganztägig)``."""
    if not isinstance(start, datetime):
        letzter = ende - timedelta(days=1) if not isinstance(ende, datetime) else ende.date()
        if letzter <= start:
            return f"{datum_kurz(start)} (ganztägig)"
        return f"{datum_kurz(start)} – {datum_kurz(letzter)} (ganztägig)"
    if not isinstance(ende, datetime) or ende <= start:
        return f"{datum_kurz(start)} {start:%H:%M}"
    if ende.date() == start.date():
        return f"{datum_kurz(start)} {start:%H:%M}–{ende:%H:%M}"
    return f"{datum_kurz(start)} {start:%H:%M} – {datum_kurz(ende.date())} {ende:%H:%M}"


class Kalender:
    """Termine in einem Nextcloud-Kalender (Anzeigename ``name``)."""

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

    def _kalender(self) -> Any:
        return self.verbindung.sammlung(self.name)

    def _beschreiben(self, komponente: Any) -> tuple[datetime, str]:
        """Sortierschlüssel und eine Zeile Text für einen Termin."""
        start = in_zone(komponente["dtstart"].dt, self.zone)
        ende = in_zone(ende_berechnen(komponente, komponente["dtstart"].dt), self.zone)
        zeile = (
            f"- {zeitraum_text(start, ende)}: {text_feld(komponente, 'summary') or '(ohne Titel)'}"
        )
        if ort := text_feld(komponente, "location"):
            zeile += f" | Ort: {ort}"
        if notiz := text_feld(komponente, "description"):
            zeile += f" | Notiz: {notiz[:200]}"
        if komponente.get("recurrence-id") is not None or komponente.get("rrule") is not None:
            zeile += " | wiederkehrend"
        zeile += f" [ID {kurz_id(text_feld(komponente, 'uid'))}]"
        return als_zeitpunkt(start, self.zone), zeile

    # ------------------------------------------------------------------ Werkzeuge

    def termine_anzeigen(self, von: str, bis: str | None = None) -> str:
        anfang = zeitpunkt_lesen(von, self.zone, "von")
        schluss = zeitpunkt_lesen(bis, self.zone, "bis") if bis else anfang
        start = als_zeitpunkt(anfang, self.zone)
        # Ein Datum bei "bis" meint den ganzen Tag.
        ende = (
            als_zeitpunkt(schluss, self.zone)
            if isinstance(schluss, datetime)
            else als_zeitpunkt(schluss + timedelta(days=1), self.zone)
        )
        if ende <= start:
            raise WerkzeugFehler("„bis“ muss nach „von“ liegen.")
        if ende - start > timedelta(days=MAX_TAGE):
            raise WerkzeugFehler(f"Bitte höchstens {MAX_TAGE} Tage auf einmal abfragen.")

        with self.verbindung.fehler_abfangen("Termine lesen"):
            gefunden = self._kalender().search(start=start, end=ende, event=True, expand=True)
            eintraege = [
                self._beschreiben(komponente)
                for objekt in gefunden
                for komponente in komponenten(objekt, "VEVENT")
                if komponente.get("dtstart") is not None
            ]

        zeitraum = datum_lang(start.date())
        letzter_tag = (ende - timedelta(microseconds=1)).date()
        if letzter_tag != start.date():
            zeitraum += f" bis {datum_lang(letzter_tag)}"
        if not eintraege:
            return f"Keine Termine im Kalender „{self.name}“ für {zeitraum}."
        eintraege.sort(key=lambda eintrag: eintrag[0])
        zeilen = "\n".join(zeile for _, zeile in eintraege)
        return f"Termine im Kalender „{self.name}“ für {zeitraum}:\n{zeilen}"

    def termin_eintragen(
        self,
        titel: str,
        start: str,
        ende: str | None = None,
        ort: str | None = None,
        notiz: str | None = None,
    ) -> str:
        titel = str(titel).strip()
        if not titel:
            raise WerkzeugFehler("Der Termin braucht einen Titel.")
        beginn = zeitpunkt_lesen(start, self.zone, "start")
        if ende:
            schluss = zeitpunkt_lesen(ende, self.zone, "ende")
        elif isinstance(beginn, datetime):
            schluss = beginn + timedelta(hours=1)
        else:
            schluss = beginn + timedelta(days=1)
        if isinstance(beginn, datetime) != isinstance(schluss, datetime):
            raise WerkzeugFehler(
                "start und ende müssen beide ein Datum (ganztägig) "
                "oder beide Datum mit Uhrzeit sein."
            )
        if not isinstance(beginn, datetime) and schluss == beginn:
            schluss = beginn + timedelta(days=1)  # Ganztägig: Ende ist exklusiv.
        if schluss <= beginn:
            raise WerkzeugFehler("Das Ende liegt vor dem Beginn.")

        termin = icalendar.Event()
        uid = neue_uid()
        termin.add("uid", uid)
        termin.add("dtstamp", jetzt_utc())
        termin.add("created", jetzt_utc())
        termin.add("summary", titel)
        termin.add("dtstart", beginn)
        termin.add("dtend", schluss)
        if ort:
            termin.add("location", str(ort).strip())
        if notiz:
            termin.add("description", str(notiz).strip())
        kalender = neuer_kalender()
        kalender.add_component(termin)

        with self.verbindung.fehler_abfangen("Termin eintragen"):
            self._kalender().save_event(als_ical_text(kalender))
        _, zeile = self._beschreiben(termin)
        return f"Eingetragen im Kalender „{self.name}“:\n{zeile}"

    def _termin_finden(self, termin_id: str) -> tuple[Any, Any]:
        """Sucht einen Termin über UID oder kurze ID; gibt (Objekt, Komponente) zurück."""
        termin_id = str(termin_id).strip()
        if not termin_id:
            raise WerkzeugFehler("Bitte eine termin_id angeben.")
        with self.verbindung.fehler_abfangen("Termin suchen"):
            kalender = self._kalender()
            try:
                objekt = kalender.event_by_uid(termin_id)
            except caldav_fehler.NotFoundError:
                objekt = None
            if objekt is None:
                jetzt = datetime.now(self.zone)
                kandidaten = kalender.search(
                    start=jetzt - SUCHFENSTER_ZURUECK, end=jetzt + SUCHFENSTER_VOR, event=True
                )
                objekt = next(
                    (
                        k
                        for k in kandidaten
                        if any(
                            passt_zu_id(text_feld(c, "uid"), termin_id)
                            for c in komponenten(k, "VEVENT")
                        )
                    ),
                    None,
                )
        if objekt is None:
            raise WerkzeugFehler(
                f"Keinen Termin mit der ID {termin_id} gefunden. Zeig die Termine mit "
                "termine_anzeigen an und nimm die ID von dort."
            )
        haupt = next(
            (c for c in komponenten(objekt, "VEVENT") if c.get("recurrence-id") is None),
            komponenten(objekt, "VEVENT")[0],
        )
        return objekt, haupt

    def termin_loeschen(self, termin_id: str, bestaetigt: bool = False) -> str:
        objekt, komponente = self._termin_finden(termin_id)
        uid = text_feld(komponente, "uid")
        _, zeile = self._beschreiben(komponente)
        if komponente.get("rrule") is not None:
            zeile += "\nAchtung: wiederkehrender Termin – es werden alle Wiederholungen gelöscht."
        sperre = loeschen_pruefen(
            self.bestaetigungen,
            f"termin:{uid}",
            bool(bestaetigt),
            zeile,
            "termin_loeschen",
            f"termin_id={kurz_id(uid)}",
        )
        if sperre:
            return sperre
        with self.verbindung.fehler_abfangen("Termin löschen"):
            objekt.delete()
        self.bestaetigungen.erledigt(f"termin:{uid}")
        return f"Gelöscht aus dem Kalender „{self.name}“:\n{zeile}"


def kalender_werkzeuge(
    verbindung: NextcloudVerbindung,
    name: str,
    zone: ZoneInfo,
    bestaetigungen: Bestaetigungen,
) -> list[Werkzeug]:
    kalender = Kalender(verbindung, name, zone, bestaetigungen)
    zeitangabe = "ISO-Format, Zeitzone Europe/Zurich"
    return [
        Werkzeug(
            "termine_anzeigen",
            "Zeigt Louis' Termine aus dem Nextcloud-Kalender in einem Zeitraum "
            "(wiederkehrende Termine einzeln). Für einen einzelnen Tag nur 'von' angeben.",
            {
                "type": "object",
                "properties": {
                    "von": {
                        "type": "string",
                        "description": (
                            f"Erster Tag oder Zeitpunkt ({zeitangabe}), z. B. 2026-11-10"
                        ),
                    },
                    "bis": {
                        "type": "string",
                        "description": "Letzter Tag (einschließlich) oder Zeitpunkt; "
                        "weglassen für nur einen Tag",
                    },
                },
                "required": ["von"],
            },
            kalender.termine_anzeigen,
        ),
        Werkzeug(
            "termin_eintragen",
            "Trägt einen Termin in Louis' Nextcloud-Kalender ein. Ohne Ende dauert er eine "
            "Stunde; nur ein Datum ohne Uhrzeit ergibt einen ganztägigen Termin.",
            {
                "type": "object",
                "properties": {
                    "titel": {"type": "string", "description": "Titel, z. B. Zahnarzt"},
                    "start": {
                        "type": "string",
                        "description": f"Beginn ({zeitangabe}), z. B. 2026-11-13T14:00",
                    },
                    "ende": {"type": "string", "description": f"Ende ({zeitangabe}), optional"},
                    "ort": {"type": "string", "description": "Ort, optional"},
                    "notiz": {"type": "string", "description": "Notiz, optional"},
                },
                "required": ["titel", "start"],
            },
            kalender.termin_eintragen,
        ),
        Werkzeug(
            "termin_loeschen",
            "Löscht einen Termin. Zuerst ohne bestaetigt aufrufen: dann wird nur gemeldet, "
            "was gelöscht würde. Frag Louis und rufe erst nach seinem ausdrücklichen Ja "
            "nochmals mit bestaetigt=true auf.",
            {
                "type": "object",
                "properties": {
                    "termin_id": {
                        "type": "string",
                        "description": "ID des Termins aus termine_anzeigen",
                    },
                    "bestaetigt": {
                        "type": "boolean",
                        "description": "Nur true, wenn Louis das Löschen bestätigt hat",
                    },
                },
                "required": ["termin_id"],
            },
            kalender.termin_loeschen,
        ),
    ]
