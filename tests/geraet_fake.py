"""Nachbau eines angeschlossenen Geräts (z. B. Laptop) nach dem Vertrag „Geräte-Anschluss v1“.

Das Fake-Gerät spricht echtes HTTP mit einem Missa-Core, der für den Test auf 127.0.0.1
läuft (:func:`laufender_server`): ``POST /api/geraete/anmelden``, den Stream
``GET /api/geraete/auftraege`` (in einem eigenen Thread) und ``POST /api/geraete/ergebnis``.
Es spricht nie einen echten Rechner an.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import uvicorn

#: Antwort für ein Werkzeug: Text (Erfolg), fertiges Ergebnis (dict), Funktion oder SCHWEIGEN.
SCHWEIGEN = object()


def werkzeug(
    name: str, stufe: str = "lesen", beschreibung: str = "", **eigenschaften: Any
) -> dict[str, Any]:
    """Ein Werkzeug so, wie das Gerät es anmeldet."""
    return {
        "name": name,
        "beschreibung": beschreibung or f"Gerätewerkzeug {name}.",
        "parameter": {"type": "object", "properties": eigenschaften},
        "stufe": stufe,
    }


@contextlib.contextmanager
def laufender_server(app: Any) -> Iterator[str]:
    """Startet ``app`` mit uvicorn auf einem freien Port und gibt die Adresse zurück."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    faden = threading.Thread(target=server.run, daemon=True)
    faden.start()
    grenze = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > grenze or not faden.is_alive():
            raise RuntimeError("Test-Server ist nicht gestartet")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        faden.join(10)


def warten(bedingung: Callable[[], Any], sekunden: float = 5.0) -> Any:
    """Wartet, bis ``bedingung()`` etwas Wahres liefert; sonst AssertionError."""
    grenze = time.monotonic() + sekunden
    while time.monotonic() < grenze:
        if wert := bedingung():
            return wert
        time.sleep(0.02)
    raise AssertionError("Bedingung ist nicht eingetreten")


class FakeGeraet:
    """Ein Gerät, das sich an Missa anschliesst und Aufträge mit festen Antworten erledigt."""

    def __init__(
        self,
        basis: str,
        token: str,
        name: str = "laptop",
        sitzungen: tuple[str, ...] = ("laptop-chat",),
        werkzeuge: list[dict[str, Any]] | None = None,
    ) -> None:
        self.basis = basis
        self.token = token
        self.name = name
        self.sitzungen = list(sitzungen)
        self.werkzeuge = werkzeuge if werkzeuge is not None else [werkzeug("rechner_status")]
        #: Werkzeugname → Antwort (Text, dict mit ok/art/fehler, Funktion(auftrag) oder SCHWEIGEN).
        self.antworten: dict[str, Any] = {}
        self.auftraege: list[dict[str, Any]] = []
        self.meldungen: list[dict[str, Any]] = []
        self.abbrueche: list[str] = []
        self.ergebnis_antworten: list[httpx.Response] = []
        self.pings = 0
        self.verbunden = threading.Event()
        self._ausgefuehrt: set[str] = set()
        self._stopp = threading.Event()
        self._faden: threading.Thread | None = None
        self._client = httpx.Client(
            base_url=basis,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            trust_env=False,
        )

    # -------------------------------------------------------------- Vertrag

    def anmelden(self, **zusatz: Any) -> httpx.Response:
        nutzlast = {
            "protokoll": 1,
            "geraet": self.name,
            "sitzungen": self.sitzungen,
            "werkzeuge": self.werkzeuge,
            **zusatz,
        }
        return self._client.post("/api/geraete/anmelden", json=nutzlast)

    def verbinden(self) -> None:
        """Öffnet den Stream in einem eigenen Thread (wie das echte Gerät im Hintergrund)."""
        self._stopp.clear()
        self.verbunden.clear()
        self._faden = threading.Thread(target=self._lesen, daemon=True)
        self._faden.start()
        if not self.verbunden.wait(5):
            raise AssertionError("Stream ist nicht zustande gekommen")

    def trennen(self) -> None:
        self._stopp.set()
        if self._faden is not None:
            self._faden.join(5)
            self._faden = None

    def schliessen(self) -> None:
        self.trennen()
        self._client.close()

    def ergebnis(self, daten: dict[str, Any]) -> httpx.Response:
        antwort = self._client.post("/api/geraete/ergebnis", json=daten)
        self.ergebnis_antworten.append(antwort)
        return antwort

    # -------------------------------------------------------------- Stream

    def _lesen(self) -> None:
        art, daten = "message", ""
        try:
            with (
                httpx.Client(
                    base_url=self.basis,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=httpx.Timeout(10, read=30),
                    trust_env=False,
                ) as client,
                client.stream("GET", "/api/geraete/auftraege") as antwort,
            ):
                if antwort.status_code != 200:
                    return
                self.verbunden.set()
                for zeile in antwort.iter_lines():
                    if self._stopp.is_set():
                        return
                    if zeile.startswith(":"):
                        self.pings += "ping" in zeile
                    elif zeile.startswith("event:"):
                        art = zeile.removeprefix("event:").strip()
                    elif zeile.startswith("data:"):
                        daten += zeile.removeprefix("data:").strip()
                    elif not zeile:
                        if daten:
                            self._ereignis(art, json.loads(daten))
                        art, daten = "message", ""
        except httpx.HTTPError:
            return

    def _ereignis(self, art: str, daten: dict[str, Any]) -> None:
        if art == "meldung":
            self.meldungen.append(daten)
        elif art == "abbruch":
            self.abbrueche.append(daten["id"])
        elif art == "auftrag":
            self.auftraege.append(daten)
            if daten["id"] in self._ausgefuehrt:
                return  # jede Auftrags-ID höchstens einmal
            self._ausgefuehrt.add(daten["id"])
            threading.Thread(target=self._erledigen, args=(daten,), daemon=True).start()

    def _erledigen(self, auftrag: dict[str, Any]) -> None:
        antwort = self.antworten.get(
            auftrag["werkzeug"],
            {"ok": False, "art": "fehler", "fehler": "Werkzeug kenne ich nicht"},
        )
        if callable(antwort):
            antwort = antwort(auftrag)
        if antwort is SCHWEIGEN:
            return
        if isinstance(antwort, str):
            antwort = {"ok": True, "ergebnis": antwort}
        self.ergebnis({"id": auftrag["id"], **antwort})
