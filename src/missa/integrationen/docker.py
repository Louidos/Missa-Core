"""Docker-Container lesen: Ausgabe von ``docker ps`` / ``docker stats`` als JSON auswerten.

Gelesen wird über SSH (``missa-befehl docker-ps`` und ``docker-stats``). Hier steckt nur
das Auswerten: Status, Gesundheit, Laufzeit und ob ein Container hängt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

#: So lange darf ein Container in „health: starting“ hängen, bevor er als hängend gilt.
START_GRENZE = timedelta(minutes=10)

EINHEITEN = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def dauer_lesen(text: str) -> timedelta | None:
    """``Up 12 minutes`` / ``About an hour`` / ``Less than a second`` → Dauer."""
    roh = text.casefold()
    if "less than a second" in roh:
        return timedelta(0)
    treffer = re.search(r"(about an?|an?|\d+)\s+(second|minute|hour|day|week|month|year)s?", roh)
    if not treffer:
        return None
    anzahl = 1 if not treffer.group(1)[0].isdigit() else int(treffer.group(1))
    return EINHEITEN[treffer.group(2)] * anzahl


@dataclass(frozen=True)
class Container:
    """Ein Container, wie ``docker ps --all`` ihn zeigt (plus Werte aus ``docker stats``)."""

    host: str
    name: str
    image: str
    zustand: str  # running, exited, restarting, paused, created, dead
    status: str  # z. B. „Up 3 hours (healthy)“
    ports: str = ""
    cpu: str = ""
    speicher: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def gesundheit(self) -> str:
        """``healthy``, ``unhealthy``, ``starting`` oder leer (kein Healthcheck)."""
        treffer = re.search(r"\((?:health: )?(healthy|unhealthy|starting)\)", self.status)
        return treffer.group(1) if treffer else ""

    @property
    def exit_code(self) -> int | None:
        treffer = re.search(r"Exited \((-?\d+)\)", self.status)
        return int(treffer.group(1)) if treffer else None

    @property
    def laeuft_seit(self) -> timedelta | None:
        if not self.status.startswith("Up"):
            return None
        return dauer_lesen(self.status.split("(")[0])

    def problem(self) -> str:
        """Was mit dem Container nicht stimmt – leer, wenn er in Ordnung ist."""
        if self.zustand == "restarting":
            return "startet ständig neu (Neustart-Schleife)"
        if self.zustand == "dead":
            return "ist tot (dead) und lässt sich so nicht mehr starten"
        if self.gesundheit == "unhealthy":
            return "ist ungesund (der Healthcheck schlägt fehl)"
        if self.gesundheit == "starting" and (self.laeuft_seit or timedelta(0)) >= START_GRENZE:
            return "hängt beim Start (Healthcheck seit über 10 Minuten in „starting“)"
        if self.zustand == "exited" and self.exit_code not in (None, 0):
            return f"ist abgestürzt (Exit-Code {self.exit_code})"
        return ""

    def zeile(self) -> str:
        teile = f"- {self.name}: {self.status}"
        werte = ", ".join(w for w in (self.cpu and f"CPU {self.cpu}", self.speicher) if w)
        if werte:
            teile += f" | {werte}"
        teile += f" | {self.image}"
        if problem := self.problem():
            teile += f"  ⚠️ {problem}"
        return teile

    def als_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "name": self.name,
            "image": self.image,
            "zustand": self.zustand,
            "status": self.status,
            "gesundheit": self.gesundheit,
            "ports": self.ports,
            "cpu": self.cpu,
            "speicher": self.speicher,
            "problem": self.problem(),
        }


def _json_zeilen(text: str) -> list[dict[str, Any]]:
    eintraege = []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile.startswith("{"):
            continue
        try:
            eintrag = json.loads(zeile)
        except ValueError:
            continue
        if isinstance(eintrag, dict):
            eintraege.append(eintrag)
    return eintraege


def ps_lesen(host: str, ps_text: str, stats_text: str = "") -> list[Container]:
    """Container aus ``docker ps --format '{{json .}}'`` (optional mit Werten aus stats)."""
    werte = {}
    for eintrag in _json_zeilen(stats_text):
        name = str(eintrag.get("Name", "")).lstrip("/")
        mem = str(eintrag.get("MemUsage", "")).split("/")[0].strip()
        prozent = str(eintrag.get("MemPerc", "")).strip()
        speicher = f"RAM {mem}" + (f" ({prozent})" if prozent else "") if mem else ""
        werte[name] = (str(eintrag.get("CPUPerc", "")).strip(), speicher)
    container = []
    for eintrag in _json_zeilen(ps_text):
        name = str(eintrag.get("Names", "")).split(",")[0].lstrip("/")
        if not name:
            continue
        cpu, speicher = werte.get(name, ("", ""))
        container.append(
            Container(
                host=host,
                name=name,
                image=str(eintrag.get("Image", "")),
                zustand=str(eintrag.get("State", "")).casefold(),
                status=str(eintrag.get("Status", "")),
                ports=str(eintrag.get("Ports", "")),
                cpu=cpu,
                speicher=speicher,
                extra=eintrag,
            )
        )
    return sorted(container, key=lambda c: (c.zustand != "running", c.name))
