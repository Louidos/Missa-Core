"""Werkzeuge für den Nextcloud-Kalender: volle Kontrolle für Missa.

Missa darf im Kalender alles ohne Rückfrage: Termine anlegen, ändern,
verschieben und löschen, Farben (``COLOR``), Kategorien, Erinnerungen
(``VALARM``), Wiederholungen (``RRULE`` mit einzelnen Ausnahmen), ganztägige
Termine und eigene Kalender (anlegen, umbenennen, einfärben).

Dafür gilt:

- **Protokoll:** Jede Änderung wird mit Zustand vorher/nachher gespeichert und
  lässt sich mit ``kalender_rueckgaengig`` zurücknehmen.
- **Einladungen nur nach Rückfrage:** Sobald andere Personen (Teilnehmer) betroffen
  sind, verschickt Nextcloud E-Mails an sie. Dann fragt Missa vorher (gleiche
  Regel wie bisher beim Löschen: Zustimmung erst in Louis' nächster Nachricht).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
from caldav.lib import error as caldav_fehler

from missa.core.prompt import datum_lang
from missa.core.werkzeuge import Werkzeug, WerkzeugFehler
from missa.tools.bestaetigung import Bestaetigungen, zustimmung_pruefen
from missa.tools.farben import Farbsystem, farbe_lesen, farbe_name
from missa.tools.kalender_protokoll import KalenderProtokoll
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
#: Zeitfenster, in dem ein Termin über seine kurze ID gesucht wird.
SUCHFENSTER_ZURUECK = timedelta(days=366)
SUCHFENSTER_VOR = timedelta(days=2 * 366)
#: Werte, die „entfernen“ bedeuten.
LEER = frozenset({"", "keine", "keiner", "kein", "nein", "ohne", "entfernen", "none", "-"})
WOCHENTAG_KUERZEL = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
WOCHENTAG_NAMEN = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
#: Änderungen an diesen Feldern lösen bei Terminen mit Teilnehmern keine E-Mails aus.
KOSMETISCH = frozenset({"farbe", "kategorien", "erinnerungen"})

#: Wird nach jeder Terminänderung mit dem betroffenen Tag aufgerufen (z. B. Pausen-Prüfung).
NachPruefung = Callable[[date], str | None]


@dataclass(frozen=True)
class Belegung:
    """Ein Termin als Zeitblock (für freie Zeiten, Puffer und Pausen)."""

    start: datetime
    ende: datetime
    titel: str
    ort: str
    kalender: str
    uid: str
    ganztaegig: bool
    frei: bool  # TRANSP:TRANSPARENT – blockiert keine Zeit
    art: str  # X-MISSA-ART bei Blöcken, die Missa selbst eingetragen hat


def freie_luecke(
    belegt: list[Belegung], von: datetime, bis: datetime, dauer: timedelta
) -> datetime | None:
    """Beginn der ersten freien Lücke von mindestens ``dauer`` zwischen ``von`` und ``bis``."""
    zeiger = von
    for block in sorted(
        (b for b in belegt if not b.frei and not b.ganztaegig), key=lambda b: b.start
    ):
        if block.ende <= zeiger:
            continue
        if block.start >= bis:
            break
        if block.start - zeiger >= dauer:
            return zeiger
        zeiger = max(zeiger, block.ende)
    return zeiger if bis - zeiger >= dauer else None


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


# ---------------------------------------------------------------------- Eingaben lesen


def _liste(wert: Any) -> list[str]:
    if wert is None:
        return []
    if isinstance(wert, list | tuple):
        teile = [str(t) for t in wert]
    else:
        teile = re.split(r"[,;]|\bund\b", str(wert))
    return [t.strip() for t in teile if t.strip()]


def _ist_leer(wert: Any) -> bool:
    if isinstance(wert, list | tuple):
        return not wert or all(str(t).strip().casefold() in LEER for t in wert)
    return str(wert).strip().casefold() in LEER


def erinnerungen_lesen(wert: Any) -> list[int]:
    """Minuten vor Beginn: ``15``, ``[15, 60]``, ``"15, 60"``, ``"1 Tag"``, ``"2 Stunden"``."""
    if isinstance(wert, int | float):
        return [int(wert)]
    minuten = []
    for teil in _liste(wert):
        roh = teil.casefold().replace("vorher", "").strip()
        if treffer := re.fullmatch(r"(\d+)\s*(min\w*|m)?", roh):
            minuten.append(int(treffer.group(1)))
        elif treffer := re.fullmatch(r"(\d+|eine?r?)\s*(h|std|stunden?)", roh):
            zahl = 1 if not treffer.group(1).isdigit() else int(treffer.group(1))
            minuten.append(zahl * 60)
        elif treffer := re.fullmatch(r"(\d+|eine?n?)\s*(tag|tage|tagen)", roh):
            zahl = 1 if not treffer.group(1).isdigit() else int(treffer.group(1))
            minuten.append(zahl * 1440)
        elif treffer := re.fullmatch(r"(\d+|eine?r?)\s*(woche|wochen)", roh):
            zahl = 1 if not treffer.group(1).isdigit() else int(treffer.group(1))
            minuten.append(zahl * 10080)
        else:
            raise WerkzeugFehler(
                f"Die Erinnerung „{teil}“ verstehe ich nicht. Bitte Minuten vor Beginn angeben, "
                "z. B. 15 oder „1 Tag“."
            )
    if any(m < 0 for m in minuten):
        raise WerkzeugFehler("Erinnerungen müssen vor dem Termin liegen.")
    return sorted(set(minuten))


def _ende_des_tages_utc(tag: date, zone: ZoneInfo) -> datetime:
    return datetime(tag.year, tag.month, tag.day, 23, 59, 59, tzinfo=zone).astimezone(
        ZoneInfo("UTC")
    )


def rrule_lesen(text: str, beginn: date | datetime, zone: ZoneInfo) -> dict[str, Any]:
    """Liest eine Wiederholung: RRULE-Text oder Deutsch.

    Beispiele: ``FREQ=WEEKLY;BYDAY=MO``, „täglich“, „werktags“, „wöchentlich“,
    „alle 2 Wochen“, „jeden Montag und Donnerstag“, „monatlich“, „jährlich“,
    jeweils optional mit „bis 31.12.2026“ oder „10 mal“.
    """
    roh = str(text).strip()
    if roh.upper().startswith(("FREQ=", "RRULE:")):
        try:
            return dict(icalendar.vRecur.from_ical(roh.removeprefix("RRULE:")))
        except ValueError as fehler:
            raise WerkzeugFehler(f"Die Regel „{roh}“ ist keine gültige RRULE.") from fehler

    klein = roh.casefold()
    regel: dict[str, Any] = {}
    if treffer := re.search(
        r"\bbis\s+(?:zum\s+)?(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{2,4})", klein
    ):
        wert = treffer.group(1)
        if "-" in wert:
            bis = date.fromisoformat(wert)
        else:
            tag, monat, jahr = (int(t) for t in wert.split("."))
            bis = date(jahr + 2000 if jahr < 100 else jahr, monat, tag)
        regel["UNTIL"] = bis if not isinstance(beginn, datetime) else _ende_des_tages_utc(bis, zone)
        klein = klein[: treffer.start()] + klein[treffer.end() :]
    if treffer := re.search(r"\b(\d+)\s*(?:mal|x|-mal)\b", klein):
        regel["COUNT"] = int(treffer.group(1))
        klein = klein[: treffer.start()] + klein[treffer.end() :]

    intervall = 1
    if treffer := re.search(r"\b(?:alle|jede[nrs]?)\s+(\d+|zwei|drei|vier)\.?\s*", klein):
        zahl = treffer.group(1)
        intervall = {"zwei": 2, "drei": 3, "vier": 4}.get(zahl) or int(zahl)

    tage = [i for i, name in enumerate(WOCHENTAG_NAMEN) if re.search(rf"\b{name}s?\b", klein)]
    if re.search(r"werktag|wochentag(?!s?\s*\w)|montag\s+bis\s+freitag", klein):
        regel.update(FREQ="WEEKLY", BYDAY=WOCHENTAG_KUERZEL[:5])
    elif tage:
        regel.update(FREQ="WEEKLY", BYDAY=[WOCHENTAG_KUERZEL[t] for t in tage])
    elif re.search(r"täglich|taeglich|\btag(e|en)?\b", klein):
        regel["FREQ"] = "DAILY"
    elif re.search(r"wöchentlich|woechentlich|\bwoche[n]?\b", klein):
        regel["FREQ"] = "WEEKLY"
    elif re.search(r"monatlich|\bmonat(e|en)?\b", klein):
        regel["FREQ"] = "MONTHLY"
    elif re.search(r"jährlich|jaehrlich|\bjahr(e|en)?\b", klein):
        regel["FREQ"] = "YEARLY"
    else:
        raise WerkzeugFehler(
            f"Die Wiederholung „{roh}“ verstehe ich nicht. Beispiele: täglich, werktags, "
            "wöchentlich, alle 2 Wochen, jeden Montag, monatlich, jährlich (optional „bis "
            "31.12.2026“ oder „10 mal“) oder eine RRULE wie FREQ=WEEKLY;BYDAY=MO."
        )
    if intervall > 1:
        regel["INTERVAL"] = intervall
    return regel


def rrule_text(regel: Any) -> str:
    """Kurze deutsche Beschreibung einer RRULE."""
    if regel is None:
        return ""
    werte = {k.upper(): v for k, v in dict(regel).items()}

    def erster(name: str) -> Any:
        wert = werte.get(name)
        return wert[0] if isinstance(wert, list) and wert else wert

    freq = str(erster("FREQ") or "").upper()
    intervall = int(erster("INTERVAL") or 1)
    namen = {"DAILY": "Tag", "WEEKLY": "Woche", "MONTHLY": "Monat", "YEARLY": "Jahr"}
    if intervall > 1:
        text = f"alle {intervall} {namen.get(freq, freq)}{'e' if freq != 'WEEKLY' else 'n'}"
    else:
        text = {
            "DAILY": "täglich",
            "WEEKLY": "wöchentlich",
            "MONTHLY": "monatlich",
            "YEARLY": "jährlich",
        }.get(freq, freq.lower())
    if tage := werte.get("BYDAY"):
        kuerzel = [str(t)[-2:] for t in (tage if isinstance(tage, list) else [tage])]
        if kuerzel == WOCHENTAG_KUERZEL[:5]:
            text = "werktags"
        else:
            text += " am " + ", ".join(
                WOCHENTAG_NAMEN[WOCHENTAG_KUERZEL.index(k)].capitalize()
                for k in kuerzel
                if k in WOCHENTAG_KUERZEL
            )
    if bis := erster("UNTIL"):
        text += f" bis {bis:%d.%m.%Y}"
    if anzahl := erster("COUNT"):
        text += f", {anzahl}-mal"
    return text


def _adresse(wert: str) -> str:
    adresse = wert.strip().removeprefix("mailto:").removeprefix("MAILTO:").strip("<> ")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", adresse):
        raise WerkzeugFehler(
            f"„{wert}“ ist keine E-Mail-Adresse. Für Einladungen brauche ich die Adresse."
        )
    return adresse.lower()


def _teilnehmer(komponente: Any) -> list[str]:
    roh = komponente.get("attendee")
    if roh is None:
        return []
    werte = roh if isinstance(roh, list) else [roh]
    return [str(w).removeprefix("mailto:").removeprefix("MAILTO:") for w in werte]


# ---------------------------------------------------------------------- Kalender


class Kalender:
    """Termine in Nextcloud-Kalendern; ``name`` ist der Standardkalender."""

    def __init__(
        self,
        verbindung: NextcloudVerbindung,
        name: str,
        zone: ZoneInfo,
        bestaetigungen: Bestaetigungen,
        protokoll: KalenderProtokoll | None = None,
        farbsystem: Farbsystem | None = None,
    ) -> None:
        self.verbindung = verbindung
        self.name = name
        self.zone = zone
        self.bestaetigungen = bestaetigungen
        self.protokoll = protokoll
        self.farbsystem = farbsystem
        self.nach_pruefungen: list[NachPruefung] = []

    # -------------------------------------------------------------- Zugriff

    def _kalender(self, name: str | None = None) -> Any:
        return self.verbindung.sammlung(name or self.name)

    def _name(self, name: str | None) -> str:
        """Anzeigename, wie er in Nextcloud steht."""
        if not name:
            return self.name
        gesucht = name.strip().casefold()
        for vorhanden, _ in self.verbindung.alle_sammlungen():
            if vorhanden.casefold() == gesucht:
                return vorhanden
        self._kalender(name)  # wirft eine verständliche Meldung
        return name

    def _anzeige_kalender(self, kalender: str | None) -> list[tuple[str, Any]]:
        if kalender and kalender.strip().casefold() in ("alle", "*", "alles"):
            return [(n, k) for n, k in self.verbindung.alle_sammlungen() if _hat_termine(k)]
        name = self._name(kalender)
        return [(name, self._kalender(name))]

    def _protokollieren(self, *args: Any, **kwargs: Any) -> str:
        if self.protokoll is None:
            return ""
        aenderung = self.protokoll.eintragen(*args, **kwargs)
        return f"\n(Änderung #{aenderung.id} – rückgängig mit kalender_rueckgaengig)"

    def _nachpruefen(self, *tage: date | datetime) -> str:
        hinweise = []
        for tag in dict.fromkeys(t.date() if isinstance(t, datetime) else t for t in tage):
            for pruefung in self.nach_pruefungen:
                try:
                    if text := pruefung(tag):
                        hinweise.append(text)
                except Exception:  # Hinweise dürfen die Änderung nie scheitern lassen
                    continue
        return ("\n\n" + "\n".join(hinweise)) if hinweise else ""

    # -------------------------------------------------------------- Anzeige

    def _beschreiben(self, komponente: Any, kalender: str | None = None) -> tuple[datetime, str]:
        """Sortierschlüssel und eine Zeile Text für einen Termin."""
        start = in_zone(komponente["dtstart"].dt, self.zone)
        ende = in_zone(ende_berechnen(komponente, komponente["dtstart"].dt), self.zone)
        zeile = (
            f"- {zeitraum_text(start, ende)}: {text_feld(komponente, 'summary') or '(ohne Titel)'}"
        )
        if kalender:
            zeile += f" | Kalender: {kalender}"
        if ort := text_feld(komponente, "location"):
            zeile += f" | Ort: {ort}"
        if notiz := text_feld(komponente, "description"):
            zeile += f" | Notiz: {notiz[:200]}"
        if farbe := text_feld(komponente, "color"):
            zeile += f" | Farbe: {farbe_name(farbe)}"
        if kategorien := _kategorien(komponente):
            zeile += f" | Kategorien: {', '.join(kategorien)}"
        if alarme := _alarm_minuten(komponente):
            zeile += " | Erinnerung: " + ", ".join(_minuten_text(m) for m in alarme)
        if komponente.get("rrule") is not None:
            zeile += f" | wiederkehrend ({rrule_text(komponente['rrule'])})"
        elif komponente.get("recurrence-id") is not None:
            zeile += " | wiederkehrend"
        if teilnehmer := _teilnehmer(komponente):
            zeile += f" | Teilnehmer: {', '.join(teilnehmer)}"
        zeile += f" [ID {kurz_id(text_feld(komponente, 'uid'))}]"
        return als_zeitpunkt(start, self.zone), zeile

    def termine_anzeigen(
        self, von: str, bis: str | None = None, kalender: str | None = None
    ) -> str:
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

        auswahl = self._anzeige_kalender(kalender)
        mehrere = len(auswahl) > 1
        eintraege = []
        with self.verbindung.fehler_abfangen("Termine lesen"):
            for name, sammlung in auswahl:
                for objekt in sammlung.search(start=start, end=ende, event=True, expand=True):
                    for komponente in komponenten(objekt, "VEVENT"):
                        if komponente.get("dtstart") is not None:
                            eintraege.append(
                                self._beschreiben(komponente, name if mehrere else None)
                            )

        namen = "in allen Kalendern" if mehrere else f"im Kalender „{auswahl[0][0]}“"
        zeitraum = datum_lang(start.date())
        letzter_tag = (ende - timedelta(microseconds=1)).date()
        if letzter_tag != start.date():
            zeitraum += f" bis {datum_lang(letzter_tag)}"
        if not eintraege:
            return f"Keine Termine {namen} für {zeitraum}."
        eintraege.sort(key=lambda eintrag: eintrag[0])
        zeilen = "\n".join(zeile for _, zeile in eintraege)
        return f"Termine {namen} für {zeitraum}:\n{zeilen}"

    def belegungen(
        self, start: datetime, ende: datetime, kalender: list[str] | None = None
    ) -> list[Belegung]:
        """Alle Termine aus allen (oder den genannten) Kalendern als Zeitblöcke."""
        gesucht = {k.casefold() for k in kalender} if kalender else None
        ergebnis = []
        with self.verbindung.fehler_abfangen("Termine lesen"):
            for name, sammlung in self.verbindung.alle_sammlungen():
                if not _hat_termine(sammlung) or (gesucht and name.casefold() not in gesucht):
                    continue
                for objekt in sammlung.search(start=start, end=ende, event=True, expand=True):
                    for komponente in komponenten(objekt, "VEVENT"):
                        if komponente.get("dtstart") is None:
                            continue
                        roh = komponente["dtstart"].dt
                        ergebnis.append(
                            Belegung(
                                start=als_zeitpunkt(roh, self.zone),
                                ende=als_zeitpunkt(ende_berechnen(komponente, roh), self.zone),
                                titel=text_feld(komponente, "summary"),
                                ort=text_feld(komponente, "location"),
                                kalender=name,
                                uid=text_feld(komponente, "uid"),
                                ganztaegig=not isinstance(roh, datetime),
                                frei=text_feld(komponente, "transp").upper() == "TRANSPARENT",
                                art=text_feld(komponente, "x-missa-art"),
                            )
                        )
        ergebnis.sort(key=lambda b: (b.start, b.ende))
        return ergebnis

    # -------------------------------------------------------------- Eigenschaften setzen

    def _zeiten(
        self,
        start: str,
        ende: str | None,
        ganztaegig: bool | None,
        dauer: timedelta | None = None,
    ) -> tuple[date | datetime, date | datetime]:
        beginn = zeitpunkt_lesen(start, self.zone, "start")
        if ganztaegig and isinstance(beginn, datetime):
            beginn = beginn.date()
        if ende:
            schluss = zeitpunkt_lesen(ende, self.zone, "ende")
            if ganztaegig and isinstance(schluss, datetime):
                schluss = schluss.date() + timedelta(days=1)
        elif dauer is not None and isinstance(beginn, datetime) and dauer.days < 1:
            schluss = beginn + dauer
        elif dauer is not None and not isinstance(beginn, datetime):
            schluss = beginn + timedelta(days=max(1, dauer.days))
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
        return beginn, schluss

    def _farbe_setzen(self, komponente: Any, farbe: Any) -> None:
        if "color" in komponente:
            del komponente["color"]
        if farbe is not None and not _ist_leer(farbe):
            css, _ = farbe_lesen(str(farbe))
            komponente.add("color", css)

    def _kategorien_setzen(self, komponente: Any, kategorien: Any) -> None:
        if "categories" in komponente:
            del komponente["categories"]
        if kategorien is not None and not _ist_leer(kategorien):
            komponente.add("categories", _liste(kategorien))

    def _alarme_setzen(self, komponente: Any, erinnerungen: Any) -> None:
        komponente.subcomponents = [
            c for c in komponente.subcomponents if getattr(c, "name", "") != "VALARM"
        ]
        if erinnerungen is None or _ist_leer(erinnerungen):
            return
        titel = text_feld(komponente, "summary") or "Termin"
        for minuten in erinnerungen_lesen(erinnerungen):
            alarm = icalendar.Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", titel)
            alarm.add("trigger", timedelta(minutes=-minuten))
            komponente.add_component(alarm)

    def _wiederholung_setzen(self, komponente: Any, wiederholung: Any) -> None:
        for name in ("rrule", "exdate", "rdate"):
            if name in komponente:
                del komponente[name]
        if wiederholung is not None and not _ist_leer(wiederholung):
            komponente.add(
                "rrule", rrule_lesen(str(wiederholung), komponente["dtstart"].dt, self.zone)
            )

    def _teilnehmer_setzen(self, komponente: Any, teilnehmer: list[str]) -> None:
        for name in ("attendee", "organizer"):
            if name in komponente:
                del komponente[name]
        if not teilnehmer:
            return
        if organisator := self.verbindung.eigene_adresse():
            komponente.add("organizer", f"mailto:{organisator}")
        for adresse in teilnehmer:
            komponente.add(
                "attendee",
                f"mailto:{adresse}",
                parameters={"ROLE": "REQ-PARTICIPANT", "PARTSTAT": "NEEDS-ACTION", "RSVP": "TRUE"},
            )

    def _einladung_pruefen(
        self,
        art: str,
        uid: str,
        beschreibung: str,
        teilnehmer: list[str],
        bestaetigt: bool,
        werkzeug: str,
    ) -> str | None:
        # Die Zustimmung gilt genau für diese Änderung (ohne die zufällige ID neuer Termine).
        inhalt = re.sub(r"\s*\[ID \w+\]", "", beschreibung)
        pruefsumme = hashlib.sha1(f"{art}|{uid}|{inhalt}|{','.join(sorted(teilnehmer))}".encode())
        schluessel = "einladung:" + pruefsumme.hexdigest()
        return zustimmung_pruefen(
            self.bestaetigungen,
            schluessel,
            bestaetigt,
            f"{beschreibung}\nBetroffene Personen (bekommen eine E-Mail von Nextcloud): "
            f"{', '.join(teilnehmer)}",
            werkzeug,
            "gleiche Parameter",
            "Dabei bekommen andere Personen eine E-Mail. Frag Louis zuerst, ob das so "
            "rausgehen soll:",
        )

    # -------------------------------------------------------------- Termine

    def termin_eintragen(
        self,
        titel: str,
        start: str,
        ende: str | None = None,
        ort: str | None = None,
        notiz: str | None = None,
        kalender: str | None = None,
        farbe: str | None = None,
        kategorien: Any = None,
        erinnerungen: Any = None,
        wiederholung: str | None = None,
        ganztaegig: bool | None = None,
        teilnehmer: Any = None,
        bestaetigt: bool = False,
    ) -> str:
        titel = str(titel).strip()
        if not titel:
            raise WerkzeugFehler("Der Termin braucht einen Titel.")
        beginn, schluss = self._zeiten(start, ende, ganztaegig)
        name = self._name(kalender)
        liste_kategorien = [] if kategorien is None or _ist_leer(kategorien) else _liste(kategorien)
        if farbe is None and liste_kategorien and self.farbsystem is not None:
            farbe = self.farbsystem.farbe_fuer(liste_kategorien)
        adressen = [_adresse(t) for t in _liste(teilnehmer)]

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
        self._farbe_setzen(termin, farbe)
        self._kategorien_setzen(termin, liste_kategorien)
        self._alarme_setzen(termin, erinnerungen)
        if wiederholung:
            self._wiederholung_setzen(termin, wiederholung)
        _, zeile = self._beschreiben(termin)

        if adressen:
            sperre = self._einladung_pruefen(
                "neu", f"{titel}|{beginn}", zeile, adressen, bool(bestaetigt), "termin_eintragen"
            )
            if sperre:
                return sperre
            self._teilnehmer_setzen(termin, adressen)
            _, zeile = self._beschreiben(termin)

        ical_kalender = neuer_kalender()
        ical_kalender.add_component(termin)
        text = als_ical_text(ical_kalender)
        with self.verbindung.fehler_abfangen("Termin eintragen"):
            self._kalender(name).save_event(text)
        hinweis = self._protokollieren("termin_neu", name, zeile[2:], nachher=text, uid=uid)
        return f"Eingetragen im Kalender „{name}“:\n{zeile}{hinweis}" + self._nachpruefen(beginn)

    def _suchen_in(self, sammlung: Any, termin_id: str) -> Any:
        try:
            objekt = sammlung.event_by_uid(termin_id)
        except caldav_fehler.NotFoundError:
            objekt = None
        if objekt is not None:
            return objekt
        jetzt = datetime.now(self.zone)
        kandidaten = sammlung.search(
            start=jetzt - SUCHFENSTER_ZURUECK, end=jetzt + SUCHFENSTER_VOR, event=True
        )
        return next(
            (
                k
                for k in kandidaten
                if any(
                    passt_zu_id(text_feld(c, "uid"), termin_id) for c in komponenten(k, "VEVENT")
                )
            ),
            None,
        )

    def _termin_finden(self, termin_id: str, kalender: str | None = None) -> tuple[str, Any, Any]:
        """Sucht einen Termin über UID oder kurze ID – erst im genannten bzw. Standard-,
        dann in allen anderen Kalendern. Gibt (Kalendername, Objekt, Hauptkomponente) zurück."""
        termin_id = str(termin_id).strip()
        if not termin_id:
            raise WerkzeugFehler("Bitte eine termin_id angeben.")
        with self.verbindung.fehler_abfangen("Termin suchen"):
            erster = self._name(kalender)
            reihenfolge = [(erster, self._kalender(erster))]
            if not kalender:
                reihenfolge += [
                    (n, k)
                    for n, k in self.verbindung.alle_sammlungen()
                    if n.casefold() != erster.casefold() and _hat_termine(k)
                ]
            for name, sammlung in reihenfolge:
                objekt = self._suchen_in(sammlung, termin_id)
                if objekt is not None:
                    haupt = next(
                        (
                            c
                            for c in komponenten(objekt, "VEVENT")
                            if c.get("recurrence-id") is None
                        ),
                        komponenten(objekt, "VEVENT")[0],
                    )
                    return name, objekt, haupt
        raise WerkzeugFehler(
            f"Keinen Termin mit der ID {termin_id} gefunden. Zeig die Termine mit "
            "termine_anzeigen an und nimm die ID von dort."
        )

    def _vorkommen(self, ical: icalendar.Calendar, uid: str, tag_text: str) -> date | datetime:
        """Ursprünglicher Beginn des Vorkommens einer Serie an einem Tag (RECURRENCE-ID)."""
        tag = zeitpunkt_lesen(tag_text, self.zone, "nur_am")
        tag = tag.date() if isinstance(tag, datetime) else tag
        for vorkommen in recurring_ical_events.of(ical).at(tag):
            if text_feld(vorkommen, "uid") != uid:
                continue
            kennung = vorkommen.get("recurrence-id")
            return kennung.dt if kennung is not None else vorkommen["dtstart"].dt
        raise WerkzeugFehler(f"Die Serie findet am {datum_kurz(tag)} nicht statt.")

    @staticmethod
    def _gleiche_kennung(a: date | datetime, b: date | datetime) -> bool:
        if isinstance(a, datetime) and isinstance(b, datetime):
            return a.astimezone(ZoneInfo("UTC")) == b.astimezone(ZoneInfo("UTC"))
        return a == b

    def _ausnahme(self, ical: icalendar.Calendar, master: Any, kennung: date | datetime) -> Any:
        """Holt oder erzeugt die Ausnahme (Override) einer Serie für ein Vorkommen."""
        for komponente in ical.walk("VEVENT"):
            rid = komponente.get("recurrence-id")
            if rid is not None and self._gleiche_kennung(rid.dt, kennung):
                return komponente
        ausnahme = icalendar.Event.from_ical(master.to_ical())
        dauer = ende_berechnen(master, master["dtstart"].dt) - master["dtstart"].dt
        for name in ("rrule", "rdate", "exdate", "dtstart", "dtend", "duration", "recurrence-id"):
            if name in ausnahme:
                del ausnahme[name]
        ausnahme.add("recurrence-id", kennung)
        ausnahme.add("dtstart", kennung)
        ausnahme.add("dtend", kennung + dauer)
        ical.add_component(ausnahme)
        return ausnahme

    def termin_aendern(
        self,
        termin_id: str,
        titel: str | None = None,
        start: str | None = None,
        ende: str | None = None,
        ort: str | None = None,
        notiz: str | None = None,
        farbe: str | None = None,
        kategorien: Any = None,
        erinnerungen: Any = None,
        wiederholung: str | None = None,
        ganztaegig: bool | None = None,
        nur_am: str | None = None,
        teilnehmer: Any = None,
        kalender: str | None = None,
        bestaetigt: bool = False,
    ) -> str:
        name, objekt, haupt = self._termin_finden(termin_id, kalender)
        uid = text_feld(haupt, "uid")
        vorher = als_ical_text(objekt.icalendar_instance)
        ical = icalendar.Calendar.from_ical(vorher)
        master = next(
            (c for c in ical.walk("VEVENT") if c.get("recurrence-id") is None),
            next(iter(ical.walk("VEVENT"))),
        )
        _, zeile_vorher = self._beschreiben(master)

        ziel = master
        if nur_am:
            if master.get("rrule") is None:
                raise WerkzeugFehler("nur_am geht nur bei wiederkehrenden Terminen.")
            if wiederholung is not None:
                raise WerkzeugFehler("Die Wiederholung kann man nur für die ganze Serie ändern.")
            ziel = self._ausnahme(ical, master, self._vorkommen(ical, uid, nur_am))

        geaendert: set[str] = set()
        alt_start = ziel["dtstart"].dt
        tage = [alt_start]
        if start or ende or ganztaegig is not None:
            dauer = ende_berechnen(ziel, alt_start) - alt_start
            beginn, schluss = self._zeiten(
                start or _iso(in_zone(alt_start, self.zone)), ende, ganztaegig, dauer
            )
            for feld in ("dtstart", "dtend", "duration"):
                if feld in ziel:
                    del ziel[feld]
            ziel.add("dtstart", beginn)
            ziel.add("dtend", schluss)
            geaendert.add("zeit")
            tage.append(beginn)
        for feld, wert, ical_name in (
            ("titel", titel, "summary"),
            ("ort", ort, "location"),
            ("notiz", notiz, "description"),
        ):
            if wert is None:
                continue
            if ical_name in ziel:
                del ziel[ical_name]
            if not _ist_leer(wert) or feld == "titel":
                text = str(wert).strip()
                if feld == "titel" and not text:
                    raise WerkzeugFehler("Der Titel darf nicht leer sein.")
                ziel.add(ical_name, text)
            geaendert.add(feld)
        if kategorien is not None:
            self._kategorien_setzen(ziel, kategorien)
            geaendert.add("kategorien")
            if farbe is None and self.farbsystem is not None and not _ist_leer(kategorien):
                farbe = self.farbsystem.farbe_fuer(_liste(kategorien))
        if farbe is not None:
            self._farbe_setzen(ziel, farbe)
            geaendert.add("farbe")
        if erinnerungen is not None:
            self._alarme_setzen(ziel, erinnerungen)
            geaendert.add("erinnerungen")
        if wiederholung is not None:
            self._wiederholung_setzen(ziel, wiederholung)
            for komponente in list(ical.walk("VEVENT")):
                if komponente.get("recurrence-id") is not None and _ist_leer(wiederholung):
                    ical.subcomponents.remove(komponente)
            geaendert.add("wiederholung")
        alte_teilnehmer = _teilnehmer(master)
        neue_teilnehmer = alte_teilnehmer
        if teilnehmer is not None:
            neue_teilnehmer = (
                [] if _ist_leer(teilnehmer) else [_adresse(t) for t in _liste(teilnehmer)]
            )
            geaendert.add("teilnehmer")
        if not geaendert:
            raise WerkzeugFehler("Es gibt nichts zu ändern – welche Angabe soll anders werden?")

        for feld in ("dtstamp", "last-modified"):
            if feld in ziel:
                del ziel[feld]
        ziel.add("dtstamp", jetzt_utc())
        ziel.add("last-modified", jetzt_utc())
        _, zeile = self._beschreiben(ziel)

        betroffen = sorted(set(alte_teilnehmer) | set(neue_teilnehmer))
        if betroffen and not geaendert <= KOSMETISCH:
            sperre = self._einladung_pruefen(
                "aendern", uid, zeile, betroffen, bool(bestaetigt), "termin_aendern"
            )
            if sperre:
                return sperre
        if teilnehmer is not None:
            self._teilnehmer_setzen(ziel, neue_teilnehmer)
            _, zeile = self._beschreiben(ziel)

        nachher = als_ical_text(ical)
        with self.verbindung.fehler_abfangen("Termin ändern"):
            objekt.data = nachher
            objekt.save(only_this_recurrence=False)
        was = f"am {nur_am} (nur dieses Mal)" if nur_am else ""
        hinweis = self._protokollieren(
            "termin_geaendert",
            name,
            f"{zeile_vorher[2:]} → {zeile[2:]}",
            vorher=vorher,
            nachher=nachher,
            uid=uid,
        )
        return (
            f"Geändert im Kalender „{name}“{(' ' + was) if was else ''}:\n"
            f"vorher: {zeile_vorher[2:]}\njetzt:  {zeile[2:]}{hinweis}"
            + (self._nachpruefen(*tage) if "zeit" in geaendert else "")
        )

    def termin_loeschen(
        self,
        termin_id: str,
        nur_am: str | None = None,
        kalender: str | None = None,
        bestaetigt: bool = False,
    ) -> str:
        name, objekt, haupt = self._termin_finden(termin_id, kalender)
        uid = text_feld(haupt, "uid")
        _, zeile = self._beschreiben(haupt)
        vorher = als_ical_text(objekt.icalendar_instance)
        if teilnehmer := _teilnehmer(haupt):
            sperre = self._einladung_pruefen(
                "loeschen",
                uid,
                zeile + (f" (nur am {nur_am})" if nur_am else ""),
                teilnehmer,
                bool(bestaetigt),
                "termin_loeschen",
            )
            if sperre:
                return sperre

        if nur_am:
            if haupt.get("rrule") is None:
                raise WerkzeugFehler("nur_am geht nur bei wiederkehrenden Terminen.")
            ical = icalendar.Calendar.from_ical(vorher)
            master = next(c for c in ical.walk("VEVENT") if c.get("recurrence-id") is None)
            kennung = self._vorkommen(ical, uid, nur_am)
            for komponente in list(ical.walk("VEVENT")):
                rid = komponente.get("recurrence-id")
                if rid is not None and self._gleiche_kennung(rid.dt, kennung):
                    ical.subcomponents.remove(komponente)
            master.add("exdate", kennung)
            nachher = als_ical_text(ical)
            with self.verbindung.fehler_abfangen("Termin löschen"):
                objekt.data = nachher
                objekt.save(only_this_recurrence=False)
            tag = kennung.date() if isinstance(kennung, datetime) else kennung
            beschreibung = f"{zeile[2:]} – nur am {datum_kurz(tag)} gelöscht"
            hinweis = self._protokollieren(
                "termin_geaendert", name, beschreibung, vorher=vorher, nachher=nachher, uid=uid
            )
            return f"Im Kalender „{name}“ nur am {datum_kurz(tag)} gelöscht:\n{zeile}{hinweis}"

        if haupt.get("rrule") is not None:
            zeile += "\n(wiederkehrender Termin – alle Wiederholungen gelöscht)"
        with self.verbindung.fehler_abfangen("Termin löschen"):
            objekt.delete()
        hinweis = self._protokollieren("termin_geloescht", name, zeile[2:], vorher=vorher, uid=uid)
        return f"Gelöscht aus dem Kalender „{name}“:\n{zeile}{hinweis}"

    # -------------------------------------------------------------- Kalender verwalten

    def kalender_anzeigen(self) -> str:
        zeilen = []
        for name, sammlung in self.verbindung.alle_sammlungen():
            if not _hat_termine(sammlung):
                continue
            farbe = self.verbindung.kalender_farbe(sammlung)
            zeile = f"- {name}"
            if farbe:
                zeile += f" (Farbe: {farbe_name(farbe)})"
            if name.casefold() == self.name.casefold():
                zeile += " [Standard]"
            zeilen.append(zeile)
        text = "Kalender in Nextcloud:\n" + ("\n".join(zeilen) or "(keine)")
        if self.farbsystem is not None:
            text += f"\nDein Farbsystem (Kategorie = Farbe): {self.farbsystem.text()}"
        return text

    def kalender_anlegen(self, name: str, farbe: str | None = None) -> str:
        name = " ".join(str(name).split())
        if not name:
            raise WerkzeugFehler("Der Kalender braucht einen Namen.")
        if any(n.casefold() == name.casefold() for n, _ in self.verbindung.alle_sammlungen()):
            raise WerkzeugFehler(f"Den Kalender „{name}“ gibt es schon.")
        if farbe is None and self.farbsystem is not None:
            farbe = self.farbsystem.farbe_fuer([name])
        farbe_hex = farbe_lesen(farbe)[1] if farbe else None
        self.verbindung.kalender_anlegen(name, farbe_hex)
        hinweis = self._protokollieren(
            "kalender_neu",
            name,
            f"„{name}“" + (f" in {farbe_name(farbe_hex)}" if farbe_hex else ""),
            nachher={"name": name, "farbe": farbe_hex},
        )
        farbtext = f" mit der Farbe {farbe_name(farbe_hex)}" if farbe_hex else ""
        return f"Kalender „{name}“{farbtext} angelegt.{hinweis}"

    def kalender_aendern(
        self, name: str, neuer_name: str | None = None, farbe: str | None = None
    ) -> str:
        alt_name = self._name(name)
        sammlung = self._kalender(alt_name)
        neuer_name = " ".join(str(neuer_name).split()) if neuer_name else None
        if neuer_name and alt_name.casefold() == self.name.casefold():
            raise WerkzeugFehler(
                f"„{alt_name}“ ist mein Standardkalender (NEXTCLOUD_KALENDER). Den benenne ich "
                "nicht um, sonst finde ich ihn nach einem Neustart nicht mehr. Die Farbe kann "
                "ich ändern."
            )
        if neuer_name and any(
            n.casefold() == neuer_name.casefold() for n, _ in self.verbindung.alle_sammlungen()
        ):
            raise WerkzeugFehler(f"Einen Kalender „{neuer_name}“ gibt es schon.")
        farbe_hex = farbe_lesen(farbe)[1] if farbe else None
        if not neuer_name and not farbe_hex:
            raise WerkzeugFehler("Neuer Name oder neue Farbe fehlt.")
        alt_farbe = self.verbindung.kalender_farbe(sammlung)
        self.verbindung.kalender_aendern(sammlung, neuer_name, farbe_hex)
        teile = []
        if neuer_name:
            teile.append(f"umbenannt in „{neuer_name}“")
        if farbe_hex:
            teile.append(f"neue Farbe {farbe_name(farbe_hex)}")
        hinweis = self._protokollieren(
            "kalender_geaendert",
            neuer_name or alt_name,
            f"„{alt_name}“: " + ", ".join(teile),
            vorher={"name": alt_name, "farbe": alt_farbe},
            nachher={"name": neuer_name or alt_name, "farbe": farbe_hex or alt_farbe},
        )
        return f"Kalender „{alt_name}“ {' und '.join(teile)}.{hinweis}"

    def farbsystem_festlegen(self, kategorie: str, farbe: str | None = None) -> str:
        if self.farbsystem is None:
            raise WerkzeugFehler("Das Farbsystem ist nicht verfügbar.")
        self.farbsystem.festlegen(kategorie, None if farbe is None or _ist_leer(farbe) else farbe)
        return f"Farbsystem aktualisiert: {self.farbsystem.text()}"

    # -------------------------------------------------------------- Rückgängig

    def rueckgaengig(self, aenderung_id: int | str | None = None) -> str:
        if self.protokoll is None:
            raise WerkzeugFehler("Ohne Protokoll kann ich nichts rückgängig machen.")
        if aenderung_id in (None, ""):
            aenderung = self.protokoll.letzte_offene()
            if aenderung is None:
                raise WerkzeugFehler("Es gibt keine Kalenderänderung, die ich zurücknehmen könnte.")
        else:
            try:
                nummer = int(str(aenderung_id).strip().lstrip("#"))
            except ValueError as fehler:
                raise WerkzeugFehler(f"„{aenderung_id}“ ist keine Änderungsnummer.") from fehler
            aenderung = self.protokoll.holen(nummer)
            if aenderung is None:
                raise WerkzeugFehler(f"Eine Änderung #{nummer} gibt es nicht.")
        if aenderung.rueckgaengig:
            raise WerkzeugFehler(f"Änderung #{aenderung.id} ist schon rückgängig gemacht.")

        aktion = aenderung.aktion
        with self.verbindung.fehler_abfangen("Rückgängig machen"):
            if aktion in ("termin_neu", "termin_geaendert", "termin_geloescht"):
                sammlung = self._kalender(aenderung.kalender)
                objekt = self._suchen_in(sammlung, aenderung.uid or "")
                if aktion == "termin_neu":
                    if objekt is not None:
                        objekt.delete()
                elif objekt is not None:
                    objekt.data = aenderung.vorher
                    objekt.save(only_this_recurrence=False, increase_seqno=False)
                else:
                    sammlung.save_event(aenderung.vorher)
            elif aktion == "kalender_neu":
                name = aenderung.json_nachher().get("name", aenderung.kalender)
                sammlung = self._kalender(name)
                if sammlung.search(event=True):
                    raise WerkzeugFehler(
                        f"Im Kalender „{name}“ stehen inzwischen Termine – den lösche ich nicht "
                        "einfach. Soll er wirklich weg, mach das bitte in Nextcloud selbst."
                    )
                self.verbindung.kalender_loeschen(sammlung)
            elif aktion == "kalender_geaendert":
                vorher, nachher = aenderung.json_vorher(), aenderung.json_nachher()
                sammlung = self._kalender(nachher.get("name", aenderung.kalender))
                self.verbindung.kalender_aendern(
                    sammlung,
                    vorher.get("name") if vorher.get("name") != nachher.get("name") else None,
                    vorher.get("farbe") if vorher.get("farbe") != nachher.get("farbe") else None,
                )
            else:
                raise WerkzeugFehler(f"Änderungen vom Typ {aktion} kann ich nicht zurücknehmen.")
        self.protokoll.als_rueckgaengig_markieren(aenderung.id)
        return f"Rückgängig gemacht:\n{aenderung.zeile()}"

    def protokoll_anzeigen(self, anzahl: int = 10) -> str:
        if self.protokoll is None:
            return "Es gibt kein Protokoll."
        liste = self.protokoll.liste(max(1, min(int(anzahl), 50)))
        if not liste:
            return "Bisher habe ich nichts am Kalender geändert."
        return "Meine letzten Kalenderänderungen (neueste zuerst):\n" + "\n".join(
            a.zeile() for a in liste
        )


# ---------------------------------------------------------------------- Hilfen


def _iso(wert: date | datetime) -> str:
    return wert.strftime("%Y-%m-%dT%H:%M") if isinstance(wert, datetime) else wert.isoformat()


def _hat_termine(sammlung: Any) -> bool:
    """``True``, wenn die Sammlung Termine (VEVENT) aufnehmen kann."""
    try:
        unterstuetzt = sammlung.get_supported_components()
    except Exception:
        return True
    return not unterstuetzt or "VEVENT" in unterstuetzt


def _kategorien(komponente: Any) -> list[str]:
    roh = komponente.get("categories")
    if roh is None:
        return []
    werte = roh if isinstance(roh, list) else [roh]
    ergebnis: list[str] = []
    for wert in werte:
        if hasattr(wert, "cats"):
            ergebnis.extend(str(c) for c in wert.cats)
        else:
            ergebnis.extend(t.strip() for t in str(wert).split(",") if t.strip())
    return ergebnis


def _alarm_minuten(komponente: Any) -> list[int]:
    minuten = []
    for alarm in getattr(komponente, "subcomponents", []):
        if getattr(alarm, "name", "") != "VALARM" or alarm.get("trigger") is None:
            continue
        wert = alarm["trigger"].dt
        if isinstance(wert, timedelta):
            minuten.append(int(-wert.total_seconds() // 60))
    return sorted(minuten)


def _minuten_text(minuten: int) -> str:
    if minuten == 0:
        return "zu Beginn"
    if minuten % 1440 == 0:
        tage = minuten // 1440
        return f"{tage} Tag{'e' if tage != 1 else ''} vorher"
    if minuten % 60 == 0:
        stunden = minuten // 60
        return f"{stunden} Std. vorher"
    return f"{minuten} Min. vorher"


# ---------------------------------------------------------------------- Werkzeuge


def kalender_werkzeuge(
    verbindung: NextcloudVerbindung,
    name: str,
    zone: ZoneInfo,
    bestaetigungen: Bestaetigungen,
    protokoll: KalenderProtokoll | None = None,
    farbsystem: Farbsystem | None = None,
    kalender: Kalender | None = None,
) -> list[Werkzeug]:
    k = kalender or Kalender(verbindung, name, zone, bestaetigungen, protokoll, farbsystem)
    zeitangabe = "ISO-Format, Zeitzone Europe/Zurich"
    selbststaendig = (
        " Führe Louis' Kalenderwünsche direkt aus, ohne vorher zu fragen, und sag ihm danach "
        "kurz, was du gemacht hast."
    )
    kalender_param = {
        "type": "string",
        "description": f"Name des Kalenders, optional (Standard: {name})",
    }
    eigenschaften = {
        "ort": {"type": "string", "description": "Ort; leer = entfernen"},
        "notiz": {"type": "string", "description": "Beschreibung; leer = entfernen"},
        "farbe": {
            "type": "string",
            "description": "Farbe, z. B. rot, blau, grün, türkis oder #E53935; 'keine' = entfernen",
        },
        "kategorien": {
            "type": "array",
            "items": {"type": "string"},
            "description": 'Kategorien, z. B. ["Arbeit"]; ohne Farbe gilt dein Farbsystem',
        },
        "erinnerungen": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Erinnerungen in Minuten vor Beginn, z. B. [15, 1440]; [] = keine",
        },
        "wiederholung": {
            "type": "string",
            "description": "z. B. täglich, werktags, wöchentlich, alle 2 Wochen, jeden Montag, "
            "monatlich, jährlich, optional „bis 31.12.2026“ oder „10 mal“, oder eine RRULE; "
            "'keine' = Serie beenden",
        },
        "ganztaegig": {"type": "boolean", "description": "true = ganztägiger Termin"},
        "teilnehmer": {
            "type": "array",
            "items": {"type": "string"},
            "description": "E-Mail-Adressen anderer Personen (bekommen eine Einladung – nur "
            "nach Louis' ausdrücklichem Ja)",
        },
        "bestaetigt": {
            "type": "boolean",
            "description": "Nur true, wenn Louis E-Mails an Teilnehmer ausdrücklich zugestimmt hat",
        },
    }
    termin_id = {"type": "string", "description": "ID des Termins aus termine_anzeigen"}
    nur_am = {
        "type": "string",
        "description": "Nur dieses eine Vorkommen einer Serie (Datum, z. B. 2026-11-16)",
    }
    return [
        Werkzeug(
            "termine_anzeigen",
            "Zeigt Louis' Termine aus dem Nextcloud-Kalender in einem Zeitraum "
            "(wiederkehrende Termine einzeln, mit Farbe, Kategorien, Erinnerungen und ID). "
            "Für einen einzelnen Tag nur 'von' angeben. kalender='alle' zeigt alle Kalender.",
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
                    "kalender": {
                        "type": "string",
                        "description": f"Kalendername oder 'alle' (Standard: {name})",
                    },
                },
                "required": ["von"],
            },
            k.termine_anzeigen,
        ),
        Werkzeug(
            "termin_eintragen",
            "Trägt einen Termin ein. Ohne Ende dauert er eine Stunde; nur ein Datum ohne "
            "Uhrzeit ergibt einen ganztägigen Termin. Farbe, Kategorien, Erinnerungen und "
            "Wiederholung gehen gleich mit." + selbststaendig,
            {
                "type": "object",
                "properties": {
                    "titel": {"type": "string", "description": "Titel, z. B. Zahnarzt"},
                    "start": {
                        "type": "string",
                        "description": f"Beginn ({zeitangabe}), z. B. 2026-11-13T14:00",
                    },
                    "ende": {"type": "string", "description": f"Ende ({zeitangabe}), optional"},
                    "kalender": kalender_param,
                    **eigenschaften,
                },
                "required": ["titel", "start"],
            },
            k.termin_eintragen,
        ),
        Werkzeug(
            "termin_aendern",
            "Ändert oder verschiebt einen Termin: nur die Angaben mitschicken, die sich ändern. "
            "Neuer start ohne ende verschiebt den Termin und behält die Dauer. Mit nur_am wird "
            "bei einer Serie nur dieses eine Vorkommen geändert." + selbststaendig,
            {
                "type": "object",
                "properties": {
                    "termin_id": termin_id,
                    "titel": {"type": "string", "description": "Neuer Titel"},
                    "start": {"type": "string", "description": f"Neuer Beginn ({zeitangabe})"},
                    "ende": {"type": "string", "description": f"Neues Ende ({zeitangabe})"},
                    "nur_am": nur_am,
                    "kalender": kalender_param,
                    **eigenschaften,
                },
                "required": ["termin_id"],
            },
            k.termin_aendern,
        ),
        Werkzeug(
            "termin_loeschen",
            "Löscht einen Termin (oder mit nur_am nur ein Vorkommen einer Serie). Wird "
            "protokolliert und lässt sich mit kalender_rueckgaengig zurückholen. Nur bei "
            "Terminen mit Teilnehmern (die eine Absage-Mail bekämen) erst Louis fragen."
            + selbststaendig,
            {
                "type": "object",
                "properties": {
                    "termin_id": termin_id,
                    "nur_am": nur_am,
                    "kalender": kalender_param,
                    "bestaetigt": eigenschaften["bestaetigt"],
                },
                "required": ["termin_id"],
            },
            k.termin_loeschen,
        ),
        Werkzeug(
            "kalender_anzeigen",
            "Listet alle Kalender mit Farbe und dein Farbsystem (Kategorie = Farbe).",
            {"type": "object", "properties": {}},
            k.kalender_anzeigen,
        ),
        Werkzeug(
            "kalender_anlegen",
            "Legt einen neuen Kalender an, optional mit Farbe (z. B. für Arbeit, Lehre, "
            "Pausen)." + selbststaendig,
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name des Kalenders"},
                    "farbe": {"type": "string", "description": "Farbe, optional"},
                },
                "required": ["name"],
            },
            k.kalender_anlegen,
        ),
        Werkzeug(
            "kalender_aendern",
            "Benennt einen Kalender um oder ändert seine Farbe." + selbststaendig,
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Heutiger Name"},
                    "neuer_name": {"type": "string", "description": "Neuer Name, optional"},
                    "farbe": {"type": "string", "description": "Neue Farbe, optional"},
                },
                "required": ["name"],
            },
            k.kalender_aendern,
        ),
        Werkzeug(
            "farbsystem_festlegen",
            "Legt fest, welche Farbe Termine einer Kategorie bekommen (z. B. Lehre = grün); "
            "ohne farbe wird die Kategorie entfernt.",
            {
                "type": "object",
                "properties": {
                    "kategorie": {"type": "string", "description": "z. B. Arbeit"},
                    "farbe": {"type": "string", "description": "z. B. blau"},
                },
                "required": ["kategorie"],
            },
            k.farbsystem_festlegen,
        ),
        Werkzeug(
            "kalender_rueckgaengig",
            "Macht eine Kalenderänderung rückgängig („mach das rückgängig“). Ohne ID die "
            "letzte; mit kalender_protokoll siehst du die Liste.",
            {
                "type": "object",
                "properties": {
                    "aenderung_id": {"type": "integer", "description": "Optional: Nummer"},
                },
            },
            k.rueckgaengig,
        ),
        Werkzeug(
            "kalender_protokoll",
            "Zeigt deine letzten Kalenderänderungen mit Nummer (für Rückgängig).",
            {
                "type": "object",
                "properties": {"anzahl": {"type": "integer", "description": "Standard 10"}},
            },
            k.protokoll_anzeigen,
        ),
    ]
