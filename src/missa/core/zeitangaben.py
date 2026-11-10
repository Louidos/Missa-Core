"""Zeitangaben aus natürlicher Sprache verstehen (Deutsch, Zeitzone Europe/Zurich).

Beispiele, die verstanden werden:

- Uhrzeit: „um 6:30“, „6.30 Uhr“, „um 18 Uhr“, „um halb 7“, „viertel nach 6“
- relativ: „in 20 Minuten“, „in einer halben Stunde“, „in 2 Stunden und 15 Minuten“
- Tag: „heute“, „morgen“, „übermorgen“, „am Freitag“, „nächsten Montag“,
  „am 24.12.“, „am 3. Dezember“, „in 3 Tagen um 9“
- Tageszeit: „morgen früh“, „heute Abend um 8“, „mittags“
- wiederkehrend: „jeden Montag um 7“, „täglich um 6:30“, „werktags um 7“,
  „jeden Montag und Donnerstag um 18 Uhr“, „jeden Monat am 15. um 9“
- ISO-Format: „2026-11-13T14:00“

Sommer- und Winterzeit werden über die Zeitzone korrekt behandelt: Wiederkehrende
Erinnerungen behalten ihre Uhrzeit an der Wand (7:00 bleibt 7:00), auch über die
Umstellung hinweg. Eine Uhrzeit, die es wegen der Umstellung nicht gibt (z. B.
2:30 am letzten Sonntag im März), wird auf die gültige Zeit danach geschoben.

Ist eine Angabe mehrdeutig (z. B. „um 6“, wenn es schon 7 Uhr ist), wird
:class:`Rueckfrage` geworfen – mit einer fertigen Frage für Louis.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from missa.core.werkzeuge import WerkzeugFehler

WOCHENTAGE = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
WOCHENTAGE_ANZEIGE = [w.capitalize() for w in WOCHENTAGE]
MONATE = {
    "januar": 1,
    "jan": 1,
    "jänner": 1,
    "februar": 2,
    "feb": 2,
    "märz": 3,
    "maerz": 3,
    "mär": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "juni": 6,
    "jun": 6,
    "juli": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "dez": 12,
}
ZAHLWOERTER = {
    "ein": 1,
    "eine": 1,
    "einer": 1,
    "einem": 1,
    "einen": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "fuenf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "zwoelf": 12,
    "fünfzehn": 15,
    "zwanzig": 20,
    "dreißig": 30,
    "dreissig": 30,
    "vierzig": 40,
    "fünfzig": 50,
    "sechzig": 60,
    "neunzig": 90,
    "anderthalb": 1.5,
    "eineinhalb": 1.5,
    "halbe": 0.5,
    "halben": 0.5,
}
EINHEITEN = {
    "s": 1,
    "sek": 1,
    "sekunde": 1,
    "sekunden": 1,
    "min": 60,
    "minute": 60,
    "minuten": 60,
    "h": 3600,
    "std": 3600,
    "stunde": 3600,
    "stunden": 3600,
    "tag": 86400,
    "tage": 86400,
    "tagen": 86400,
    "woche": 7 * 86400,
    "wochen": 7 * 86400,
}
#: Standard-Uhrzeiten für Tageszeiten ohne genaue Uhrzeit.
TAGESZEITEN = {
    "früh": time(7, 0),
    "frueh": time(7, 0),
    "morgens": time(7, 0),
    "vormittags": time(10, 0),
    "mittags": time(12, 0),
    "nachmittags": time(15, 0),
    "abends": time(19, 0),
    "nachts": time(22, 0),
}
NACHMITTAG_WOERTER = ("abend", "abends", "nachmittag", "nachmittags", "nachts", "nacht")
VORMITTAG_WOERTER = ("früh", "frueh", "morgens", "vormittag", "vormittags")


class Unverstanden(WerkzeugFehler):
    """Die Zeitangabe konnte nicht verstanden werden."""


class Rueckfrage(WerkzeugFehler):
    """Die Zeitangabe ist mehrdeutig; ``frage`` ist eine fertige Rückfrage an Louis."""

    def __init__(self, frage: str, optionen: list[datetime] | None = None) -> None:
        super().__init__(frage)
        self.frage = frage
        self.optionen = optionen or []


# ---------------------------------------------------------------------- Zeitzone


def wandzeit(tag: date, uhrzeit: time, zone: ZoneInfo) -> datetime:
    """Datum + Uhrzeit an der Wand in ``zone``.

    Gibt es die Uhrzeit wegen der Sommerzeit-Umstellung nicht, wird sie auf die
    gültige Zeit danach geschoben (2:30 → 3:30). Doppelte Uhrzeiten (Umstellung
    im Herbst) gelten in der ersten Stunde (Sommerzeit).
    """
    roh = datetime.combine(tag, uhrzeit.replace(tzinfo=None), tzinfo=zone)
    return roh.astimezone(UTC).astimezone(zone)


# ---------------------------------------------------------------------- Wiederholung


@dataclass(frozen=True)
class Wiederholung:
    """Regel für wiederkehrende Erinnerungen.

    ``art`` ist ``taeglich``, ``werktags``, ``woechentlich`` (mit ``wochentage``,
    0 = Montag) oder ``monatlich`` (mit ``monatstag``).
    """

    art: str
    wochentage: tuple[int, ...] = ()
    monatstag: int | None = None

    def speichern(self) -> str:
        if self.art == "woechentlich":
            return "woechentlich:" + ",".join(str(t) for t in self.wochentage)
        if self.art == "monatlich":
            return f"monatlich:{self.monatstag}"
        return self.art

    @classmethod
    def lesen(cls, text: str | None) -> Wiederholung | None:
        if not text:
            return None
        art, _, rest = text.partition(":")
        if art == "woechentlich":
            return cls(art, tuple(int(t) for t in rest.split(",") if t != ""))
        if art == "monatlich":
            return cls(art, monatstag=int(rest))
        if art in ("taeglich", "werktags"):
            return cls(art)
        raise ValueError(f"Unbekannte Wiederholung: {text}")

    def text(self) -> str:
        """Lesbar, z. B. ``jeden Montag und Donnerstag``."""
        if self.art == "taeglich":
            return "jeden Tag"
        if self.art == "werktags":
            return "jeden Werktag (Mo–Fr)"
        if self.art == "monatlich":
            return f"jeden Monat am {self.monatstag}."
        namen = [WOCHENTAGE_ANZEIGE[t] for t in self.wochentage]
        if set(self.wochentage) == {5, 6}:
            return "jedes Wochenende (Sa und So)"
        if len(namen) == 1:
            return f"jeden {namen[0]}"
        return "jeden " + ", ".join(namen[:-1]) + " und " + namen[-1]

    def passt(self, tag: date) -> bool:
        if self.art == "taeglich":
            return True
        if self.art == "werktags":
            return tag.weekday() < 5
        if self.art == "woechentlich":
            return tag.weekday() in self.wochentage
        if self.art == "monatlich":
            letzter = calendar.monthrange(tag.year, tag.month)[1]
            return tag.day == min(self.monatstag or 1, letzter)
        return False

    def naechster(self, nach: datetime, uhrzeit: time, zone: ZoneInfo) -> datetime:
        """Der erste Zeitpunkt dieser Regel, der echt nach ``nach`` liegt."""
        tag = nach.astimezone(zone).date()
        for _ in range(400):
            if self.passt(tag):
                kandidat = wandzeit(tag, uhrzeit, zone)
                if kandidat > nach:
                    return kandidat
            tag += timedelta(days=1)
        raise Unverstanden("Für diese Wiederholung finde ich keinen nächsten Termin.")


@dataclass(frozen=True)
class Zeitangabe:
    """Ergebnis von :func:`zeit_verstehen`."""

    zeitpunkt: datetime
    wiederholung: Wiederholung | None = None

    @property
    def uhrzeit(self) -> time:
        return self.zeitpunkt.time().replace(second=0, microsecond=0)


# ---------------------------------------------------------------------- Hilfen


def _zahl(text: str) -> float | None:
    text = text.strip().replace(",", ".")
    if text in ZAHLWOERTER:
        return float(ZAHLWOERTER[text])
    try:
        return float(text)
    except ValueError:
        return None


def _stunde(stunde: int, minute: int = 0) -> time:
    if not (0 <= stunde <= 24 and 0 <= minute < 60) or (stunde == 24 and minute):
        raise Unverstanden(f"{stunde}:{minute:02d} ist keine gültige Uhrzeit.")
    return time(0 if stunde == 24 else stunde, minute)


def _uhrzeit_finden(text: str) -> tuple[time | None, bool, str]:
    """Sucht eine Uhrzeit. Gibt (Uhrzeit, mit_minuten_oder_eindeutig, Rest) zurück.

    Der zweite Wert sagt, ob die Stunde eindeutig ist (über 12, Tageszeit-Wort
    oder ``00``-Schreibweise wie ``06:30``).
    """
    muster = [
        # halb 7 = 6:30, viertel nach 6, viertel vor 7, dreiviertel 7
        (r"\b(?:um\s+)?halb\s+(\d{1,2})\b(?:\s*uhr)?", "halb"),
        (r"\b(?:um\s+)?viertel\s+nach\s+(\d{1,2})\b(?:\s*uhr)?", "nach"),
        (r"\b(?:um\s+)?viertel\s+vor\s+(\d{1,2})\b(?:\s*uhr)?", "vor"),
        (r"\b(?:um\s+)?dreiviertel\s+(\d{1,2})\b(?:\s*uhr)?", "vor"),
        (r"\b(?:um\s+)?(\d{1,2})[:h](\d{2})\b(?:\s*uhr)?", "hm"),
        (r"\b(?:um\s+)?(\d{1,2})\.(\d{2})\s*uhr\b", "hm"),
        (r"\bum\s+(\d{1,2})\.(\d{2})\b(?!\.)", "hm"),
        (r"\b(?:um\s+)?(\d{1,2})\s*uhr\b(?:\s*(\d{1,2})\b)?", "uhr"),
        (r"\bum\s+(\d{1,2})\b(?![.:]\d)", "h"),
    ]
    for regex, art in muster:
        treffer = re.search(regex, text)
        if not treffer:
            continue
        stunde = int(treffer.group(1))
        rest = (text[: treffer.start()] + " " + text[treffer.end() :]).strip()
        roh = treffer.group(0)
        eindeutig = stunde == 0 or stunde > 12 or bool(re.search(r"\b0\d[:.h]", roh))
        if art == "halb":
            return _stunde((stunde - 1) % 24, 30), eindeutig, rest
        if art == "nach":
            return _stunde(stunde, 15), eindeutig, rest
        if art == "vor":
            return _stunde((stunde - 1) % 24, 45), eindeutig, rest
        if art == "hm":
            return _stunde(stunde, int(treffer.group(2))), eindeutig, rest
        if art == "uhr":
            minute = int(treffer.group(2)) if treffer.group(2) else 0
            return _stunde(stunde, minute), eindeutig, rest
        return _stunde(stunde), eindeutig, rest
    return None, False, text


def _relativ_finden(text: str) -> tuple[timedelta | None, str]:
    """„in 20 Minuten“, „in einer halben Stunde“, „in 2 Stunden und 15 Minuten“."""
    zahl = r"(\d+(?:[.,]\d+)?|" + "|".join(sorted(ZAHLWOERTER, key=len, reverse=True)) + r")"
    einheit = r"(" + "|".join(sorted(EINHEITEN, key=len, reverse=True)) + r")\b"
    sonder = {
        r"\bin\s+einer\s+viertelstunde\b": timedelta(minutes=15),
        r"\bin\s+einer\s+dreiviertelstunde\b": timedelta(minutes=45),
        r"\bin\s+einer\s+halben\s+stunde\b": timedelta(minutes=30),
        r"\bin\s+einer\s+halbe\s+stunde\b": timedelta(minutes=30),
    }
    for regex, abstand in sonder.items():
        if treffer := re.search(regex, text):
            return abstand, (text[: treffer.start()] + " " + text[treffer.end() :]).strip()

    kette = re.search(
        rf"\bin\s+{zahl}\s*(?:halbe\s+)?{einheit}(?:\s*(?:und|,)?\s*{zahl}\s*{einheit})*", text
    )
    if not kette:
        return None, text
    summe = 0.0
    for teil in re.finditer(rf"{zahl}\s*(halbe\s+)?{einheit}", kette.group(0)):
        menge = _zahl(teil.group(1))
        if menge is None:
            continue
        if teil.group(2):  # „in einer halben Stunde“
            menge *= 0.5
        summe += menge * EINHEITEN[teil.group(3)]
    if summe <= 0:
        return None, text
    rest = (text[: kette.start()] + " " + text[kette.end() :]).strip()
    return timedelta(seconds=summe), rest


def _datum_finden(text: str, heute: date) -> tuple[date | None, bool, str]:
    """Sucht einen Tag. Gibt (Tag, ausdrücklich_heute, Rest) zurück."""

    def ohne(treffer: re.Match[str]) -> str:
        return (text[: treffer.start()] + " " + text[treffer.end() :]).strip()

    if treffer := re.search(r"\bübermorgen\b|\buebermorgen\b", text):
        return heute + timedelta(days=2), False, ohne(treffer)
    # „morgen“, aber nicht „morgens“ und nicht „heute morgen“ (= heute früh)
    if treffer := re.search(r"\bheute\b", text):
        return heute, True, ohne(treffer)
    if treffer := re.search(r"\bmorgen\b", text):
        return heute + timedelta(days=1), False, ohne(treffer)

    # 24.12.2026, 24.12.26, 24.12.
    if treffer := re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?(?!\d)", text):
        tag, monat = int(treffer.group(1)), int(treffer.group(2))
        jahr = int(treffer.group(3)) if treffer.group(3) else heute.year
        if jahr < 100:
            jahr += 2000
        try:
            gefunden = date(jahr, monat, tag)
        except ValueError as fehler:
            raise Unverstanden(f"Den {treffer.group(0)} gibt es nicht.") from fehler
        if not treffer.group(3) and gefunden < heute:
            gefunden = gefunden.replace(year=heute.year + 1)
        return gefunden, False, ohne(treffer)

    # 3. Dezember (2026)
    monate = "|".join(sorted(MONATE, key=len, reverse=True))
    if treffer := re.search(rf"\b(\d{{1,2}})\.?\s*({monate})\b\.?(?:\s*(\d{{4}}))?", text):
        tag, monat = int(treffer.group(1)), MONATE[treffer.group(2)]
        jahr = int(treffer.group(3)) if treffer.group(3) else heute.year
        try:
            gefunden = date(jahr, monat, tag)
        except ValueError as fehler:
            raise Unverstanden(f"Den {treffer.group(0)} gibt es nicht.") from fehler
        if not treffer.group(3) and gefunden < heute:
            gefunden = gefunden.replace(year=heute.year + 1)
        return gefunden, False, ohne(treffer)

    # (am / nächsten / kommenden / diesen) Freitag
    tage = "|".join(WOCHENTAGE)
    if treffer := re.search(
        rf"\b(?:am\s+)?(nächsten|naechsten|kommenden|diesen|nächster|nächste)?\s*({tage})\b",
        text,
    ):
        ziel = WOCHENTAGE.index(treffer.group(2))
        abstand = (ziel - heute.weekday()) % 7
        if abstand == 0 and treffer.group(1) and treffer.group(1) != "diesen":
            abstand = 7
        return heute + timedelta(days=abstand), abstand == 0, ohne(treffer)
    return None, False, text


def _wiederholung_finden(text: str, heute: date) -> tuple[Wiederholung | None, str]:
    def ohne(treffer: re.Match[str]) -> str:
        return (text[: treffer.start()] + " " + text[treffer.end() :]).strip()

    if treffer := re.search(r"\b(täglich|taeglich|jeden\s+tag|alle\s+tage)\b", text):
        return Wiederholung("taeglich"), ohne(treffer)
    if treffer := re.search(r"\b(jeden\s+(morgen|abend|mittag))\b", text):
        # „jeden Morgen“ = täglich, die Tageszeit bleibt als Hinweis im Text.
        tageszeit = {"morgen": "morgens", "abend": "abends", "mittag": "mittags"}[treffer.group(2)]
        return Wiederholung("taeglich"), ohne(treffer) + " " + tageszeit
    if treffer := re.search(
        r"\b(werktags|jeden\s+werktag|(?:an\s+)?werktagen|montags?\s+bis\s+freitags?)\b", text
    ):
        return Wiederholung("werktags"), ohne(treffer)
    if treffer := re.search(r"\b(jedes\s+wochenende|wochenends)\b", text):
        return Wiederholung("woechentlich", (5, 6)), ohne(treffer)
    if treffer := re.search(
        r"\b(?:jeden\s+monat|monatlich|jeden\s+monats?)\b(?:\s+am)?\s*(\d{1,2})\.?|"
        r"\bam\s+(\d{1,2})\.\s*(?:jeden|jedes)\s+monats?\b",
        text,
    ):
        tag = int(treffer.group(1) or treffer.group(2))
        if not 1 <= tag <= 31:
            raise Unverstanden(f"Einen {tag}. im Monat gibt es nicht.")
        return Wiederholung("monatlich", monatstag=tag), ohne(treffer)

    tage = "|".join(WOCHENTAGE)
    liste = rf"(?:{tage})s?(?:\s*(?:,|und|&)\s*(?:jeden\s+)?(?:{tage})s?)*"
    if treffer := re.search(rf"\b(?:jeden|jede|alle|immer)\s+({liste})\b", text) or re.search(
        rf"\b((?:{tage})s(?:\s*(?:,|und)\s*(?:{tage})s)*)\b", text
    ):
        gefunden = sorted({WOCHENTAGE.index(n) for n in re.findall(rf"({tage})", treffer.group(1))})
        return Wiederholung("woechentlich", tuple(gefunden)), ohne(treffer)
    if treffer := re.search(r"\b(wöchentlich|woechentlich|jede\s+woche)\b", text):
        return Wiederholung("woechentlich", (heute.weekday(),)), ohne(treffer)
    return None, text


def _uhrzeit_text(wert: datetime) -> str:
    return f"{wert:%H:%M} Uhr"


def _tag_text(wert: datetime, heute: date) -> str:
    if wert.date() == heute:
        return "heute"
    if wert.date() == heute + timedelta(days=1):
        return "morgen"
    return f"am {WOCHENTAGE_ANZEIGE[wert.weekday()]}, {wert:%d.%m.}"


# ---------------------------------------------------------------------- Hauptfunktion


def zeit_verstehen(text: str, jetzt: datetime) -> Zeitangabe:
    """Versteht eine Zeitangabe relativ zu ``jetzt`` (aware, in der gewünschten Zeitzone).

    Wirft :class:`Rueckfrage` bei mehrdeutigen und :class:`Unverstanden` bei
    unverständlichen Angaben oder Zeitpunkten in der Vergangenheit.
    """
    zone = jetzt.tzinfo
    if not isinstance(zone, ZoneInfo):
        raise ValueError("jetzt braucht eine ZoneInfo-Zeitzone")
    original = " ".join(str(text or "").split())
    if not original:
        raise Unverstanden("Es fehlt die Zeitangabe (z. B. „um 6:30“ oder „in 20 Minuten“).")

    # ISO-Format, wie es das Modell gern schickt
    try:
        iso = datetime.fromisoformat(original.replace(" ", "T", 1))
    except ValueError:
        iso = None
    if iso is not None and re.match(r"^\d{4}-\d{2}-\d{2}", original):
        if len(original) == 10:
            raise Rueckfrage(
                f"Um wie viel Uhr soll ich dich am {iso:%d.%m.%Y} erinnern?",
            )
        if iso.tzinfo is None:
            zeitpunkt = wandzeit(iso.date(), iso.time(), zone)
        else:
            zeitpunkt = iso.astimezone(zone)
        if zeitpunkt <= jetzt:
            raise Unverstanden(
                f"{zeitpunkt:%d.%m.%Y %H:%M} liegt in der Vergangenheit – wann genau meinst du?"
            )
        return Zeitangabe(zeitpunkt)

    roh = original.lower()
    roh = re.sub(r"\b(erinnere?|mich|bitte|nochmal|noch\s+mal|wieder)\b", " ", roh)
    heute = jetzt.date()

    wiederholung, roh = _wiederholung_finden(roh, heute)

    abstand, roh = _relativ_finden(roh)
    if abstand is not None and abstand < timedelta(days=1):
        return Zeitangabe(jetzt + abstand)

    tag, heute_genannt, roh = _datum_finden(roh, heute)
    if abstand is not None:
        tag = heute + timedelta(days=abstand.days)
    if tag == heute and re.search(r"\bmorgen\b", roh):
        roh += " früh"  # „heute morgen“ = heute früh

    uhrzeit, eindeutig, roh = _uhrzeit_finden(roh)

    nachmittag = any(re.search(rf"\b{w}\b", roh) for w in NACHMITTAG_WOERTER)
    vormittag = any(re.search(rf"\b{w}\b", roh) for w in VORMITTAG_WOERTER)
    # „heute morgen“ / „morgen früh“: Wort „morgen“ wurde schon als Tag gelesen.
    if uhrzeit is None:
        for wort, standard in TAGESZEITEN.items():
            abend = wort == "abends" and re.search(r"\babend\b", roh)
            if re.search(rf"\b{wort}\b", roh) or abend:
                uhrzeit, eindeutig = standard, True
                break
        if uhrzeit is None and re.search(r"\bmittag\b", roh):
            uhrzeit, eindeutig = time(12, 0), True
        if uhrzeit is None and re.search(r"\bnachmittag\b", roh):
            uhrzeit, eindeutig = time(15, 0), True
    elif not eindeutig:
        if nachmittag and uhrzeit.hour < 12:
            uhrzeit, eindeutig = uhrzeit.replace(hour=uhrzeit.hour + 12), True
        elif vormittag or (uhrzeit.hour == 12):
            eindeutig = True

    if uhrzeit is None:
        if tag is None and wiederholung is None:
            raise Unverstanden(
                f"Die Zeitangabe „{original}“ verstehe ich nicht. Beispiele: „um 6:30“, "
                "„in 20 Minuten“, „morgen um 8“, „jeden Montag um 7“."
            )
        wann = _tag_text(wandzeit(tag, time(12), zone), heute) if tag else wiederholung.text()  # type: ignore[union-attr]
        raise Rueckfrage(f"Um wie viel Uhr soll ich dich {wann} erinnern?")

    # ------------------------------------------------------------ wiederkehrend
    if wiederholung is not None:
        if not eindeutig and 1 <= uhrzeit.hour <= 5:
            frueh = uhrzeit
            spaet = uhrzeit.replace(hour=uhrzeit.hour + 12)
            raise Rueckfrage(
                f"Meinst du {wiederholung.text()} um {frueh:%H:%M} Uhr (nachts) oder um "
                f"{spaet:%H:%M} Uhr?"
            )
        if wiederholung.art == "woechentlich" and not wiederholung.wochentage:
            wiederholung = Wiederholung("woechentlich", (heute.weekday(),))
        ab = jetzt
        if tag is not None and tag > heute:
            ab = wandzeit(tag, time(0), zone) - timedelta(seconds=1)
        return Zeitangabe(wiederholung.naechster(ab, uhrzeit, zone), wiederholung)

    # ------------------------------------------------------------ einmalig
    if tag is not None:
        kandidat = wandzeit(tag, uhrzeit, zone)
        if not eindeutig and 1 <= uhrzeit.hour <= 5 and tag != heute:
            spaet = wandzeit(tag, uhrzeit.replace(hour=uhrzeit.hour + 12), zone)
            raise Rueckfrage(
                f"Meinst du {_tag_text(kandidat, heute)} um {_uhrzeit_text(kandidat)} (nachts) "
                f"oder um {_uhrzeit_text(spaet)}?",
                [kandidat, spaet],
            )
        if kandidat <= jetzt:
            heute_ausdruecklich = bool(re.search(r"\bheute\b", original.lower()))
            # „am Freitag“ an einem Freitag: sonst wäre nächste Woche gemeint
            naechste_woche = wandzeit(tag + timedelta(days=7), uhrzeit, zone)
            spaet = None
            if not eindeutig and uhrzeit.hour < 12:
                spaet = wandzeit(tag, uhrzeit.replace(hour=uhrzeit.hour + 12), zone)
                if spaet <= jetzt:
                    spaet = None
            if spaet is not None and heute_ausdruecklich:
                return Zeitangabe(spaet)
            if spaet is not None and heute_genannt:
                raise Rueckfrage(
                    f"Meinst du heute um {_uhrzeit_text(spaet)} oder nächsten "
                    f"{WOCHENTAGE_ANZEIGE[tag.weekday()]} um {_uhrzeit_text(naechste_woche)}?",
                    [spaet, naechste_woche],
                )
            if heute_genannt and not heute_ausdruecklich:
                return Zeitangabe(naechste_woche)
            raise Unverstanden(
                f"{_tag_text(kandidat, heute).capitalize()} um {_uhrzeit_text(kandidat)} ist "
                "schon vorbei – wann genau meinst du?"
            )
        return Zeitangabe(kandidat)

    kandidat = wandzeit(heute, uhrzeit, zone)
    if kandidat > jetzt:
        return Zeitangabe(kandidat)
    morgen = wandzeit(heute + timedelta(days=1), uhrzeit, zone)
    if not eindeutig and uhrzeit.hour < 12:
        spaet = wandzeit(heute, uhrzeit.replace(hour=uhrzeit.hour + 12), zone)
        if spaet > jetzt:
            raise Rueckfrage(
                f"Es ist schon {jetzt:%H:%M} Uhr. Meinst du heute um {_uhrzeit_text(spaet)} "
                f"oder morgen um {_uhrzeit_text(morgen)}?",
                [spaet, morgen],
            )
    return Zeitangabe(morgen)
