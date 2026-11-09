"""Aufbau des Systemprompts: Charakter, aktuelle Zeit, Werkzeuge."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]

#: Wird benutzt, wenn die Charakterdatei fehlt oder leer ist.
ERSATZ_CHARAKTER = (
    "Du bist Missa, die persönliche Assistentin von Louis. "
    "Du antwortest auf Deutsch, freundlich, ehrlich und kurz."
)


def charakter_lesen(pfad: Path) -> str:
    """Liest die Charakterdatei – bei jeder Anfrage neu, damit Änderungen sofort wirken."""
    try:
        text = pfad.read_text(encoding="utf-8").strip()
    except OSError as fehler:
        log.warning("Charakterdatei %s nicht lesbar (%s), nutze Ersatztext", pfad, fehler)
        return ERSATZ_CHARAKTER
    if not text:
        log.warning("Charakterdatei %s ist leer, nutze Ersatztext", pfad)
        return ERSATZ_CHARAKTER
    return text


def datum_lang(tag: datetime) -> str:
    """z. B. ``Montag, 9. November 2026``."""
    return f"{WOCHENTAGE[tag.weekday()]}, {tag.day}. {MONATE[tag.month - 1]} {tag.year}"


def zeit_text(jetzt: datetime) -> str:
    """Datum, Wochentag, Uhrzeit und die nächsten Tage als Hilfe für relative Angaben."""
    zone = getattr(jetzt.tzinfo, "key", None) or jetzt.tzname() or ""
    zeilen = [
        f"Jetzt ist {datum_lang(jetzt)}, {jetzt:%H:%M} Uhr (Zeitzone {zone}).",
        f"ISO-Format: {jetzt:%Y-%m-%dT%H:%M}",
        "Die nächsten Tage:",
    ]
    for abstand in range(1, 8):
        tag = jetzt + timedelta(days=abstand)
        name = "morgen" if abstand == 1 else "übermorgen" if abstand == 2 else ""
        vorsatz = f"{name}: " if name else ""
        zeilen.append(f"- {vorsatz}{datum_lang(tag)} = {tag:%Y-%m-%d}")
    return "\n".join(zeilen)


def werkzeug_text(beschreibung: str) -> str:
    if not beschreibung:
        return "Dir stehen gerade keine Werkzeuge zur Verfügung."
    return (
        "Du kannst diese Werkzeuge aufrufen. Nutze sie, wenn sie helfen, und erfinde "
        "nie Ergebnisse. Meldet ein Werkzeug einen Fehler, sag das ehrlich.\n" + beschreibung
    )


def systemprompt_bauen(
    charakter: str,
    jetzt: datetime,
    werkzeuge: str,
    zusatz: Iterable[str] = (),
    client_system: str | None = None,
) -> str:
    """Setzt den Systemprompt zusammen.

    Reihenfolge: Charakter, Zeit, Werkzeuge, weitere Blöcke (z. B. Gedächtnis),
    zuletzt Hinweise der App. Die App kann den Charakter damit nie ersetzen.
    """
    teile = [
        charakter.strip(),
        "## Aktuelle Zeit\n" + zeit_text(jetzt),
        "## Werkzeuge\n" + werkzeug_text(werkzeuge),
    ]
    teile.extend(block.strip() for block in zusatz if block and block.strip())
    if client_system and client_system.strip():
        teile.append("## Zusätzliche Hinweise der App\n" + client_system.strip())
    return "\n\n".join(teile)
