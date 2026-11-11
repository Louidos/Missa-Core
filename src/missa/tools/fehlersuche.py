"""Fehlersuche: Logs lesen, relevante Zeilen herausfiltern, wahrscheinliche Ursache erklären.

„Warum geht Nextcloud nicht?“ → Missa sucht die passenden Container und Dienste auf den
Hosts, liest deren Logs (Docker, journal, nextcloud.log) und das System-Log, nimmt den
Zustand aus Monitoring und Erreichbarkeit dazu und erkennt bekannte Ursachen im Code
(:data:`missa.integrationen.logs.URSACHEN`). Das Modell bekommt die Belege und erklärt
sie Louis verständlich. Lange Logs kommen in Teilen; eine sehr lange Analyse kann
Missa für die Nachtschicht vormerken (``nachtaufgabe_anlegen``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.integrationen.anmeldungen import NextcloudLog, nextcloud_fehler
from missa.integrationen.logs import relevante_zeilen, ursachen_erkennen
from missa.integrationen.ssh import SshFehler, SshVerbindung
from missa.tools.container import ContainerDienst, teil_lesen, teil_text, zeilen_lesen

log = logging.getLogger(__name__)

#: So viele relevante Zeilen zeigt die Fehlersuche höchstens.
MAX_BELEGE = 40
PRIORITAETEN = {
    "notfall": 0,
    "alarm": 1,
    "kritisch": 2,
    "fehler": 3,
    "error": 3,
    "warnung": 4,
    "warning": 4,
    "hinweis": 5,
    "info": 6,
}


def dienste_lesen(text: str) -> dict[str, list[tuple[str, str]]]:
    """``nextcloud:int01:nextcloud-app,nextcloud-db;caddy:rp01:caddy.service`` → Zuordnung.

    Je Dienst Host und Quellen: ein Container-Name, eine systemd-Unit (``.service``) oder
    leer (dann sucht Missa auf dem Host selbst).
    """
    dienste: dict[str, list[tuple[str, str]]] = {}
    for eintrag in (text or "").split(";"):
        if not eintrag.strip():
            continue
        teile = [t.strip() for t in eintrag.split(":")]
        if len(teile) < 2 or not teile[0] or not teile[1]:
            raise ValueError(f"Ungültiger Eintrag in FEHLERSUCHE_DIENSTE: {eintrag}")
        name, host = teile[0].casefold(), teile[1].casefold()
        quellen = [q.strip() for q in (teile[2] if len(teile) > 2 else "").split(",") if q.strip()]
        dienste.setdefault(name, []).extend((host, q) for q in (quellen or [""]))
    return dienste


def prioritaet_lesen(wert: Any) -> int | None:
    if wert in (None, ""):
        return None
    text = str(wert).strip().casefold()
    if text.isdigit() and 0 <= int(text) <= 7:
        return int(text)
    if text in PRIORITAETEN:
        return PRIORITAETEN[text]
    raise WerkzeugFehler("prioritaet: fehler, warnung, kritisch oder eine Zahl 0–7.")


def ist_unit(quelle: str) -> bool:
    return bool(re.search(r"\.(service|socket|timer|mount)$|\*", quelle))


@dataclass
class Bericht:
    """Was die Fehlersuche gefunden hat."""

    geprueft: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    zeilen: list[tuple[str, str]] = field(default_factory=list)  # (Quelle, Zeile)
    hinweise: list[str] = field(default_factory=list)
    zu_lang: bool = False


class Fehlersuche:
    """Liest Logs auf den Hosts und erklärt die wahrscheinliche Ursache eines Problems."""

    def __init__(
        self,
        ssh: SshVerbindung,
        container: ContainerDienst | None = None,
        *,
        dienste: dict[str, list[tuple[str, str]]] | None = None,
        nextcloud_log: NextcloudLog | None = None,
        server: Any = None,
        erreichbarkeit: Any = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.ssh = ssh
        self.container = container
        self.dienste = dienste or {}
        self.nextcloud_log = nextcloud_log
        self.server = server
        self.erreichbarkeit = erreichbarkeit
        self._uhr = uhr or (lambda: datetime.now().astimezone())

    def jetzt(self) -> datetime:
        return self._uhr()

    # -------------------------------------------------------------- journal

    async def journal_text(
        self,
        host: str,
        unit: str | None = None,
        prioritaet: int | None = None,
        stunden: float = 1,
        zeilen: int = 300,
        ident: str | None = None,
    ) -> str:
        seit = self.jetzt() - timedelta(hours=max(0.1, min(float(stunden), 24 * 14)))
        angaben = [f"seit=@{int(seit.timestamp())}", f"zeilen={zeilen}"]
        if unit:
            angaben.append(f"unit={unit}")
        if prioritaet is not None:
            angaben.append(f"prio={prioritaet}")
        if ident:
            angaben.append(f"ident={ident}")
        return await self.ssh.befehl(host, "journal", *angaben, timeout=40)

    async def journal_lesen(
        self,
        host: str,
        unit: str | None = None,
        prioritaet: str | int | None = None,
        stunden: float | str | None = None,
        zeilen: int | str | None = None,
        teil: int | str | None = None,
        nur_probleme: bool = False,
    ) -> str:
        name = self.ssh.host(host).name
        try:
            dauer = float(str(stunden).replace(",", ".")) if stunden not in (None, "") else 1.0
        except ValueError as fehler:
            raise WerkzeugFehler("stunden muss eine Zahl sein.") from fehler
        text = await self.journal_text(
            name, unit or None, prioritaet_lesen(prioritaet), dauer, zeilen_lesen(zeilen, 300)
        )
        log_zeilen = [z for z in text.splitlines() if z.strip() and not z.startswith("-- ")]
        if nur_probleme:
            auffaellig = relevante_zeilen(log_zeilen, 200)
            log_zeilen = [z + (f"  ({n}×)" if n > 1 else "") for z, n in auffaellig]
        kopf = f"System-Log (journal) von {name}" + (f", Unit {unit}" if unit else "")
        kopf += f", letzte {dauer:g} Stunde(n)"
        return teil_text(log_zeilen, teil_lesen(teil), kopf, "journal_lesen")

    # -------------------------------------------------------------- Fehlersuche

    async def _ziele(self, dienst: str, host: str | None) -> list[tuple[str, str, str]]:
        """(Host, Art, Name) für den Dienst: Container, Unit oder Nextcloud-Log."""
        ziele: list[tuple[str, str, str]] = []
        zuordnung = self.dienste.get(dienst, [])
        if host:
            hosts = [self.ssh.host(host).name]
        elif zuordnung:
            hosts = sorted({h for h, _ in zuordnung if h in self.ssh.hosts})
        else:
            hosts = self.ssh.namen
        for h, quelle in zuordnung:
            if h in hosts and quelle:
                ziele.append((h, "unit" if ist_unit(quelle) else "container", quelle))
        gesucht = {h for h, q in zuordnung if h in hosts and not q} or (
            set(hosts) if not zuordnung else set()
        )
        if self.container is not None and gesucht:
            ergebnisse = await asyncio.gather(
                *(self.container.liste(h, mit_werten=False) for h in sorted(gesucht)),
                return_exceptions=True,
            )
            for h, ergebnis in zip(sorted(gesucht), ergebnisse, strict=True):
                if isinstance(ergebnis, BaseException):
                    continue
                for c in ergebnis:
                    if dienst in c.name.casefold():
                        ziele.append((h, "container", c.name))
        if not any(art == "container" for _, art, _ in ziele):
            ziele += [(h, "unit", f"*{dienst}*") for h in sorted(gesucht)]
        if self.nextcloud_log is not None and "cloud" in dienst:
            ziele.append((self.nextcloud_log.host, "nextcloud", "nextcloud.log"))
        return list(dict.fromkeys(ziele))

    async def _ziel_lesen(self, ziel: tuple[str, str, str], bericht: Bericht) -> None:
        host, art, name = ziel
        if art == "container" and self.container is not None:
            try:
                c = await self.container.finden(host, name)
            except WerkzeugFehler as fehler:
                bericht.hinweise.append(str(fehler))
                return
            zustand = f"- Container {c.name} auf {host}: {c.status}"
            if problem := c.problem():
                zustand += f" ⚠️ {problem}"
            bericht.status.append(zustand)
            text = await self.ssh.befehl(host, "docker-logs", c.name, "400", timeout=40)
            quelle = f"{host}/{c.name}"
        elif art == "unit":
            text = await self.journal_text(host, unit=name, stunden=2, zeilen=400)
            quelle = f"{host}/{name}"
        elif art == "nextcloud" and self.nextcloud_log is not None:
            roh = await self.nextcloud_log.lesen(self.ssh, 500)
            text = "\n".join(nextcloud_fehler(roh))
            quelle = f"{host}/nextcloud.log"
        else:
            return
        bericht.geprueft.append(quelle)
        zeilen = [z for z in text.splitlines() if z.strip() and not z.startswith("-- ")]
        relevant = relevante_zeilen(zeilen, 200)
        if len(relevant) > MAX_BELEGE // 2:
            bericht.zu_lang = True
        bericht.zeilen += [(quelle, z + (f"  ({n}×)" if n > 1 else "")) for z, n in relevant]

    async def _system_lesen(self, host: str, bericht: Bericht) -> None:
        text = await self.journal_text(host, prioritaet=3, stunden=1, zeilen=150)
        bericht.geprueft.append(f"{host}/System-Log")
        zeilen = [z for z in text.splitlines() if z.strip() and not z.startswith("-- ")]
        bericht.zeilen += [
            (f"{host}/System", z + (f"  ({n}×)" if n > 1 else ""))
            for z, n in relevante_zeilen(zeilen, 15)
        ]

    def _umfeld(self, dienst: str, hosts: set[str], bericht: Bericht) -> None:
        """Was Monitoring und Erreichbarkeit schon wissen."""
        if self.server is not None:
            for wert in getattr(self.server, "aktuell", {}).values():
                if wert.ziel in hosts and wert.einheit == "%" and wert.wert >= 95:
                    bericht.status.append(f"- {wert.ziel}: {wert.text()} (fast voll!)")
                if wert.ziel in hosts and wert.metrik == "laeuft" and wert.wert < 1:
                    bericht.status.append(f"- VM {wert.ziel} läuft laut Proxmox nicht")
        if self.erreichbarkeit is not None:
            for domain, zustand in self.erreichbarkeit.zustand().items():
                kurz = domain.split(".")[0]
                if kurz and (kurz in dienst or dienst in kurz):
                    if zustand["status"] == "ok":
                        bericht.status.append(f"- {domain} ist von außen erreichbar")
                    else:
                        bericht.status.append(
                            f"- {domain} ist von außen NICHT erreichbar: {zustand['problem']}"
                        )

    async def suchen(self, dienst: str, host: str | None = None) -> str:
        name = " ".join(str(dienst or "").split()).casefold()
        if not name:
            raise WerkzeugFehler("Welcher Dienst geht nicht? Der Name fehlt (z. B. nextcloud).")
        if not re.fullmatch(r"[a-z0-9äöü._-]{1,60}", name):
            raise WerkzeugFehler(f"„{dienst}“ ist kein Dienst-Name, den ich suchen kann.")
        bericht = Bericht()
        ziele = await self._ziele(name, host)
        hosts = {h for h, _, _ in ziele}
        aufgaben = [self._ziel_lesen(z, bericht) for z in ziele]
        aufgaben += [self._system_lesen(h, bericht) for h in sorted(hosts)]
        for ergebnis in await asyncio.gather(*aufgaben, return_exceptions=True):
            if isinstance(ergebnis, SshFehler):
                bericht.hinweise.append(str(ergebnis))
            elif isinstance(ergebnis, BaseException):
                log.exception("Fehlersuche: unerwarteter Fehler", exc_info=ergebnis)
                bericht.hinweise.append(f"Unerwarteter Fehler: {ergebnis}")
        self._umfeld(name, hosts, bericht)
        return self._text(name, bericht)

    def _text(self, dienst: str, bericht: Bericht) -> str:
        zeilen = [f"Fehlersuche „{dienst}“."]
        zeilen.append(
            "Geprüft: "
            + (", ".join(dict.fromkeys(bericht.geprueft)) or "nichts – keine Quelle gefunden")
        )
        ursachen = [s for s in bericht.status if "⚠️" in s or "NICHT" in s or "fast voll" in s]
        erkannt = ursachen_erkennen([z for _, z in bericht.zeilen])
        if ursachen or erkannt:
            zeilen.append("Wahrscheinliche Ursache (vom Code erkannt):")
            for nummer, s in enumerate(ursachen, start=1):
                zeilen.append(f"{nummer}. {s.lstrip('- ')}")
            erste = len(ursachen) + 1
            for nummer, (ursache, anzahl, beispiel) in enumerate(erkannt, start=erste):
                zeilen.append(
                    f"{nummer}. {ursache.erklaerung} ({anzahl}× im Log, "
                    f"z. B.: „{beispiel[:200]}“)\n   Nächster Schritt: {ursache.tipp}"
                )
        else:
            zeilen.append("Eine eindeutige Ursache hat der Code nicht erkannt.")
        if bericht.status:
            zeilen.append("Zustand:")
            zeilen += bericht.status
        if bericht.zeilen:
            zeilen.append("Auffällige Log-Zeilen (neueste zuletzt):")
            zeilen += [f"[{q}] {z}" for q, z in bericht.zeilen[-MAX_BELEGE:]]
        elif bericht.geprueft:
            zeilen.append("In den Logs stehen keine auffälligen Zeilen.")
        if bericht.hinweise:
            zeilen.append("Nicht prüfbar: " + "; ".join(bericht.hinweise))
        if bericht.zu_lang:
            zeilen.append(
                "(Es gibt viele auffällige Zeilen. Mehr mit container_logs oder journal_lesen "
                "(teil=2 …); für eine gründliche Analyse aller Logs kannst du Louis anbieten, "
                "sie heute Nacht zu machen – nachtaufgabe_anlegen.)"
            )
        zeilen.append(
            "(Anweisung: Erkläre Louis in 2–4 einfachen Sätzen die wahrscheinliche Ursache und "
            "den nächsten Schritt. Stütz dich nur auf die Belege oben und sag ehrlich, wenn sie "
            "nicht eindeutig sind. Einen Neustart (container_neustarten) nur vorschlagen.)"
        )
        return "\n".join(zeilen)


def fehlersuche_werkzeuge(suche: Fehlersuche) -> list[Werkzeug]:
    return [
        Werkzeug(
            "fehlersuche",
            "Sucht die Ursache, wenn ein Dienst nicht geht („Warum geht Nextcloud nicht?“): "
            "liest Container- und System-Logs auf den Hosts, filtert die wichtigen Zeilen und "
            "erkennt bekannte Ursachen. Danach erklärst du es Louis verständlich.",
            {
                "type": "object",
                "properties": {
                    "dienst": {"type": "string", "description": "z. B. nextcloud, caddy, gitea"},
                    "host": {"type": "string", "description": "optional, z. B. int01"},
                },
                "required": ["dienst"],
            },
            suche.suchen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "journal_lesen",
            "Liest das System-Log (journalctl) eines Hosts, optional nur eine Unit oder ab einer "
            "Priorität. Lange Logs kommen in Teilen (teil=1 sind die neuesten).",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "z. B. wb01"},
                    "unit": {"type": "string", "description": "z. B. caddy.service"},
                    "prioritaet": {"type": "string", "description": "fehler, warnung, 0–7"},
                    "stunden": {"type": "number", "description": "Zeitraum, Standard 1"},
                    "zeilen": {"type": "integer", "description": "Standard 300"},
                    "teil": {"type": "integer", "description": "1 = neueste Zeilen"},
                    "nur_probleme": {"type": "boolean", "description": "nur auffällige Zeilen"},
                },
                "required": ["host"],
            },
            suche.journal_lesen,
            stufe=Stufe.LESEN,
        ),
    ]
