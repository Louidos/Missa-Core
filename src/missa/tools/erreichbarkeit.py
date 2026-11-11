"""Erreichbarkeit der eigenen Domains: Ausfall und Wiederherstellung melden.

Der :class:`ErreichbarkeitsWaechter` prüft regelmäßig (Standard: alle 5 Minuten)
jede konfigurierte Domain über die öffentliche Adresse (DNS, HTTPS-Antwort, gültiges
Zertifikat). Schlägt die Prüfung :data:`AUSFALL_NACH` Mal hintereinander fehl, meldet
Missa den Ausfall (Standard: ``wichtig``); klappt sie danach wieder, meldet sie die
Rückkehr (``info``). Der Zustand steht in der Tabelle ``erreichbarkeit`` und
überlebt Neustarts – auch das Ablaufdatum der Zertifikate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug
from missa.integrationen.erreichbarkeit import DomainErgebnis, ErreichbarkeitsPruefer

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS erreichbarkeit (
    domain TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('ok', 'gestoert')),
    seit TEXT NOT NULL,
    fehler_in_folge INTEGER NOT NULL DEFAULT 0,
    gemeldet INTEGER NOT NULL DEFAULT 0,
    letzte_pruefung TEXT NOT NULL,
    problem TEXT NOT NULL DEFAULT '',
    adressen TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    zertifikat_bis TEXT
);
"""

#: So viele Fehlschläge hintereinander gelten als Ausfall (kurze Aussetzer zählen nicht).
AUSFALL_NACH = 2


def _dauer_text(von: datetime, bis: datetime) -> str:
    minuten = max(1, round((bis - von).total_seconds() / 60))
    if minuten < 60:
        return f"{minuten} Minute{'n' if minuten != 1 else ''}"
    stunden, rest = divmod(minuten, 60)
    return f"{stunden} Std. {rest} Min." if rest else f"{stunden} Std."


class ErreichbarkeitsWaechter:
    """Merkt sich den Zustand jeder Domain und meldet Ausfall und Rückkehr."""

    def __init__(
        self,
        pruefer: ErreichbarkeitsPruefer,
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        *,
        stufe: AlarmStufe = AlarmStufe.WICHTIG,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.pruefer = pruefer
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self.stufe = stufe
        self._uhr = uhr or alarme.jetzt
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def zustand(self) -> dict[str, dict[str, Any]]:
        """Gespeicherter Zustand je Domain."""
        with verbinden(self.datei) as db:
            zeilen = db.execute("SELECT * FROM erreichbarkeit ORDER BY domain").fetchall()
        return {z["domain"]: dict(z) for z in zeilen}

    async def pruefen(self) -> list[Alarm]:
        """Regelmäßiger Auftrag: alle Domains prüfen und Änderungen melden."""
        jetzt = self.jetzt()
        gemeldet = []
        for ergebnis in await self.pruefer.alle_pruefen():
            alarm = self.verarbeiten(ergebnis, jetzt)
            if alarm is not None:
                gemeldet.append(alarm)
        return gemeldet

    def verarbeiten(self, ergebnis: DomainErgebnis, jetzt: datetime) -> Alarm | None:
        alt = self.zustand().get(ergebnis.domain)
        seit = datetime.fromisoformat(alt["seit"]).astimezone(self.zone) if alt else jetzt
        gemeldet = bool(alt and alt["gemeldet"])
        alarm = None
        if ergebnis.ok:
            if alt and alt["status"] != "ok":
                if gemeldet:
                    alarm = self.alarme.melden(
                        AlarmStufe.INFO,
                        f"{ergebnis.domain} ist wieder erreichbar",
                        f"Ausfall von {seit:%H:%M} bis {jetzt:%H:%M} Uhr "
                        f"({_dauer_text(seit, jetzt)}).",
                        schluessel=f"erreichbarkeit:{ergebnis.domain}:zurueck",
                        quelle="erreichbarkeit",
                    )
                    log.info("%s ist wieder erreichbar", ergebnis.domain)
                seit = jetzt
            status, fehler, gemeldet = "ok", 0, False
        else:
            if not alt or alt["status"] == "ok":
                seit = jetzt
            status, fehler = "gestoert", (alt["fehler_in_folge"] if alt else 0) + 1
            if fehler >= AUSFALL_NACH and not gemeldet:
                alarm = self.alarme.melden(
                    self.stufe,
                    f"{ergebnis.domain} ist nicht erreichbar",
                    f"Seit {seit:%H:%M} Uhr: {ergebnis.problem}. Geprüft von ai01 über die "
                    "öffentliche Domain.",
                    schluessel=f"erreichbarkeit:{ergebnis.domain}:ausfall",
                    quelle="erreichbarkeit",
                )
                gemeldet = alarm is not None
        bis = ergebnis.zertifikat_bis
        if bis is None and alt and alt["zertifikat_bis"]:
            bis = datetime.fromisoformat(alt["zertifikat_bis"])
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO erreichbarkeit (domain, status, seit, fehler_in_folge, gemeldet, "
                "letzte_pruefung, problem, adressen, http_status, zertifikat_bis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (domain) DO UPDATE SET "
                "status = excluded.status, seit = excluded.seit, "
                "fehler_in_folge = excluded.fehler_in_folge, gemeldet = excluded.gemeldet, "
                "letzte_pruefung = excluded.letzte_pruefung, problem = excluded.problem, "
                "adressen = excluded.adressen, http_status = excluded.http_status, "
                "zertifikat_bis = excluded.zertifikat_bis",
                (
                    ergebnis.domain,
                    status,
                    seit.astimezone(UTC).isoformat(timespec="seconds"),
                    fehler,
                    int(gemeldet),
                    jetzt.astimezone(UTC).isoformat(timespec="seconds"),
                    ergebnis.problem,
                    ",".join(ergebnis.adressen),
                    ergebnis.http_status,
                    bis.astimezone(UTC).isoformat(timespec="seconds") if bis else None,
                ),
            )
        return alarm

    def zeile(self, ergebnis: DomainErgebnis) -> str:
        if ergebnis.ok:
            text = f"- {ergebnis.domain}: erreichbar (HTTP {ergebnis.http_status}"
            if ergebnis.zertifikat_bis:
                tage = (ergebnis.zertifikat_bis - datetime.now(UTC)).days
                text += f", Zertifikat gültig bis {ergebnis.zertifikat_bis:%d.%m.%Y}, {tage} Tage"
            return text + ")"
        return f"- {ergebnis.domain}: NICHT erreichbar – {ergebnis.problem}"

    async def jetzt_pruefen(self, domain: str | None = None) -> str:
        """Werkzeug: prüft sofort (alle oder eine Domain) und zeigt das Ergebnis."""
        gesucht = (domain or "").strip().casefold()
        domains = [d for d in self.pruefer.domains if not gesucht or gesucht in d]
        if not domains:
            return (
                f"„{domain}“ prüfe ich nicht. Konfiguriert sind: "
                + (", ".join(self.pruefer.domains) or "keine")
                + " (ERREICHBARKEIT_DOMAINS)."
            )
        zeilen = ["Erreichbarkeit von außen (geprüft von ai01 über die öffentliche Domain):"]
        for name in domains:
            zeilen.append(self.zeile(await self.pruefer.pruefen(name)))
        gestoert = [d for d, z in self.zustand().items() if z["status"] != "ok" and z["gemeldet"]]
        if gestoert:
            zeilen.append("Als Ausfall gemeldet: " + ", ".join(gestoert))
        return "\n".join(zeilen)

    def kontext(self, _frage: str) -> str | None:
        """Kontextquelle: gemeldete Ausfälle im Systemprompt."""
        gestoert = [
            f"- {d}: {z['problem']}"
            for d, z in self.zustand().items()
            if z["status"] != "ok" and z["gemeldet"]
        ]
        if not gestoert:
            return None
        return "## Nicht erreichbare Domains\n" + "\n".join(gestoert)


def erreichbarkeit_werkzeuge(waechter: ErreichbarkeitsWaechter) -> list[Werkzeug]:
    return [
        Werkzeug(
            "erreichbarkeit_pruefen",
            "Prüft sofort, ob die eigenen Domains (z. B. cloud., git., ki.) von außen erreichbar "
            "sind: DNS, HTTPS-Antwort und gültiges Zertifikat. Optional nur eine Domain.",
            {
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "z. B. git"}},
            },
            waechter.jetzt_pruefen,
            stufe=Stufe.LESEN,
        )
    ]
