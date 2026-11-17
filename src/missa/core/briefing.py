"""Briefing nach einer Auszeit: eine Zusammenfassung statt vieler Einzelmeldungen.

Missa-Core vermerkt jede Minute „zuletzt gelaufen“ (Tabelle ``herzschlag``). Beim Start
ergibt sich daraus, wie lange der Dienst weg war. War die Auszeit länger als
``BRIEFING_AB_MINUTEN`` (Standard 30), kommt nach dem Start **eine** Nachricht:

- wie lange Missa weg war,
- welche Erinnerungen und Routinen nachgeholt werden (das Nachholen selbst macht wie bisher
  der Zeitplaner – das Briefing fasst nur zusammen),
- was während der Ruhezeit aufgelaufen ist (fällt das Ende der Ruhezeit mit dem Briefing
  zusammen, übernimmt das Briefing die Sammelmeldung: es gibt nur eine Nachricht),
- heutige Termine, ungelesene Mails (Anzahl und Absender), Auffälligkeiten am Server und
  fertige Nachtaufgaben.

Das Briefing wird aus Daten im Code gebaut. Die schlichte Fassung steht sofort als
Erinnerung im Zeitplaner (sie geht also auch nach einem Absturz raus); ist das Modell
erreichbar, formuliert Missa sie danach in ihrem Ton. Das Briefing zählt nicht gegen das
Tagesbudget der Denkschleife.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.lagebild import (
    Lagebild,
    erreichbarkeit_quelle,
    mails_quelle,
    server_quelle,
)
from missa.core.prompt import datum_lang
from missa.core.werkzeuge import fremd_markieren
from missa.core.zeitplaner import Erinnerung, Zeitplaner

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS herzschlag (
    name TEXT PRIMARY KEY,
    zeit TEXT NOT NULL
);
"""

HERZSCHLAG = "missa-core"
#: So lange wartet die schlichte Fassung, damit Missa sie noch formulieren kann.
VERZOEGERUNG = timedelta(minutes=3)
#: Sammelmeldungen der Ruhezeit, die bis so lange nach dem Briefing fällig wären, übernimmt es.
ZUSAMMEN = timedelta(minutes=30)

#: Lässt Missa einen Text in ihrem Ton formulieren (``ChatDienst.formulieren``).
Formulierer = Callable[[str, str], Awaitable[str]]


def _utc_text(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def dauer_text(dauer: timedelta) -> str:
    """``8 Stunden 5 Minuten``, ``2 Tage 3 Stunden``, ``45 Minuten``."""
    minuten = int(dauer.total_seconds() // 60)
    tage, rest = divmod(minuten, 24 * 60)
    stunden, minuten = divmod(rest, 60)
    teile = []
    if tage:
        teile.append(f"{tage} Tag{'e' if tage != 1 else ''}")
    if stunden:
        teile.append(f"{stunden} Stunde{'n' if stunden != 1 else ''}")
    if minuten and not tage:
        teile.append(f"{minuten} Minute{'n' if minuten != 1 else ''}")
    return " ".join(teile) or "weniger als eine Minute"


class Briefing:
    """Herzschlag und Briefing nach einer Auszeit."""

    def __init__(
        self,
        datei: Path,
        zone: ZoneInfo,
        zeitplaner: Zeitplaner,
        *,
        alarme: Any = None,
        hintergrund: Any = None,
        lagebild: Lagebild | None = None,
        formulierer: Formulierer | None = None,
        aktiv: bool = True,
        ab_minuten: int = 30,
        verzoegerung: timedelta = VERZOEGERUNG,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.zone = zone
        self.zeitplaner = zeitplaner
        self.alarme = alarme
        self.hintergrund = hintergrund
        #: Quellen für Termine, Mails und Server (fehlertolerant wie im Lagebild).
        self.lagebild = lagebild
        self.formulierer = formulierer
        self.aktiv = aktiv
        self.ab = timedelta(minutes=max(1, int(ab_minuten)))
        self.verzoegerung = verzoegerung
        self._uhr = uhr or zeitplaner.jetzt
        self._task: asyncio.Task[Any] | None = None
        self._daten: tuple[Any, ...] = ()
        self._basis = ""
        self._dazu: list[str] = []
        self._sofort = True
        self._erinnerung: Erinnerung | None = None
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Herzschlag

    def herzschlag(self) -> None:
        """Jede Minute: „zuletzt gelaufen“ vermerken."""
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO herzschlag (name, zeit) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET zeit = excluded.zeit",
                (HERZSCHLAG, _utc_text(self.jetzt())),
            )

    def zuletzt_gelaufen(self) -> datetime | None:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT zeit FROM herzschlag WHERE name = ?", (HERZSCHLAG,)
            ).fetchone()
        return datetime.fromisoformat(zeile["zeit"]).astimezone(self.zone) if zeile else None

    # -------------------------------------------------------------- Daten

    def _nachgeholt(self, jetzt: datetime) -> list[str]:
        zeilen = []
        for art, name in (("erinnerung", "Erinnerung"), ("routine", "Routine")):
            for eintrag in self.zeitplaner.offene(art):
                if eintrag.faellig <= jetzt and not eintrag.pausiert:
                    zeilen.append(
                        f"- {name} „{eintrag.text}“ (war für {eintrag.faellig:%H:%M} Uhr)"
                    )
        return zeilen

    def _ruhezeit_uebernehmen(self, faellig: datetime) -> list[str]:
        """Übernimmt Sammelmeldungen der Ruhezeit, die etwa mit dem Briefing fällig wären."""
        if self.alarme is None:
            return []
        grenze = faellig + ZUSAMMEN
        zeilen: list[str] = []
        for eintrag in self.zeitplaner.offene():
            if not (eintrag.schluessel or "").startswith("alarm-sammlung:"):
                continue
            if eintrag.faellig > grenze:
                continue
            aufgelaufen = self.alarme.aufgeschobene(eintrag.faellig)
            zeilen += self.alarme.sammel_zeilen(aufgelaufen)
            # Das Briefing bringt sie mit – sonst kämen zwei Nachrichten.
            self.zeitplaner.loeschen(eintrag.id)
        return zeilen

    def _nachtaufgaben(self, seit: datetime) -> list[str]:
        if self.hintergrund is None:
            return []
        return [
            f"- „{a.titel}“" + (" (fehlgeschlagen)" if a.status == "fehler" else "")
            for a in self.hintergrund.liste("nacht", 2)
            if a.status in ("fertig", "fehler") and a.beendet and a.beendet >= seit
        ]

    def schlichte_fassung(
        self,
        zuletzt: datetime,
        jetzt: datetime,
        nachgeholt: list[str],
        ruhezeit: list[str],
        nachtaufgaben: list[str],
        abschnitte: str = "",
    ) -> str:
        teile = [
            f"☀️ Briefing: Ich war {dauer_text(jetzt - zuletzt)} weg "
            f"(seit {datum_lang(zuletzt.date())}, {zuletzt:%H:%M} Uhr) und bin jetzt wieder da."
        ]
        if nachgeholt:
            teile.append("Nachgeholt (kommt gleich einzeln):\n" + "\n".join(nachgeholt))
        if ruhezeit:
            teile.append("Während der Ruhezeit aufgelaufen:\n" + "\n".join(ruhezeit))
        if abschnitte:
            teile.append(abschnitte)
        if nachtaufgaben:
            teile.append(
                "Fertige Nachtaufgaben (Details im Morgenbericht):\n" + "\n".join(nachtaufgaben)
            )
        return "\n\n".join(teile)

    # -------------------------------------------------------------- Ablauf

    def _zustellzeit(self, jetzt: datetime) -> tuple[datetime, bool]:
        """Wann das Briefing rausgeht – in der Ruhezeit erst an ihrem Ende."""
        if self.alarme is not None and self.alarme.in_ruhezeit(jetzt):
            return self.alarme.ruhezeit_ende(jetzt), False
        return jetzt + self.verzoegerung, True

    def beim_start(self) -> Erinnerung | None:
        """Vor dem Start des Zeitplaners aufrufen: legt bei langer Auszeit das Briefing an.

        Die schlichte Fassung steht sofort im Zeitplaner (fällig nach ``verzoegerung``, in
        der Ruhezeit an deren Ende). Gibt die Erinnerung zurück oder ``None``.
        """
        zuletzt = self.zuletzt_gelaufen()
        jetzt = self.jetzt()
        self.herzschlag()
        if not self.aktiv or zuletzt is None or jetzt - zuletzt < self.ab:
            return None
        faellig, self._sofort = self._zustellzeit(jetzt)
        self._daten = (
            zuletzt,
            jetzt,
            self._nachgeholt(jetzt),
            self._ruhezeit_uebernehmen(faellig),
            self._nachtaufgaben(zuletzt),
        )
        self._basis = self.schlichte_fassung(*self._daten)
        self._erinnerung = self.zeitplaner.anlegen(
            "Briefing nach der Auszeit",
            faellig,
            nachricht=self._basis,
            schluessel=f"briefing:{_utc_text(jetzt)}",
            herkunft="briefing",
        )
        if self.alarme is not None:
            # Was bis dahin in der Ruhezeit noch aufläuft, kommt mit ins Briefing.
            self.alarme.sammel_umleitung = self.ruhezeit_aufnehmen
        log.info("Briefing nach %s Auszeit angelegt", dauer_text(jetzt - zuletzt))
        return self._erinnerung

    def _offen(self) -> Erinnerung | None:
        if self._erinnerung is None:
            return None
        aktuell = self.zeitplaner.holen(self._erinnerung.id)
        return aktuell if aktuell is not None and aktuell.status == "offen" else None

    def _aktualisieren(self) -> None:
        aktuell = self._offen()
        if aktuell is None:
            return
        text = self._basis
        if self._dazu:
            text += "\n\nSeitdem während der Ruhezeit dazugekommen:\n" + "\n".join(self._dazu)
        if aktuell.nachricht != text:
            self.zeitplaner.aendern(aktuell.id, nachricht=text)

    def ruhezeit_aufnehmen(self, zustellen_ab: datetime) -> bool:
        """Von den Alarmen aufgerufen: Gehört die Sammelmeldung der Ruhezeit ins Briefing?

        ``True``, wenn das noch offene Briefing etwa zur selben Zeit kommt – dann steht der
        Alarm im Briefing und es gibt keine zweite Nachricht.
        """
        aktuell = self._offen()
        if aktuell is None or self.alarme is None:
            return False
        if abs(aktuell.faellig - zustellen_ab) > ZUSAMMEN:
            return False
        bekannt = set(self._daten[3]) if self._daten else set()
        self._dazu = [
            z
            for z in self.alarme.sammel_zeilen(self.alarme.aufgeschobene(zustellen_ab))
            if z not in bekannt
        ]
        self._aktualisieren()
        return True

    async def ausarbeiten(self, erinnerung: Erinnerung) -> str:
        """Ergänzt Termine, Mails und Server und lässt Missa das Briefing formulieren."""
        if not self._daten:
            return erinnerung.nachricht or ""
        abschnitte = ""
        if self.lagebild is not None and self.lagebild.quellen:
            stand = await self.lagebild.erstellen()
            abschnitte = stand.text(markiert=False)
        text = self.schlichte_fassung(*self._daten, abschnitte=abschnitte)
        self._basis = text
        self._aktualisieren()
        if self.formulierer is not None:
            auftrag = (
                "Du warst eine Weile nicht erreichbar und bist wieder da. Schreib Louis ein "
                "kurzes Briefing in deinem Ton. Behalte alle Fakten (Dauer, Nachgeholtes, "
                "Aufgelaufenes, Termine, Mails, Server, Nachtaufgaben), erfinde nichts dazu:\n"
                + fremd_markieren("Briefing", text)
            )
            self._basis = (await self.formulierer(auftrag, text)).strip() or text
            self._aktualisieren()
        aktuell = self._offen()
        if self._sofort and aktuell is not None:
            # Fertig: sofort zustellen statt auf die Verzögerung zu warten.
            self.zeitplaner.aendern(aktuell.id, zeitpunkt=self.jetzt())
        return self._basis

    def starten(self) -> None:
        """Im laufenden Event-Loop: Briefing anlegen und im Hintergrund ausarbeiten."""
        erinnerung = self.beim_start()
        if erinnerung is None:
            return

        async def ausarbeiten() -> None:
            try:
                await self.ausarbeiten(erinnerung)
            except Exception:
                log.exception(
                    "Briefing: Ausarbeiten fehlgeschlagen – die schlichte Fassung geht raus"
                )

        self._task = asyncio.create_task(ausarbeiten(), name="missa-briefing")

    async def stoppen(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None


def briefing_lagebild(
    zone: ZoneInfo,
    *,
    uhr: Callable[[], datetime] | None = None,
    kalender: Any = None,
    mail_dienst: Any = None,
    server: Any = None,
    erreichbarkeit: Any = None,
) -> Lagebild:
    """Die Quellen fürs Briefing: Termine heute, Mails, Server – nur, was eingerichtet ist."""
    lagebild = Lagebild(zone, uhr)
    if kalender is not None:

        def termine() -> str:
            heute = lagebild.jetzt().date().isoformat()
            return kalender.termine_anzeigen(heute, heute)

        lagebild.registrieren("Termine heute", termine)
    if mail_dienst is not None:
        lagebild.registrieren("Mails", mails_quelle(mail_dienst))
    if server is not None:
        lagebild.registrieren("Server", server_quelle(server))
    if erreichbarkeit is not None:
        lagebild.registrieren("Erreichbarkeit", erreichbarkeit_quelle(erreichbarkeit))
    return lagebild
