"""Gefälschter CalDAV-Client für Tests (ersetzt ``caldav.DAVClient``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import icalendar
import recurring_ical_events
from caldav.lib import error as caldav_fehler


class FakeObjekt:
    """Ein Kalenderobjekt (Termin oder Aufgabe)."""

    def __init__(self, ical: str | icalendar.Calendar, sammlung: FakeSammlung) -> None:
        self.icalendar_instance = (
            ical if isinstance(ical, icalendar.Calendar) else icalendar.Calendar.from_ical(ical)
        )
        self.sammlung = sammlung
        self.erledigt_aufgerufen = False

    @property
    def icalendar_component(self) -> Any:
        return next(
            c for c in self.icalendar_instance.subcomponents if c.name in ("VEVENT", "VTODO")
        )

    @property
    def uid(self) -> str:
        return str(self.icalendar_component["uid"])

    @property
    def data(self) -> str:
        return self.icalendar_instance.to_ical().decode("utf-8")

    @data.setter
    def data(self, text: str) -> None:
        self.icalendar_instance = icalendar.Calendar.from_ical(text)

    def delete(self) -> None:
        self.sammlung._pruefen()
        self.sammlung.objekte.remove(self)

    def complete(self, **_: Any) -> None:
        self.erledigt_aufgerufen = True
        komponente = self.icalendar_component
        komponente["status"] = "COMPLETED"
        komponente.add("completed", datetime(2026, 11, 9, 12, 0))

    def save(self, **argumente: Any) -> None:
        self.sammlung._pruefen()
        self.gespeichert = getattr(self, "gespeichert", 0) + 1
        self.save_argumente = argumente


class FakeSammlung:
    """Ein Kalender oder eine Aufgabenliste."""

    def __init__(self, name: str, farbe: str | None = None, art: str = "VEVENT") -> None:
        self.name = name
        self.farbe = farbe
        self.art = art
        self.geloescht = False
        self.principal: FakePrincipal | None = None
        self.objekte: list[FakeObjekt] = []
        self.suchen: list[dict[str, Any]] = []
        #: Wenn gesetzt, liefert search() genau diese Objekte (z. B. aufgelöste Serien).
        self.suchergebnis: list[FakeObjekt] | None = None
        self.fehler: Exception | None = None

    def get_display_name(self) -> str:
        return self.name

    def get_supported_components(self) -> list[str]:
        return [self.art]

    def get_property(self, eigenschaft: Any) -> str | None:
        return self.farbe

    def set_properties(self, eigenschaften: list[Any]) -> None:
        self._pruefen()
        for eigenschaft in eigenschaften:
            if eigenschaft.tag.endswith("calendar-color"):
                self.farbe = eigenschaft.value
            elif eigenschaft.tag.endswith("displayname"):
                self.name = eigenschaft.value

    def delete(self) -> None:
        self._pruefen()
        self.geloescht = True
        if self.principal is not None:
            self.principal.sammlungen.remove(self)

    def _pruefen(self) -> None:
        if self.fehler:
            raise self.fehler

    def hinzufuegen(self, ical: str | icalendar.Calendar) -> FakeObjekt:
        objekt = FakeObjekt(ical, self)
        self.objekte.append(objekt)
        return objekt

    def save_event(self, ical: str) -> FakeObjekt:
        self._pruefen()
        return self.hinzufuegen(ical)

    def save_todo(self, ical: str) -> FakeObjekt:
        self._pruefen()
        return self.hinzufuegen(ical)

    def search(self, **argumente: Any) -> list[FakeObjekt]:
        self._pruefen()
        self.suchen.append(argumente)
        if self.suchergebnis is not None:
            return list(self.suchergebnis)
        if argumente.get("todo"):
            return self.todos(include_completed=argumente.get("include_completed", False))
        start, ende = argumente.get("start"), argumente.get("end")
        treffer = []
        for objekt in self.objekte:
            komponente = objekt.icalendar_component
            if komponente.name != "VEVENT":
                continue
            if start is None or ende is None:
                treffer.append(objekt)
                continue
            if komponente.get("rrule") is not None:
                vorkommen = recurring_ical_events.of(objekt.icalendar_instance).between(start, ende)
                if argumente.get("expand"):
                    for einzeln in vorkommen:
                        kopie = icalendar.Calendar()
                        kopie.add("prodid", "-//Test//DE")
                        kopie.add("version", "2.0")
                        kopie.add_component(einzeln)
                        treffer.append(FakeObjekt(kopie, self))
                elif vorkommen:
                    treffer.append(objekt)
                continue
            beginn = komponente["dtstart"].dt
            if not isinstance(beginn, datetime):
                beginn = datetime(beginn.year, beginn.month, beginn.day, tzinfo=start.tzinfo)
            if start <= beginn < ende:
                treffer.append(objekt)
        return treffer

    def event_by_uid(self, uid: str) -> FakeObjekt:
        self._pruefen()
        for objekt in self.objekte:
            if objekt.uid == uid and objekt.icalendar_component.name == "VEVENT":
                return objekt
        raise caldav_fehler.NotFoundError(f"{uid} nicht gefunden")

    def todo_by_uid(self, uid: str) -> FakeObjekt:
        self._pruefen()
        for objekt in self.objekte:
            if objekt.uid == uid and objekt.icalendar_component.name == "VTODO":
                return objekt
        raise caldav_fehler.NotFoundError(f"{uid} nicht gefunden")

    def todos(self, include_completed: bool = False, **_: Any) -> list[FakeObjekt]:
        self._pruefen()
        return [
            o
            for o in self.objekte
            if o.icalendar_component.name == "VTODO"
            and (include_completed or str(o.icalendar_component.get("status", "")) != "COMPLETED")
        ]


class FakePrincipal:
    def __init__(self, sammlungen: list[FakeSammlung]) -> None:
        self.sammlungen = sammlungen
        for sammlung in sammlungen:
            sammlung.principal = self

    def calendars(self) -> list[FakeSammlung]:
        return list(self.sammlungen)

    def make_calendar(self, name: str, **_: Any) -> FakeSammlung:
        neu = FakeSammlung(name)
        neu.principal = self
        self.sammlungen.append(neu)
        return neu

    def calendar_user_address_set(self) -> list[str]:
        return ["mailto:louis@example.ch", "/remote.php/dav/principals/users/louis/"]


class FakeDAV:
    """Fabrik für ``NextcloudVerbindung(client_fabrik=...)``; merkt sich die Aufrufe."""

    def __init__(self, *sammlungen: FakeSammlung) -> None:
        self.sammlungen = list(sammlungen)
        self.aufrufe: list[dict[str, Any]] = []
        self.anmeldung_fehler: Exception | None = None

    def __call__(self, **argumente: Any) -> FakeDAV:
        self.aufrufe.append(argumente)
        return self

    def principal(self) -> FakePrincipal:
        if self.anmeldung_fehler:
            raise self.anmeldung_fehler
        return FakePrincipal(self.sammlungen)


def ereignis(
    uid: str,
    titel: str,
    start: date | datetime,
    ende: date | datetime | None = None,
    **weitere: Any,
) -> icalendar.Calendar:
    """Baut einen VCALENDAR mit einem VEVENT."""
    kalender = icalendar.Calendar()
    kalender.add("prodid", "-//Test//DE")
    kalender.add("version", "2.0")
    komponente = icalendar.Event()
    komponente.add("uid", uid)
    komponente.add("summary", titel)
    komponente.add("dtstart", start)
    if ende is not None:
        komponente.add("dtend", ende)
    for name, wert in weitere.items():
        komponente.add(name.replace("_", "-"), wert)
    kalender.add_component(komponente)
    return kalender


def aufgabe(uid: str, titel: str, **weitere: Any) -> icalendar.Calendar:
    """Baut einen VCALENDAR mit einem VTODO."""
    kalender = icalendar.Calendar()
    kalender.add("prodid", "-//Test//DE")
    kalender.add("version", "2.0")
    komponente = icalendar.Todo()
    komponente.add("uid", uid)
    komponente.add("summary", titel)
    for name, wert in weitere.items():
        komponente.add(name.replace("_", "-"), wert)
    kalender.add_component(komponente)
    return kalender
