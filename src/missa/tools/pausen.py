"""Pausen und Puffer: Missa achtet von sich aus darauf, dass im Kalender Luft bleibt.

Geprüft wird jeden Abend (``PAUSEN_PRUEFZEIT``) der nächste Tag und bei jeder neuen
oder verschobenen Terminanlage der betroffene Tag:

- **kein Puffer**: Termine direkt hintereinander (weniger als ``PAUSEN_PUFFER_MINUTEN``)
- **Überschneidung**: zwei Termine gleichzeitig
- **keine Mittagspause**: keine ``PAUSEN_MITTAG_MINUTEN`` zwischen ``PAUSEN_MITTAG_VON``
  und ``PAUSEN_MITTAG_BIS``
- **zu voller Tag**: mehr als ``PAUSEN_MAX_STUNDEN`` Termine
- **fehlende Wegzeit**: Termine an verschiedenen Orten mit weniger als
  ``PAUSEN_WEGZEIT_MINUTEN`` dazwischen

Am Abend trägt Missa Puffer- und Pausenblöcke selbst im Kalender ``PAUSEN_KALENDER``
ein (eigene Farbe, als ihre Blöcke markiert, protokolliert und rückgängig machbar) und
sagt danach Bescheid. Bei einer neuen Terminanlage meldet sie sich mit einem Hinweis und
einem Vorschlag, wann der Termin besser passen würde.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from missa.core.prompt import datum_lang
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.core.zeitangaben import wandzeit
from missa.tools.kalender import Belegung, freie_luecke
from missa.tools.nextcloud import datum_kurz, zeitpunkt_lesen

if TYPE_CHECKING:
    from missa.core.rechte import Rechte
    from missa.core.zeitplaner import Zeitplaner
    from missa.tools.kalender import Kalender

log = logging.getLogger(__name__)

PAUSEN_WOERTER = re.compile(r"pause|mittag|lunch|zmittag|essen|puffer", re.IGNORECASE)
#: Rahmen für Vorschläge, wann ein Termin besser passt.
TAG_VON = time(7, 0)
TAG_BIS = time(20, 0)

Formulierer = Callable[[str, str], Awaitable[str]]


@dataclass(frozen=True)
class PausenRegeln:
    """Grenzwerte – alle über die Einstellungen änderbar."""

    puffer: timedelta = timedelta(minutes=10)
    mittag_dauer: timedelta = timedelta(minutes=30)
    mittag_von: time = time(11, 30)
    mittag_bis: time = time(14, 0)
    max_stunden: float = 8.0
    wegzeit: timedelta = timedelta(minutes=30)
    kalender: str = "Pausen"
    farbe: str = "türkis"


@dataclass(frozen=True)
class Befund:
    art: str  # kein_puffer, ueberlappung, keine_mittagspause, kein_platz_mittag, zu_voll, wegzeit
    text: str


@dataclass(frozen=True)
class Block:
    """Ein Puffer- oder Pausenblock, den Missa eintragen möchte."""

    art: str  # puffer oder mittag
    start: datetime
    ende: datetime

    @property
    def titel(self) -> str:
        return "🍽️ Mittagspause" if self.art == "mittag" else "⏸️ Puffer"

    def zeile(self) -> str:
        return f"{self.titel} {self.start:%H:%M}–{self.ende:%H:%M}"


def _minuten(abstand: timedelta) -> int:
    return int(abstand.total_seconds() // 60)


def _ist_pause(block: Belegung, regeln: PausenRegeln) -> bool:
    return bool(
        block.art
        or block.kalender.casefold() == regeln.kalender.casefold()
        or PAUSEN_WOERTER.search(block.titel)
    )


def _kurz(block: Belegung) -> str:
    return f"„{block.titel or 'Termin'}“ ({block.start:%H:%M}–{block.ende:%H:%M})"


def _orte_verschieden(a: Belegung, b: Belegung) -> bool:
    return bool(a.ort and b.ort and a.ort.strip().casefold() != b.ort.strip().casefold())


def tag_pruefen(
    tag: date, belegungen: list[Belegung], regeln: PausenRegeln, zone: ZoneInfo
) -> tuple[list[Befund], list[Block]]:
    """Prüft einen Tag. Gibt Befunde und die Blöcke zurück, die Missa eintragen würde."""
    beginn = wandzeit(tag, time(0), zone)
    ende = beginn + timedelta(days=1)
    am_tag = [
        b
        for b in belegungen
        if not b.ganztaegig and not b.frei and b.start < ende and b.ende > beginn
    ]
    termine = sorted((b for b in am_tag if not _ist_pause(b, regeln)), key=lambda b: b.start)
    pausen = [b for b in am_tag if _ist_pause(b, regeln)]
    befunde: list[Befund] = []
    bloecke: list[Block] = []
    if not termine:
        return befunde, bloecke

    def frei(von: datetime, bis: datetime) -> bool:
        return bis > von and not any(b.start < bis and b.ende > von for b in am_tag)

    # ------------------------------------------------ Puffer, Überschneidungen, Wegzeit
    kette: list[Belegung] = [termine[0]]
    ketten: list[list[Belegung]] = []
    for vorher, nachher in itertools.pairwise(termine):
        luecke = nachher.start - vorher.ende
        if luecke < timedelta(0):
            befunde.append(
                Befund("ueberlappung", f"{_kurz(vorher)} und {_kurz(nachher)} überschneiden sich.")
            )
        elif _orte_verschieden(vorher, nachher) and luecke < regeln.wegzeit:
            befunde.append(
                Befund(
                    "wegzeit",
                    f"Zwischen {_kurz(vorher)} in {vorher.ort} und {_kurz(nachher)} in "
                    f"{nachher.ort} bleiben nur {_minuten(luecke)} Min. – zu wenig Wegzeit "
                    f"(mindestens {_minuten(regeln.wegzeit)} Min.).",
                )
            )
        elif luecke < regeln.puffer:
            befunde.append(
                Befund(
                    "kein_puffer",
                    f"{_kurz(vorher)} und {_kurz(nachher)} liegen direkt hintereinander "
                    f"({_minuten(luecke)} Min. statt mindestens "
                    f"{_minuten(regeln.puffer)} Min. Puffer).",
                )
            )
            if luecke > timedelta(0) and frei(vorher.ende, nachher.start):
                bloecke.append(Block("puffer", vorher.ende, nachher.start))
        if luecke < regeln.puffer:
            kette.append(nachher)
        else:
            ketten.append(kette)
            kette = [nachher]
    ketten.append(kette)

    # Puffer vor und nach jeder Kette von Terminen, die direkt aufeinander folgen
    for kette in ketten:
        if len(kette) < 2:
            continue
        erster, letzter = kette[0], max(kette, key=lambda b: b.ende)
        vor_start = max(erster.start - regeln.puffer, beginn)
        vorher_ende = max((b.ende for b in am_tag if b.ende <= erster.start), default=vor_start)
        vor_start = max(vor_start, vorher_ende)
        if frei(vor_start, erster.start):
            bloecke.append(Block("puffer", vor_start, erster.start))
        nach_ende = letzter.ende + regeln.puffer
        naechster = min((b.start for b in am_tag if b.start >= letzter.ende), default=nach_ende)
        nach_ende = min(nach_ende, naechster, ende)
        if frei(letzter.ende, nach_ende):
            bloecke.append(Block("puffer", letzter.ende, nach_ende))

    # ------------------------------------------------ Mittagspause
    fenster_von = wandzeit(tag, regeln.mittag_von, zone)
    fenster_bis = wandzeit(tag, regeln.mittag_bis, zone)
    hat_pause = any(
        min(p.ende, fenster_bis) - max(p.start, fenster_von) >= regeln.mittag_dauer
        for p in pausen
        if PAUSEN_WOERTER.search(p.titel) and not p.titel.startswith("⏸️")
    )
    betrifft_mittag = len(termine) >= 2 or any(
        t.start < fenster_bis and t.ende > fenster_von for t in termine
    )
    if not hat_pause and betrifft_mittag:
        # Erst eine Lücke suchen, die auch die geplanten Puffer freilässt; sonst weichen sie.
        mit_puffern = [*am_tag, *(_als_belegung(b) for b in bloecke)]
        mittag = wandzeit(tag, time(12, 0), zone)
        start = None
        for belegt in (mit_puffern, am_tag, termine):
            if fenster_von <= mittag <= fenster_bis - regeln.mittag_dauer and not any(
                b.start < mittag + regeln.mittag_dauer and b.ende > mittag for b in belegt
            ):
                start = mittag
            else:
                start = freie_luecke(
                    belegt, max(fenster_von, mittag), fenster_bis, regeln.mittag_dauer
                )
                start = start or freie_luecke(belegt, fenster_von, fenster_bis, regeln.mittag_dauer)
            if start is not None:
                break
        if start is not None:
            befunde.append(
                Befund(
                    "keine_mittagspause",
                    "Es ist noch keine Mittagspause eingeplant (zwischen "
                    f"{regeln.mittag_von:%H:%M} "
                    f"und {regeln.mittag_bis:%H:%M}).",
                )
            )
            bloecke = [
                b for b in bloecke if not (b.start < start + regeln.mittag_dauer and b.ende > start)
            ]
            bloecke.append(Block("mittag", start, start + regeln.mittag_dauer))
        else:
            im_fenster = [t for t in termine if t.start < fenster_bis and t.ende > fenster_von]
            befunde.append(
                Befund(
                    "kein_platz_mittag",
                    f"Zwischen {regeln.mittag_von:%H:%M} und {regeln.mittag_bis:%H:%M} ist kein "
                    f"Platz für {_minuten(regeln.mittag_dauer)} Min. Mittagspause – "
                    + ", ".join(_kurz(t) for t in im_fenster)
                    + " füllen alles. Lässt sich einer davon verschieben oder kürzen?",
                )
            )

    # ------------------------------------------------ zu voll
    belegt = timedelta(0)
    zeiger = beginn
    for termin in termine:
        anfang = max(termin.start, zeiger, beginn)
        schluss = min(termin.ende, ende)
        if schluss > anfang:
            belegt += schluss - anfang
            zeiger = schluss
    if belegt > timedelta(hours=regeln.max_stunden):
        stunden = belegt.total_seconds() / 3600
        befunde.append(
            Befund(
                "zu_voll",
                f"Der Tag ist sehr voll: {stunden:.1f} Stunden Termine (Grenze "
                f"{regeln.max_stunden:g} Stunden). Lässt sich etwas verschieben?",
            )
        )
    bloecke.sort(key=lambda b: b.start)
    return befunde, _ohne_doppelte(bloecke)


def _als_belegung(block: Block) -> Belegung:
    return Belegung(block.start, block.ende, block.titel, "", "", "", False, False, block.art)


def _ohne_doppelte(bloecke: list[Block]) -> list[Block]:
    ergebnis: list[Block] = []
    for block in bloecke:
        if any(b.start < block.ende and b.ende > block.start for b in ergebnis):
            continue
        ergebnis.append(block)
    return ergebnis


class PausenWaechter:
    """Prüft Tage, trägt Blöcke ein und meldet sich bei Louis."""

    def __init__(
        self,
        kalender: Kalender,
        regeln: PausenRegeln,
        zone: ZoneInfo,
        zeitplaner: Zeitplaner | None = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.kalender = kalender
        self.regeln = regeln
        self.zone = zone
        self.zeitplaner = zeitplaner
        self._uhr = uhr or (lambda: datetime.now(zone))
        self.formulierer: Formulierer | None = None
        #: Zentrale Rechte-Schicht (Notfall-Stopp, Aktionsprotokoll) – setzt die App.
        self.rechte: Rechte | None = None

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def _belegungen(self, tag: date) -> list[Belegung]:
        beginn = wandzeit(tag, time(0), self.zone)
        return self.kalender.belegungen(beginn, beginn + timedelta(days=1))

    def pruefen(self, tag: date) -> tuple[list[Befund], list[Block]]:
        return tag_pruefen(tag, self._belegungen(tag), self.regeln, self.zone)

    def eintragen(self, bloecke: list[Block]) -> list[Block]:
        """Trägt Blöcke im Pausen-Kalender ein (legt ihn bei Bedarf an)."""
        if not bloecke:
            return []
        namen = [n.casefold() for n, _ in self.kalender.verbindung.alle_sammlungen()]
        if self.regeln.kalender.casefold() not in namen:
            self.kalender.kalender_anlegen(self.regeln.kalender, self.regeln.farbe)
        eingetragen = []
        for block in bloecke:
            try:
                self.kalender.block_eintragen(
                    block.titel,
                    block.start,
                    block.ende,
                    self.regeln.kalender,
                    block.art,
                    self.regeln.farbe,
                )
            except WerkzeugFehler as fehler:
                log.warning("Pausenblock %s nicht eingetragen: %s", block.zeile(), fehler)
                continue
            eingetragen.append(block)
        return eingetragen

    def vorschlag(self, tag: date, uid: str) -> datetime | None:
        """Ein Beginn am selben Tag, an dem der Termin ``uid`` mit Puffern Platz hätte."""
        belegt = self._belegungen(tag)
        termin = next((b for b in belegt if b.uid == uid and not b.ganztaegig), None)
        if termin is None:
            return None
        dauer = termin.ende - termin.start
        andere = [b for b in belegt if b.uid != uid]
        frueh = max(wandzeit(tag, TAG_VON, self.zone), self.jetzt())
        bis = wandzeit(tag, TAG_BIS, self.zone)
        # Erst ab dem gewünschten Beginn suchen (möglichst nah), dann ab dem Morgen.
        for von in (max(frueh, termin.start - self.regeln.puffer), frueh):
            if (kandidat := self._suchen(tag, andere, termin, dauer, von, bis)) is not None:
                return kandidat
        return None

    def _suchen(
        self,
        tag: date,
        andere: list[Belegung],
        termin: Belegung,
        dauer: timedelta,
        von: datetime,
        bis: datetime,
    ) -> datetime | None:
        uid = termin.uid
        zeiger = von
        while zeiger < bis:
            luecke = freie_luecke(andere, zeiger, bis, dauer + 2 * self.regeln.puffer)
            if luecke is None:
                return None
            kandidat = luecke + self.regeln.puffer
            versuch = [
                *andere,
                Belegung(
                    kandidat,
                    kandidat + dauer,
                    termin.titel,
                    termin.ort,
                    termin.kalender,
                    uid,
                    False,
                    False,
                    "",
                ),
            ]
            befunde, _ = tag_pruefen(tag, versuch, self.regeln, self.zone)
            if not any(
                b.art in ("kein_puffer", "ueberlappung", "wegzeit", "kein_platz_mittag")
                for b in befunde
            ):
                return kandidat
            zeiger = luecke + timedelta(minutes=15)
        return None

    # -------------------------------------------------------------- Haken und Aufträge

    def nach_termin(self, tag: date, uid: str | None) -> str | None:
        """Haken für den Kalender: Hinweis an das Modell nach einer Terminänderung."""
        befunde, _ = self.pruefen(tag)
        if uid:
            befunde = [b for b in befunde if b.art != "keine_mittagspause"]  # trägt der Abend ein
        if not befunde:
            return None
        text = (
            f"PAUSEN-HINWEIS für {datum_lang(tag)} – sag Louis das kurz in deinem Stil:\n"
            + "\n".join(f"- {b.text}" for b in befunde)
        )
        if uid and (besser := self.vorschlag(tag, uid)) is not None:
            text += (
                f"\nAlternative: Der Termin hätte um {besser:%H:%M} Uhr mit genug Puffer Platz – "
                "biete Louis an, ihn dorthin zu verschieben (termin_aendern)."
            )
        return text

    def tag_pruefen_werkzeug(self, datum: str | None = None, eintragen: bool = False) -> str:
        if datum:
            wert = zeitpunkt_lesen(datum, self.zone, "datum")
            tag = wert.date() if isinstance(wert, datetime) else wert
        else:
            tag = self.jetzt().date() + timedelta(days=1)
        befunde, bloecke = self.pruefen(tag)
        zeilen = [f"Pausen-Prüfung für {datum_lang(tag)}:"]
        zeilen += [f"- {b.text}" for b in befunde] or ["- Alles gut: genug Puffer und Pausen."]
        if bloecke:
            if eintragen:
                gemacht = self.eintragen(bloecke)
                zeilen.append(
                    f"Eingetragen im Kalender „{self.regeln.kalender}“: "
                    + ", ".join(b.zeile() for b in gemacht)
                )
            else:
                zeilen.append(
                    "Vorschlag (mit eintragen=true trage ich das ein): "
                    + ", ".join(b.zeile() for b in bloecke)
                )
        return "\n".join(zeilen)

    async def abends_pruefen(self) -> str | None:
        """Täglicher Auftrag: morgen prüfen, Blöcke eintragen, Louis Bescheid geben."""
        morgen = self.jetzt().date() + timedelta(days=1)
        befunde, bloecke = await asyncio.to_thread(self.pruefen, morgen)
        if bloecke and self.rechte is not None and self.rechte.notfall_aktiv():
            # Notfall-Stopp: nichts eintragen, nur Bescheid geben.
            self.rechte.protokollieren(
                werkzeug="pausen_eintragen",
                stufe="schreiben",
                parameter={"tag": morgen.isoformat(), "bloecke": [b.zeile() for b in bloecke]},
                bestaetigt=False,
                status="gesperrt_notfall",
                ergebnis="Notfall-Stopp aktiv",
                kanal="hintergrund",
                account="missa",
            )
            bloecke = []
        eingetragen = await asyncio.to_thread(self.eintragen, bloecke)
        if eingetragen and self.rechte is not None:
            self.rechte.protokollieren(
                werkzeug="pausen_eintragen",
                stufe="schreiben",
                parameter={"tag": morgen.isoformat()},
                bestaetigt=False,
                status="ausgefuehrt",
                ergebnis=", ".join(b.zeile() for b in eingetragen),
                kanal="hintergrund",
                account="missa",
            )
        if not befunde and not eingetragen:
            return None
        teile = [f"☕ Kurzer Blick auf morgen ({datum_kurz(morgen)}):"]
        teile += [f"• {b.text}" for b in befunde]
        if eingetragen:
            teile.append(
                f"Ich habe dir im Kalender „{self.regeln.kalender}“ eingetragen: "
                + ", ".join(b.zeile() for b in eingetragen)
                + ". Passt es nicht, sag einfach „mach das rückgängig“."
            )
        ersatz = "\n".join(teile)
        text = ersatz
        if self.formulierer is not None:
            try:
                text = await self.formulierer(
                    "Sag Louis kurz und freundlich, was dir an seinem Kalender für morgen "
                    "aufgefallen ist und was du eingetragen hast. Behalte alle Uhrzeiten genau "
                    f"bei:\n{ersatz}",
                    ersatz,
                )
            except Exception:
                log.exception("Pausen: Formulieren fehlgeschlagen")
        if self.zeitplaner is not None:
            self.zeitplaner.anlegen(
                f"Pausen für {datum_kurz(morgen)}",
                self.jetzt(),
                nachricht=text,
                schluessel=f"pausen:{morgen.isoformat()}",
                herkunft="pausen",
            )
        return text


def pausen_werkzeuge(waechter: PausenWaechter) -> list[Werkzeug]:
    return [
        Werkzeug(
            "tag_pruefen",
            "Prüft einen Tag auf fehlende Puffer, Mittagspause, Wegzeit und zu volle Tage. "
            "Mit eintragen=true trägt Missa Puffer- und Pausenblöcke selbst ein.",
            {
                "type": "object",
                "properties": {
                    "datum": {"type": "string", "description": "Tag, Standard: morgen"},
                    "eintragen": {"type": "boolean", "description": "Blöcke eintragen"},
                },
            },
            waechter.tag_pruefen_werkzeug,
            stufe=Stufe.SCHREIBEN,
        )
    ]
