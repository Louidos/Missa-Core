"""Minecraft-RCON: Befehle an den Server schicken (TCP, Source-RCON-Protokoll).

Ein Paket besteht aus Länge (int32, little endian), Anfrage-ID, Typ und Text mit zwei
Null-Bytes am Ende. Typ 3 = Anmelden, 2 = Befehl, 0 = Antwort. Bei falschem Passwort
antwortet der Server mit der ID -1.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

from missa.core.werkzeuge import WerkzeugFehler

ANMELDEN = 3
BEFEHL = 2
MAX_PAKET = 4096 + 14


class RconFehler(WerkzeugFehler):
    """Der Minecraft-Server ist nicht erreichbar oder lehnt RCON ab."""


def paket(anfrage_id: int, typ: int, text: str) -> bytes:
    nutzlast = struct.pack("<ii", anfrage_id, typ) + text.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(nutzlast)) + nutzlast


async def paket_lesen(leser: asyncio.StreamReader) -> tuple[int, int, str]:
    laenge = struct.unpack("<i", await leser.readexactly(4))[0]
    if not 10 <= laenge <= MAX_PAKET:
        raise RconFehler(f"Ungültiges RCON-Paket (Länge {laenge}).")
    daten = await leser.readexactly(laenge)
    anfrage_id, typ = struct.unpack("<ii", daten[:8])
    return anfrage_id, typ, daten[8:-2].decode("utf-8", "replace")


class RconClient:
    """Eine RCON-Verbindung pro Befehl – einfach und robust bei Neustarts des Servers."""

    def __init__(self, host: str, port: int, passwort: str, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.passwort = passwort
        self.timeout = timeout

    async def befehl(self, text: str) -> str:
        try:
            leser, schreiber = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (OSError, TimeoutError) as fehler:
            raise RconFehler(
                f"Der Minecraft-Server ({self.host}:{self.port}) ist per RCON nicht erreichbar: "
                f"{fehler or type(fehler).__name__}"
            ) from fehler
        try:
            schreiber.write(paket(1, ANMELDEN, self.passwort))
            await schreiber.drain()
            anfrage_id, _typ, _ = await asyncio.wait_for(paket_lesen(leser), self.timeout)
            if anfrage_id == -1:
                raise RconFehler("Der Minecraft-Server lehnt das RCON-Passwort ab.")
            schreiber.write(paket(2, BEFEHL, text))
            await schreiber.drain()
            _id, _typ, antwort = await asyncio.wait_for(paket_lesen(leser), self.timeout)
            return antwort
        except (OSError, TimeoutError, asyncio.IncompleteReadError, struct.error) as fehler:
            raise RconFehler(
                f"RCON-Verbindung abgebrochen: {fehler or type(fehler).__name__}"
            ) from fehler
        finally:
            schreiber.close()
            with contextlib.suppress(OSError):
                await schreiber.wait_closed()
