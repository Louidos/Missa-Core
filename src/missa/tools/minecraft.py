"""Minecraft-Server per RCON verwalten: Spieler, Whitelist und Neustart.

Ohne ``MINECRAFT_RCON_HOST`` ist das aus. „Wer ist online?“ (lesen), Whitelist anzeigen
(lesen), Spieler hinzufügen und entfernen (schreiben), Neustart (zerstoererisch, warnt in
der Rückfrage, wenn gerade Spieler online sind). Der Neustart läuft über RCON ``stop``:
Der systemd-Dienst aus ``playbooks/minecraft.yml`` startet den Server danach von selbst.
"""

from __future__ import annotations

import re

from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.rcon import RconClient, RconFehler

SPIELER = re.compile(r"^[A-Za-z0-9_]{3,16}$")
LISTE = re.compile(r"There are (\d+) of a max(?: of)? (\d+) players online:?\s*(.*)", re.IGNORECASE)
WHITELIST = re.compile(
    r"There (?:are|is) (\d+) whitelisted players?(?:\(s\))?:?\s*(.*)", re.IGNORECASE
)


def spieler_pruefen(name: object) -> str:
    text = str(name or "").strip()
    if not SPIELER.match(text):
        raise WerkzeugFehler(
            f"„{name}“ ist kein gültiger Minecraft-Name (3–16 Zeichen, A–Z, 0–9, _)."
        )
    return text


def namen(text: str) -> list[str]:
    return [n.strip() for n in text.split(",") if n.strip()]


class MinecraftVerwaltung:
    def __init__(self, rcon: RconClient) -> None:
        self.rcon = rcon

    async def online(self) -> tuple[list[str], int | None]:
        antwort = await self.rcon.befehl("list")
        treffer = LISTE.search(antwort)
        if treffer is None:
            return [], None
        return namen(treffer.group(3)), int(treffer.group(2))

    async def spieler(self) -> str:
        liste, maximal = await self.online()
        if not liste:
            return "Gerade ist niemand online." + (
                f" (Platz für {maximal} Spieler.)" if maximal else ""
            )
        return (
            f"Online ({len(liste)}"
            + (f" von {maximal}" if maximal else "")
            + "): "
            + ", ".join(liste)
        )

    async def whitelist(self) -> str:
        antwort = await self.rcon.befehl("whitelist list")
        treffer = WHITELIST.search(antwort)
        if treffer is None:
            return f"Whitelist: {antwort.strip() or 'leer'}"
        liste = namen(treffer.group(2))
        return f"Auf der Whitelist ({len(liste)}): " + (", ".join(liste) or "niemand")

    async def hinzufuegen(self, spieler: str) -> str:
        name = spieler_pruefen(spieler)
        antwort = await self.rcon.befehl(f"whitelist add {name}")
        return f"Whitelist: {antwort.strip() or f'{name} hinzugefügt'}"

    async def entfernen(self, spieler: str) -> str:
        name = spieler_pruefen(spieler)
        antwort = await self.rcon.befehl(f"whitelist remove {name}")
        return f"Whitelist: {antwort.strip() or f'{name} entfernt'}"

    async def neustart_vorschau(self) -> str:
        try:
            liste, _ = await self.online()
        except RconFehler as fehler:
            raise WerkzeugFehler(str(fehler)) from fehler
        text = (
            "Der Minecraft-Server wird neu gestartet (speichern, stoppen, startet von selbst "
            "wieder; etwa 1 Minute)."
        )
        if liste:
            text += (
                f" ⚠️ ACHTUNG: Gerade sind {len(liste)} Spieler online ({', '.join(liste)}) – sie "
                "fliegen dabei raus. Sag das Louis deutlich."
            )
        else:
            text += " Gerade ist niemand online."
        return text

    async def neustarten(self) -> str:
        liste, _ = await self.online()
        if liste:
            await self.rcon.befehl("say Der Server startet gleich neu – bis gleich!")
        await self.rcon.befehl("save-all")
        await self.rcon.befehl("stop")
        return "Der Minecraft-Server startet neu (in etwa einer Minute ist er wieder da)."


def minecraft_werkzeuge(verwaltung: MinecraftVerwaltung) -> list[Werkzeug]:
    spieler = {
        "type": "object",
        "properties": {"spieler": {"type": "string", "description": "Minecraft-Name"}},
        "required": ["spieler"],
    }
    return [
        Werkzeug(
            "minecraft_spieler",
            "Zeigt, wer gerade auf dem Minecraft-Server online ist („Wer ist online?“).",
            {"type": "object", "properties": {}},
            verwaltung.spieler,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "minecraft_whitelist",
            "Zeigt die Whitelist des Minecraft-Servers.",
            {"type": "object", "properties": {}},
            verwaltung.whitelist,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "minecraft_whitelist_hinzufuegen",
            "Setzt einen Spieler auf die Whitelist des Minecraft-Servers.",
            spieler,
            verwaltung.hinzufuegen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "minecraft_whitelist_entfernen",
            "Nimmt einen Spieler von der Whitelist des Minecraft-Servers.",
            spieler,
            verwaltung.entfernen,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "minecraft_neustarten",
            "Startet den Minecraft-Server neu – nur nach Louis' Ja; warnt, wenn Spieler "
            "online sind.",
            {"type": "object", "properties": {}},
            verwaltung.neustarten,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.neustart_vorschau,
        ),
    ]
