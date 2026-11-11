"""Anstehende Updates eines Debian/Ubuntu-Hosts aus ``apt list --upgradable`` lesen.

Eine Zeile sieht so aus::

    openssl/stable-security 3.0.15-1~deb12u1 amd64 [upgradable from: 3.0.14-1~deb12u2]

Sicherheitsupdates erkennt Missa an der Quelle (``…-security``), so wie apt selbst.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ZEILE = re.compile(
    r"^(?P<name>[A-Za-z0-9.+_-]+)/(?P<quellen>\S+)\s+(?P<neu>\S+)\s+(?P<arch>\S+)"
    r"(?:\s+\[(?:upgradable from|aktualisierbar von):\s*(?P<alt>[^\]]+)\])?"
)


@dataclass(frozen=True)
class Paket:
    name: str
    neu: str
    alt: str
    quellen: str

    @property
    def sicherheit(self) -> bool:
        return any(q.endswith("-security") or q == "security" for q in self.quellen.split(","))

    def text(self) -> str:
        von = f"{self.alt} → " if self.alt else ""
        return f"{self.name} ({von}{self.neu})"

    def als_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "neu": self.neu,
            "alt": self.alt,
            "quellen": self.quellen,
            "sicherheit": self.sicherheit,
        }


def apt_liste_lesen(text: str) -> list[Paket]:
    """Pakete aus ``apt list --upgradable`` (Kopfzeilen und Warnungen werden übersprungen)."""
    pakete = []
    for zeile in text.splitlines():
        treffer = ZEILE.match(zeile.strip())
        if treffer is None:
            continue
        pakete.append(
            Paket(
                treffer["name"],
                treffer["neu"],
                (treffer["alt"] or "").strip(),
                treffer["quellen"],
            )
        )
    return sorted(pakete, key=lambda p: (not p.sicherheit, p.name))


def neustart_lesen(text: str) -> tuple[bool, list[str]]:
    """Antwort von ``neustart-noetig``: (nötig?, Pakete, die ihn verlangen)."""
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    if not zeilen or zeilen[0] != "ja":
        return False, []
    return True, sorted(set(zeilen[1:]))


def upgrade_zusammenfassen(text: str) -> str:
    """Kernaussage von ``apt-get -y upgrade``, z. B. „12 aktualisiert, 0 neu installiert …“."""
    for zeile in reversed(text.splitlines()):
        if re.search(r"\d+ (upgraded|aktualisiert)", zeile):
            return zeile.strip()
    return ""
