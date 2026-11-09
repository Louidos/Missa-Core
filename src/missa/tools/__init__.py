"""Werkzeuge, die Missa benutzen kann (Kalender, Aufgaben, Gedächtnis …)."""

from __future__ import annotations

from missa.core.config import Einstellungen
from missa.core.werkzeuge import Werkzeugkasten


def werkzeuge_laden(einstellungen: Einstellungen) -> Werkzeugkasten:
    """Stellt alle Werkzeuge zusammen, die mit dieser Konfiguration nutzbar sind."""
    return Werkzeugkasten()
