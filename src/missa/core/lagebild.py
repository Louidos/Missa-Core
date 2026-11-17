"""Lagebild: ein kompaktes Bild davon, was gerade los ist.

Das Lagebild sammelt aus allen eingerichteten Diensten je einen kurzen Abschnitt
(Termine, Erinnerungen, Alarme, Server, Hintergrund-Aufträge, Mails, Kontaktpflege,
Uhrzeit, letzte Nachricht von Louis). Es ist die Grundlage für die Denkschleife
(:mod:`missa.core.initiative`) und beantwortet für sich schon „Was ist gerade los?“.

- **Fehlertolerant** – wirft eine Quelle einen Fehler oder braucht sie länger als
  :data:`ZEITLIMIT` Sekunden, steht im Lagebild „<Quelle>: nicht erreichbar“, der Rest
  kommt trotzdem.
- **Begrenzt** – das ganze Lagebild bleibt unter ``INITIATIVE_LAGEBILD_ZEICHEN``. Wird es
  zu lang, werden die Abschnitte gleichmässig gekürzt; keine Quelle fällt ganz weg.
- **Fremdes markiert** – Inhalte aus fremden Quellen (Kalendertitel, Absender, Logs)
  werden wie überall mit :func:`~missa.core.werkzeuge.fremd_markieren` gekennzeichnet.
- **Fingerabdruck** – ein stabiler Hash ohne Uhrzeit: Bleibt er gleich, hat sich an der
  Lage nichts geändert (die Denkschleife spart sich dann den Modellaufruf).

Das Lagebild liest nur – es legt nichts an und ändert nichts.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden
from missa.core.prompt import datum_lang
from missa.core.werkzeuge import Stufe, Werkzeug, fremd_markieren

log = logging.getLogger(__name__)

#: So lange darf eine Quelle höchstens brauchen (Sekunden).
ZEITLIMIT = 10.0
#: Standard-Grösse des ganzen Lagebilds (Zeichen).
STANDARD_ZEICHEN = 3000
#: Kleinste erlaubte Grösse – darunter wäre jeder Abschnitt nur noch ein Wort.
MIN_ZEICHEN = 1000
NICHT_ERREICHBAR = "nicht erreichbar"
LEER = "(nichts)"
#: So viele Einträge listet eine Quelle höchstens auf.
MAX_EINTRAEGE = 10

#: Eine Quelle liefert einen kurzen Text oder ``None`` (darf async sein).
QuellenFunktion = Callable[[], str | Awaitable[str | None] | None]


@dataclass(frozen=True)
class Quelle:
    """Eine registrierte Quelle des Lagebilds."""

    name: str
    funktion: QuellenFunktion
    #: Inhalt stammt von aussen (Mail, Kalender, Logs) und wird markiert.
    fremd: bool = False
    #: Zählt für den Fingerabdruck (die Uhrzeit nicht).
    im_fingerabdruck: bool = True


@dataclass(frozen=True)
class Abschnitt:
    """Ergebnis einer Quelle."""

    name: str
    text: str | None
    status: str  # "ok", "leer" oder "nicht erreichbar"
    fremd: bool = False
    im_fingerabdruck: bool = True

    def als_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "fremd": self.fremd}


def _kuerzen(text: str, laenge: int) -> str:
    if len(text) <= laenge:
        return text
    if laenge <= 1:
        return "…"[:laenge]
    return text[: laenge - 1].rstrip() + "…"


@dataclass(frozen=True)
class Stand:
    """Ein erstelltes Lagebild."""

    zeit: datetime
    abschnitte: tuple[Abschnitt, ...]
    max_zeichen: int

    @property
    def fingerabdruck(self) -> str:
        """Stabiler Hash ohne Uhrzeit (und ohne die zufälligen Fremd-Markierungen)."""
        teile = [
            f"{a.name}\x1f{a.status}\x1f{a.text or ''}"
            for a in self.abschnitte
            if a.im_fingerabdruck
        ]
        return hashlib.sha256("\x1e".join(teile).encode()).hexdigest()

    @staticmethod
    def _kopf(abschnitt: Abschnitt) -> str:
        return f"### {abschnitt.name}\n"

    def _koerper(self, abschnitt: Abschnitt, text: str, markiert: bool) -> str:
        if abschnitt.status == NICHT_ERREICHBAR:
            return f"{abschnitt.name}: {NICHT_ERREICHBAR}"
        if abschnitt.fremd and markiert:
            return fremd_markieren(f"Lagebild/{abschnitt.name}", text).rstrip()
        return text

    def _anteile(self, markiert: bool) -> list[str]:
        """Die Texte der Abschnitte, gleichmässig gekürzt, damit alles ins Limit passt."""
        texte = [a.text or LEER for a in self.abschnitte]
        trenner = len("\n\n") * max(0, len(self.abschnitte) - 1)
        # Was jeder Abschnitt ohne seinen eigentlichen Text braucht (Kopf, Markierung).
        fest = [len(self._kopf(a)) + len(self._koerper(a, "", markiert)) for a in self.abschnitte]
        verfuegbar = self.max_zeichen - trenner - sum(fest)
        laengen = [
            0 if a.status == NICHT_ERREICHBAR else len(t)
            for a, t in zip(self.abschnitte, texte, strict=True)
        ]
        if sum(laengen) <= verfuegbar:
            return texte
        # Gleichmässig verteilen: Kurze Abschnitte bleiben ganz, der Rest teilt sich den Platz.
        verfuegbar = max(len(self.abschnitte), verfuegbar)
        offen = [i for i, laenge in enumerate(laengen) if laenge > 0]
        anteil = {i: 0 for i in offen}
        rest = verfuegbar
        while offen:
            gleich = rest // len(offen)
            kurze = [i for i in offen if laengen[i] <= gleich]
            if not kurze:
                for i in offen:
                    anteil[i] = gleich
                break
            for i in kurze:
                anteil[i] = laengen[i]
                rest -= laengen[i]
                offen.remove(i)
        return [_kuerzen(t, max(1, anteil[i])) if i in anteil else t for i, t in enumerate(texte)]

    def text(self, markiert: bool = True) -> str:
        """Das Lagebild als Text (höchstens ``max_zeichen`` Zeichen).

        ``markiert=False`` lässt die Fremd-Markierungen weg (für die API und für ein
        Werkzeug, dessen ganzes Ergebnis ohnehin als fremd markiert wird).
        """
        teile = [
            self._kopf(a) + self._koerper(a, t, markiert)
            for a, t in zip(self.abschnitte, self._anteile(markiert), strict=True)
        ]
        return "\n\n".join(teile)[: self.max_zeichen]

    def als_dict(self) -> dict[str, Any]:
        text = self.text(markiert=False)
        return {
            "zeit": self.zeit.isoformat(timespec="seconds"),
            "fingerabdruck": self.fingerabdruck,
            "zeichen": len(text),
            "limit": self.max_zeichen,
            "text": text,
            "quellen": [a.als_dict() for a in self.abschnitte],
        }


class Lagebild:
    """Sammelt registrierte Quellen zu einem kompakten Lagebild."""

    def __init__(
        self,
        zone: ZoneInfo,
        uhr: Callable[[], datetime] | None = None,
        max_zeichen: int = STANDARD_ZEICHEN,
        zeitlimit: float = ZEITLIMIT,
    ) -> None:
        self.zone = zone
        self._uhr = uhr or (lambda: datetime.now(zone))
        self.max_zeichen = max(MIN_ZEICHEN, int(max_zeichen))
        self.zeitlimit = zeitlimit
        self.quellen: list[Quelle] = []

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def registrieren(
        self,
        name: str,
        funktion: QuellenFunktion,
        *,
        fremd: bool = False,
        im_fingerabdruck: bool = True,
    ) -> None:
        """Registriert eine Quelle; eine Quelle mit gleichem Namen wird ersetzt."""
        self.quellen = [q for q in self.quellen if q.name != name]
        self.quellen.append(Quelle(name, funktion, fremd, im_fingerabdruck))

    @property
    def namen(self) -> list[str]:
        return [q.name for q in self.quellen]

    async def _abfragen(self, quelle: Quelle) -> Abschnitt:
        async def aufrufen() -> str | None:
            if inspect.iscoroutinefunction(quelle.funktion):
                return await quelle.funktion()
            ergebnis = await asyncio.to_thread(quelle.funktion)
            if inspect.isawaitable(ergebnis):
                return await ergebnis
            return ergebnis

        try:
            text = await asyncio.wait_for(aufrufen(), timeout=self.zeitlimit)
        except TimeoutError:
            log.warning("Lagebild: Quelle %s braucht zu lange", quelle.name)
            return Abschnitt(
                quelle.name, None, NICHT_ERREICHBAR, quelle.fremd, quelle.im_fingerabdruck
            )
        except Exception as fehler:
            log.warning("Lagebild: Quelle %s nicht erreichbar (%s)", quelle.name, fehler)
            return Abschnitt(
                quelle.name, None, NICHT_ERREICHBAR, quelle.fremd, quelle.im_fingerabdruck
            )
        text = str(text).strip() if text is not None else ""
        return Abschnitt(
            quelle.name,
            text or None,
            "ok" if text else "leer",
            quelle.fremd,
            quelle.im_fingerabdruck,
        )

    async def erstellen(self) -> Stand:
        """Fragt alle Quellen gleichzeitig ab und baut das Lagebild."""
        abschnitte = await asyncio.gather(*(self._abfragen(q) for q in self.quellen))
        return Stand(self.jetzt(), tuple(abschnitte), self.max_zeichen)

    async def fingerabdruck(self) -> str:
        return (await self.erstellen()).fingerabdruck


# ----------------------------------------------------------------------------- Quellen


def _utc_text(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _liste(zeilen: list[str], ohne: str) -> str:
    if not zeilen:
        return ohne
    mehr = len(zeilen) - MAX_EINTRAEGE
    zeilen = zeilen[:MAX_EINTRAEGE]
    if mehr > 0:
        zeilen.append(f"- … und {mehr} weitere")
    return "\n".join(zeilen)


def uhrzeit_quelle(lagebild: Lagebild) -> QuellenFunktion:
    def quelle() -> str:
        jetzt = lagebild.jetzt()
        return f"{datum_lang(jetzt.date())}, {jetzt:%H:%M} Uhr"

    return quelle


def termine_quelle(kalender: Any, lagebild: Lagebild) -> QuellenFunktion:
    """Termine heute und morgen (aus allen Kalendern)."""

    def quelle() -> str:
        heute: date = lagebild.jetzt().date()
        return kalender.termine_anzeigen(heute.isoformat(), (heute + timedelta(days=1)).isoformat())

    return quelle


def erinnerungen_quelle(zeitplaner: Any) -> QuellenFunktion:
    """Offene Erinnerungen der nächsten 24 Stunden."""

    def quelle() -> str:
        grenze = zeitplaner.jetzt() + timedelta(hours=24)
        zeilen = [e.zeile() for e in zeitplaner.offene() if e.faellig <= grenze and not e.pausiert]
        return _liste(zeilen, "Keine Erinnerungen in den nächsten 24 Stunden.")

    return quelle


def alarme_quelle(alarme: Any) -> QuellenFunktion:
    """Alarme der letzten 24 Stunden, auch aufgeschobene."""

    def quelle() -> str:
        liste = alarme.liste(alarme.jetzt() - timedelta(hours=24), 50)
        zeilen = [a.zeile() for a in reversed(liste)]
        return _liste(zeilen, "Keine Alarme in den letzten 24 Stunden.")

    return quelle


def server_quelle(server: Any) -> QuellenFunktion:
    """Serverzustand: aktive Alarme und Quellen, die nicht antworten."""

    def quelle() -> str:
        zeilen = [f"- {a}" for a in server.aktive_alarme()]
        for name, fehler in sorted(dict(server.quellen_fehler).items()):
            zeilen.append(f"- Messquelle {name} antwortet nicht ({fehler})")
        if not zeilen:
            return "Alles im grünen Bereich."
        return _liste(zeilen, "")

    return quelle


def erreichbarkeit_quelle(erreichbarkeit: Any) -> QuellenFunktion:
    """Erreichbarkeit der eigenen Domains von aussen."""

    def quelle() -> str:
        zustand = erreichbarkeit.zustand()
        gestoert = [
            f"- {d}: {z.get('problem') or z.get('status')}"
            for d, z in zustand.items()
            if z.get("status") != "ok"
        ]
        if not zustand:
            return "Noch nicht geprüft."
        if not gestoert:
            return f"Alle {len(zustand)} Domains erreichbar."
        return _liste(gestoert, "")

    return quelle


def hintergrund_quelle(hintergrund: Any) -> QuellenFunktion:
    """Wartende, laufende und in den letzten 24 Stunden fertige Hintergrund-Aufträge."""

    def quelle() -> str:
        auftraege = hintergrund.liste("sofort", 1) + hintergrund.liste("nacht", 1)
        grenze = hintergrund.jetzt() - timedelta(hours=24)
        zeilen = []
        for a in sorted(auftraege, key=lambda a: a.id):
            if a.status in ("wartet", "laeuft"):
                zeilen.append(f"- #{a.id} „{a.titel}“ ({a.art}) – {a.status}")
            elif a.status in ("fertig", "fehler") and a.beendet and a.beendet >= grenze:
                wann = f"{a.beendet:%d.%m. %H:%M}"
                zeilen.append(f"- #{a.id} „{a.titel}“ ({a.art}) – {a.status} am {wann}")
        return _liste(zeilen, "Keine wartenden oder neuen fertigen Aufträge.")

    return quelle


def mails_quelle(mail_dienst: Any) -> QuellenFunktion:
    """Ungelesene Mails: nur Anzahl und Absender (keine Betreffe)."""

    def quelle() -> str:
        koepfe = mail_dienst.konto.koepfe("INBOX", 50, True)
        if not koepfe:
            return "Keine ungelesenen Mails."
        absender = []
        for kopf in koepfe:
            name, adresse = parseaddr(kopf.von)
            wer = " ".join((name or adresse or kopf.von).split())
            if wer and wer not in absender:
                absender.append(wer)
        anzahl = f"{len(koepfe)}{'+' if len(koepfe) >= 50 else ''}"
        zeigen = absender[:MAX_EINTRAEGE]
        mehr = f" und {len(absender) - len(zeigen)} weitere" if len(absender) > len(zeigen) else ""
        return f"{anzahl} ungelesen, von: {', '.join(zeigen)}{mehr}"

    return quelle


def kontaktpflege_quelle(kontaktpflege: Any) -> QuellenFunktion:
    """Wichtige Personen, bei denen der gewünschte Abstand überschritten ist."""

    def quelle() -> str:
        heute = kontaktpflege.jetzt().date()
        zeilen = []
        for eintrag in kontaktpflege.alle(nur_wichtige=True):
            if not eintrag.abstand_tage:
                continue
            seit = eintrag.letzter_kontakt or eintrag.erstellt
            if (heute - seit).days > eintrag.abstand_tage:
                zeilen.append(eintrag.zeile(heute))
        return _liste(zeilen, "Niemand ist fällig.")

    return quelle


def letzte_nachricht_quelle(datei: Path, zone: ZoneInfo) -> QuellenFunktion:
    """Wann Louis zuletzt geschrieben hat (aus dem gespeicherten Verlauf)."""

    def quelle() -> str:
        with verbinden(datei) as db:
            zeile = db.execute(
                "SELECT MAX(erstellt) AS zuletzt FROM nachrichten WHERE rolle = 'user'"
            ).fetchone()
        if not zeile or not zeile["zuletzt"]:
            return "Noch keine Nachricht gespeichert."
        zuletzt = datetime.fromisoformat(zeile["zuletzt"]).astimezone(zone)
        return f"{datum_lang(zuletzt.date())}, {zuletzt:%H:%M} Uhr"

    return quelle


def lagebild_bauen(
    zone: ZoneInfo,
    *,
    uhr: Callable[[], datetime] | None = None,
    max_zeichen: int = STANDARD_ZEICHEN,
    datenbank: Path | None = None,
    zeitplaner: Any = None,
    alarme: Any = None,
    kalender: Any = None,
    server: Any = None,
    erreichbarkeit: Any = None,
    hintergrund: Any = None,
    mail_dienst: Any = None,
    kontaktpflege: Any = None,
) -> Lagebild:
    """Baut das Lagebild mit allen Quellen, deren Dienst eingerichtet ist."""
    lagebild = Lagebild(zone, uhr, max_zeichen)
    lagebild.registrieren("Uhrzeit", uhrzeit_quelle(lagebild), im_fingerabdruck=False)
    if kalender is not None:
        lagebild.registrieren("Termine", termine_quelle(kalender, lagebild), fremd=True)
    if zeitplaner is not None:
        lagebild.registrieren("Erinnerungen", erinnerungen_quelle(zeitplaner))
    if alarme is not None:
        lagebild.registrieren("Alarme", alarme_quelle(alarme), fremd=True)
    if server is not None:
        lagebild.registrieren("Server", server_quelle(server))
    if erreichbarkeit is not None:
        lagebild.registrieren("Erreichbarkeit", erreichbarkeit_quelle(erreichbarkeit))
    if hintergrund is not None:
        lagebild.registrieren("Hintergrund-Aufträge", hintergrund_quelle(hintergrund), fremd=True)
    if mail_dienst is not None:
        lagebild.registrieren("Mails", mails_quelle(mail_dienst), fremd=True)
    if kontaktpflege is not None:
        lagebild.registrieren("Kontaktpflege", kontaktpflege_quelle(kontaktpflege))
    if datenbank is not None:
        lagebild.registrieren(
            "Letzte Nachricht von Louis", letzte_nachricht_quelle(datenbank, zone)
        )
    return lagebild


# ----------------------------------------------------------------------------- Werkzeug


def lagebild_werkzeuge(lagebild: Lagebild) -> list[Werkzeug]:
    async def anzeigen() -> str:
        stand = await lagebild.erstellen()
        # Ohne eigene Markierung: Der Werkzeugkasten markiert das ganze Ergebnis als fremd.
        return f"Lagebild ({stand.zeit:%d.%m. %H:%M} Uhr):\n\n{stand.text(markiert=False)}"

    return [
        Werkzeug(
            "lagebild_anzeigen",
            "Zeigt eine kurze Übersicht, was gerade los ist: Termine heute und morgen, "
            "Erinnerungen, Alarme, Server, Hintergrund-Aufträge, ungelesene Mails, "
            "Kontaktpflege. Für Fragen wie „Was ist gerade los?“ oder „Gibt's was Neues?“.",
            {"type": "object", "properties": {}},
            anzeigen,
            stufe=Stufe.LESEN,
        )
    ]
