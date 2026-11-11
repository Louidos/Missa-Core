"""Messwerte aus dem eigenen Monitoring-Tool und dem Homepage-Dashboard (HTTP/JSON, nur lesen).

Jede Quelle liefert eine Liste von :class:`Messwert` – das ist die Schnittstelle,
gegen die die Server-Alarme arbeiten. Eine neue Quelle braucht nur einen ``name``
und eine async-Methode ``messwerte()``. Die Formate stehen in ``docs/monitoring.md``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

#: Zeitlimit für eine Abfrage (Sekunden).
TIMEOUT = 10.0


class QuellenFehler(Exception):
    """Eine Monitoring-Quelle hat nicht oder unbrauchbar geantwortet."""


@dataclass(frozen=True)
class Messwert:
    """Ein Wert für ein Ziel (VM, Host, Dienst), z. B. ``ai01`` / ``cpu`` / ``93.5`` ``%``."""

    ziel: str
    metrik: str
    wert: float
    einheit: str = "%"
    quelle: str = ""

    @property
    def schluessel(self) -> tuple[str, str]:
        return (self.ziel, self.metrik)

    def text(self) -> str:
        return f"{metrik_titel(self.metrik)} {wert_text(self.wert, self.einheit)}"

    def als_dict(self) -> dict[str, Any]:
        return {
            "ziel": self.ziel,
            "metrik": self.metrik,
            "wert": self.wert,
            "einheit": self.einheit,
            "quelle": self.quelle,
        }


class Quelle(Protocol):
    """Eine Monitoring-Quelle. Wirft :class:`QuellenFehler`, wenn sie nicht antwortet."""

    name: str

    async def messwerte(self) -> list[Messwert]: ...


# ----------------------------------------------------------------------------- Namen

#: Bekannte Messwerte mit ihren Einheiten.
METRIK_EINHEITEN = {
    "cpu": "%",
    "ram": "%",
    "disk": "%",
    "laeuft": "",
    "erreichbar": "",
    "antwortzeit": "ms",
}
METRIK_TITEL = {
    "cpu": "CPU",
    "ram": "RAM",
    "disk": "Disk",
    "laeuft": "läuft",
    "erreichbar": "erreichbar",
    "antwortzeit": "Antwortzeit",
}
#: Wortteile, an denen der Code erkennt, welcher Messwert gemeint ist (Reihenfolge zählt:
#: „Festplattenauslastung“ ist Disk, „RAM-Auslastung“ RAM, „Auslastung“ allein CPU).
#: Kurze Wörter (bis 4 Zeichen) zählen nur am Anfang eines Wortes, längere auch mitten drin.
METRIK_WOERTER = (
    ("disk", ("disk", "platte", "speicherplatz", "storage", "datenträger")),
    ("ram", ("ram", "arbeitsspeicher", "speicher", "memory", "mem")),
    ("cpu", ("cpu", "prozessor", "auslastung")),
    ("antwortzeit", ("antwortzeit", "latenz", "latency", "ping")),
    ("erreichbar", ("erreichbar", "online")),
    ("laeuft", ("läuft", "laeuft", "laufen", "status", "running")),
)


def metrik_verstehen(text: Any) -> str:
    """``"Auslastung"`` → ``cpu``, ``"Arbeitsspeicher"`` → ``ram``, Unbekanntes bleibt (klein)."""
    roh = " ".join(str(text or "").casefold().split())
    if not roh:
        return "cpu"
    if roh in METRIK_EINHEITEN:
        return roh
    teile = re.findall(r"[a-zäöüß0-9]+", roh)
    for metrik, woerter in METRIK_WOERTER:
        for wort in woerter:
            if len(wort) <= 4 and any(teil.startswith(wort) for teil in teile):
                return metrik
            if len(wort) > 4 and wort in roh:
                return metrik
    return re.sub(r"[^a-z0-9äöüß_.-]+", "_", roh).strip("_") or "cpu"


def metrik_titel(metrik: str) -> str:
    return METRIK_TITEL.get(metrik, metrik)


def wert_text(wert: float, einheit: str) -> str:
    if einheit == "%":
        return f"{wert:.0f} %"
    if einheit == "ms":
        return f"{wert:.0f} ms"
    if einheit == "" and wert in (0, 1):
        return "ja" if wert else "nein"
    return f"{wert:g}" + (f" {einheit}" if einheit else "")


def zahl_lesen(wert: Any) -> float | None:
    if isinstance(wert, bool):
        return float(wert)
    if isinstance(wert, int | float):
        zahl = float(wert)
    elif isinstance(wert, str):
        try:
            zahl = float(wert.strip().rstrip("%").strip().replace(",", "."))
        except ValueError:
            return None
    else:
        return None
    return zahl if math.isfinite(zahl) else None


def ziel_normal(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


# ----------------------------------------------------------------------------- HTTP


async def json_holen(
    url: str,
    *,
    token: str = "",
    params: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = TIMEOUT,
) -> Any:
    """GET auf ``url`` und JSON zurück – Fehler als :class:`QuellenFehler`."""
    kopf = {"Accept": "application/json"}
    if token:
        kopf["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout, trust_env=False
        ) as client:
            antwort = await client.get(url, params=params, headers=kopf)
    except httpx.HTTPError as fehler:
        grund = str(fehler) or type(fehler).__name__
        raise QuellenFehler(f"{url} nicht erreichbar: {grund}") from fehler
    if antwort.status_code >= 400:
        raise QuellenFehler(f"{url} antwortet mit HTTP {antwort.status_code}")
    try:
        return antwort.json()
    except ValueError as fehler:
        raise QuellenFehler(f"{url} liefert kein JSON") from fehler


# ----------------------------------------------------------------------------- Eigenes Tool

ZIEL_FELDER = ("ziel", "host", "name", "vm", "server", "target")
METRIK_FELDER = ("messwert", "metrik", "metric", "typ", "type", "key")
WERT_FELDER = ("wert", "value", "val")


def _erstes(eintrag: dict[str, Any], felder: Iterable[str]) -> Any:
    for feld in felder:
        if feld in eintrag and eintrag[feld] not in (None, ""):
            return eintrag[feld]
    return None


def json_auslesen(daten: Any, quelle: str = "monitoring") -> list[Messwert]:
    """Liest Messwerte aus einem der beiden Formate in ``docs/monitoring.md``.

    Liste: ``{"messwerte": [{"ziel": "ai01", "messwert": "cpu", "wert": 93.5}]}`` (auch
    als reine Liste oder mit englischen Feldnamen). Verschachtelt:
    ``{"ai01": {"cpu": 93.5, "ram": 41}}``. Unbrauchbare Einträge werden übersprungen.
    """
    werte: list[Messwert] = []

    def hinzu(ziel: Any, metrik: Any, wert: Any, einheit: Any = None) -> None:
        zahl = zahl_lesen(wert)
        ziel_text = ziel_normal(ziel)
        if zahl is None or not ziel_text or metrik in (None, ""):
            return
        name = metrik_verstehen(metrik)
        werte.append(
            Messwert(
                ziel_text,
                name,
                zahl,
                str(einheit) if einheit is not None else METRIK_EINHEITEN.get(name, ""),
                quelle,
            )
        )

    if isinstance(daten, dict):
        liste = _erstes(daten, ("messwerte", "metrics", "werte", "values", "data"))
        if liste is None:
            for ziel, metriken in daten.items():
                if isinstance(metriken, dict):
                    for metrik, wert in metriken.items():
                        hinzu(ziel, metrik, wert)
            return werte
        daten = liste
    if isinstance(daten, list):
        for eintrag in daten:
            if isinstance(eintrag, dict):
                hinzu(
                    _erstes(eintrag, ZIEL_FELDER),
                    _erstes(eintrag, METRIK_FELDER),
                    _erstes(eintrag, WERT_FELDER),
                    _erstes(eintrag, ("einheit", "unit")),
                )
    return werte


class JsonQuelle:
    """Louis' eigenes Monitoring-Tool: ``MONITORING_URL`` liefert Messwerte als JSON."""

    name = "monitoring"

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.url = url
        self.token = token
        self.transport = transport
        self.timeout = timeout

    async def messwerte(self) -> list[Messwert]:
        daten = await json_holen(
            self.url, token=self.token, transport=self.transport, timeout=self.timeout
        )
        werte = json_auslesen(daten, self.name)
        if not werte and daten:
            raise QuellenFehler(
                f"{self.url}: Format nicht erkannt (siehe docs/monitoring.md) – keine Messwerte"
            )
        return werte


# ----------------------------------------------------------------------------- Homepage


def _dienste(gruppen: Any, pfad: tuple[str, ...] = ()) -> Iterable[tuple[str, dict[str, Any]]]:
    """Alle Dienste aus ``/api/services`` von Homepage (Gruppen auch verschachtelt)."""
    if not isinstance(gruppen, list):
        return
    for gruppe in gruppen:
        if not isinstance(gruppe, dict):
            continue
        name = str(gruppe.get("name", ""))
        for dienst in gruppe.get("services") or []:
            if isinstance(dienst, dict) and dienst.get("name"):
                yield name, dienst
        yield from _dienste(gruppe.get("groups"), (*pfad, name))


class HomepageQuelle:
    """Homepage-Dashboard (gethomepage.dev): Status der Dienste mit ``siteMonitor``.

    Liest ``/api/services`` und für jeden Dienst mit ``siteMonitor`` dessen Status über
    ``/api/siteMonitor``. Ergebnis je Dienst: ``erreichbar`` (1/0) und ``antwortzeit`` (ms).
    """

    name = "homepage"

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self.transport = transport
        self.timeout = timeout

    async def messwerte(self) -> list[Messwert]:
        gruppen = await json_holen(
            f"{self.url}/api/services", transport=self.transport, timeout=self.timeout
        )
        werte: list[Messwert] = []
        for gruppe, dienst in _dienste(gruppen):
            if not dienst.get("siteMonitor"):
                continue
            ziel = ziel_normal(dienst["name"])
            try:
                status = await json_holen(
                    f"{self.url}/api/siteMonitor",
                    params={"groupName": gruppe, "serviceName": str(dienst["name"])},
                    transport=self.transport,
                    timeout=self.timeout,
                )
            except QuellenFehler:
                status = {}
            code = zahl_lesen(status.get("status")) if isinstance(status, dict) else None
            ok = code is not None and 0 < code < 500
            werte.append(Messwert(ziel, "erreichbar", 1.0 if ok else 0.0, "", self.name))
            latenz = zahl_lesen(status.get("latency")) if isinstance(status, dict) else None
            if ok and latenz is not None:
                werte.append(Messwert(ziel, "antwortzeit", latenz, "ms", self.name))
        return werte
