"""Beziehungspflege: Nextcloud-Kontakte (CardDAV), Geburtstage und „lange nicht gemeldet“.

- **Kontakte** kommen per CardDAV aus Nextcloud (nur lesen), inklusive Geburtstag.
- **Wichtige Personen** markiert Louis im Chat („Oma mindestens alle 3 Wochen“). Wann er
  sich zuletzt gemeldet hat, kommt aus Mails (automatisch) oder aus dem Chat („hab gerade
  mit Oma telefoniert“); Anrufe kommen später dazu.
- **Täglicher Auftrag** (``KONTAKTE_PRUEFZEIT``): legt für Geburtstage im Zeitplaner eine
  Erinnerung einige Tage vorher und eine am Tag selbst an und erinnert freundlich, wenn
  der gewünschte Abstand überschritten ist – mit einem freien Zeitpunkt aus dem Kalender.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time as uhrzeit_messung
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from missa.core.datenbank import verbinden
from missa.core.prompt import WOCHENTAGE, datum_lang
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.core.zeitangaben import wandzeit
from missa.tools.mail_konto import adressen as mail_adressen

if TYPE_CHECKING:
    from missa.core.zeitplaner import Zeitplaner
    from missa.tools.kalender import Kalender
    from missa.tools.mail_konto import MailKonto

log = logging.getLogger(__name__)

DAV = "{DAV:}"
CARD = "{urn:ietf:params:xml:ns:carddav}"
PROPFIND = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>"""
REPORT = """<?xml version="1.0" encoding="utf-8"?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop><d:getetag/><card:address-data/></d:prop>
</card:addressbook-query>"""
#: So lange werden gelesene Kontakte zwischengespeichert (Sekunden).
CACHE = 600
#: Jahr, das Apple/Nextcloud für „Geburtstag ohne Jahr“ einträgt.
OHNE_JAHR = 1604

SCHEMA = """
CREATE TABLE IF NOT EXISTS kontakte_pflege (
    schluessel TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    abstand_tage INTEGER,
    emails TEXT NOT NULL DEFAULT '',
    letzter_kontakt TEXT,
    letzte_art TEXT,
    letzte_erinnerung TEXT,
    erstellt TEXT NOT NULL
);
"""

Formulierer = Callable[[str, str], Awaitable[str]]


# ---------------------------------------------------------------------- vCard


@dataclass(frozen=True)
class Geburtstag:
    monat: int
    tag: int
    jahr: int | None = None

    def text(self) -> str:
        return f"{self.tag:02d}.{self.monat:02d}." + (str(self.jahr) if self.jahr else "")


@dataclass(frozen=True)
class Kontakt:
    uid: str
    name: str
    spitznamen: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    telefon: tuple[str, ...] = ()
    geburtstag: Geburtstag | None = None

    @property
    def schluessel(self) -> str:
        return f"uid:{self.uid}" if self.uid else f"name:{self.name.casefold()}"

    def passt(self, suche: str) -> bool:
        gesucht = suche.casefold().strip()
        if not gesucht:
            return False
        namen = [self.name, *self.spitznamen]
        return any(gesucht in n.casefold() for n in namen) or any(
            gesucht == e.casefold() for e in self.emails
        )

    def zeile(self) -> str:
        teile = [f"- {self.name}"]
        if self.spitznamen:
            teile.append(f"({', '.join(self.spitznamen)})")
        if self.emails:
            teile.append(f"| E-Mail: {', '.join(self.emails)}")
        if self.telefon:
            teile.append(f"| Tel.: {', '.join(self.telefon)}")
        if self.geburtstag:
            teile.append(f"| Geburtstag: {self.geburtstag.text()}")
        return " ".join(teile)


def _entfalten(text: str) -> list[str]:
    zeilen: list[str] = []
    for zeile in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if zeile[:1] in (" ", "\t") and zeilen:
            zeilen[-1] += zeile[1:]
        elif zeile:
            zeilen.append(zeile)
    return zeilen


def _wert(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def geburtstag_lesen(wert: str, parameter: dict[str, str] | None = None) -> Geburtstag | None:
    """``1950-02-29``, ``19500229``, ``--0229``, ``--02-29``, auch mit Uhrzeit."""
    roh = wert.strip().split("T")[0]
    if treffer := re.fullmatch(r"--(\d{2})-?(\d{2})", roh):
        jahr = None
        monat, tag = int(treffer.group(1)), int(treffer.group(2))
    elif treffer := re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", roh):
        jahr, monat, tag = (int(treffer.group(i)) for i in (1, 2, 3))
    else:
        return None
    if (parameter or {}).get("X-APPLE-OMIT-YEAR") or jahr == OHNE_JAHR:
        jahr = None
    try:
        date(jahr or 2000, monat, tag)  # 2000 ist ein Schaltjahr – 29.2. gilt
    except ValueError:
        return None
    return Geburtstag(monat, tag, jahr)


def vcards_lesen(text: str) -> list[Kontakt]:
    """Liest einen oder mehrere vCards (3.0/4.0)."""
    kontakte = []
    aktuell: list[tuple[str, dict[str, str], str]] | None = None
    for zeile in _entfalten(text):
        kopf, _, wert = zeile.partition(":")
        name, *roh_parameter = kopf.split(";")
        name = name.rpartition(".")[2].upper()  # „item1.EMAIL“ → EMAIL
        if name == "BEGIN" and wert.strip().upper() == "VCARD":
            aktuell = []
            continue
        if name == "END" and wert.strip().upper() == "VCARD" and aktuell is not None:
            kontakte.append(_kontakt(aktuell))
            aktuell = None
            continue
        if aktuell is None:
            continue
        parameter = {}
        for teil in roh_parameter:
            schluessel, _, inhalt = teil.partition("=")
            parameter[schluessel.upper()] = inhalt.strip('"')
        aktuell.append((name, parameter, wert))
    return [k for k in kontakte if k.name]


def _kontakt(felder: list[tuple[str, dict[str, str], str]]) -> Kontakt:
    werte: dict[str, list[tuple[dict[str, str], str]]] = {}
    for name, parameter, wert in felder:
        werte.setdefault(name, []).append((parameter, wert))

    def alle(feld: str) -> list[str]:
        return [_wert(w) for _, w in werte.get(feld, []) if _wert(w)]

    name = next(iter(alle("FN")), "")
    if not name and (n := next(iter(alle("N")), "")):
        teile = [t for t in n.split(";") if t]
        name = " ".join(reversed(teile[:2]))
    spitznamen = tuple(s.strip() for n in alle("NICKNAME") for s in n.split(",") if s.strip())
    geburtstag = None
    for parameter, wert in werte.get("BDAY", []):
        if geburtstag := geburtstag_lesen(wert, parameter):
            break
    return Kontakt(
        uid=next(iter(alle("UID")), ""),
        name=name,
        spitznamen=spitznamen,
        emails=tuple(e.lower() for e in alle("EMAIL")),
        telefon=tuple(alle("TEL")),
        geburtstag=geburtstag,
    )


# ---------------------------------------------------------------------- Geburtstage


def feiertag_im_jahr(geburtstag: Geburtstag, jahr: int) -> date:
    """Der Tag, an dem im Jahr ``jahr`` gefeiert wird – 29.2. in Nicht-Schaltjahren am 28.2."""
    try:
        return date(jahr, geburtstag.monat, geburtstag.tag)
    except ValueError:
        return date(jahr, 2, 28)


def naechster_geburtstag(geburtstag: Geburtstag, ab: date) -> date:
    """Der nächste Geburtstag ab ``ab`` (einschliesslich)."""
    tag = feiertag_im_jahr(geburtstag, ab.year)
    return tag if tag >= ab else feiertag_im_jahr(geburtstag, ab.year + 1)


def alter_am(geburtstag: Geburtstag, tag: date) -> int | None:
    return tag.year - geburtstag.jahr if geburtstag.jahr else None


def abstand_lesen(text: Any) -> int:
    """„3 Wochen“, „alle 14 Tage“, „monatlich“, „2 Monate“, „jede Woche“ → Tage."""
    if isinstance(text, int | float):
        return int(text)
    roh = str(text).casefold().replace("mindestens", "").replace("alle", "").strip()
    woerter = {
        "ein": 1,
        "eine": 1,
        "einen": 1,
        "zwei": 2,
        "drei": 3,
        "vier": 4,
        "fünf": 5,
        "sechs": 6,
        "acht": 8,
        "zehn": 10,
        "zwölf": 12,
    }
    if re.search(r"\b(wöchentlich|jede woche)\b", roh):
        return 7
    if re.search(r"\b(monatlich|jeden monat)\b", roh):
        return 30
    if re.search(r"\b(täglich|jeden tag)\b", roh):
        return 1
    if treffer := re.search(
        r"(\d+|[a-zäöü]+)\s*(tag|tage|tagen|woche|wochen|monat|monate|monaten)\b", roh
    ):
        zahl = treffer.group(1)
        menge = int(zahl) if zahl.isdigit() else woerter.get(zahl)
        if menge:
            faktor = {"t": 1, "w": 7, "m": 30}[treffer.group(2)[0]]
            return menge * faktor
    if roh.isdigit():
        return int(roh)
    raise WerkzeugFehler(f"„{text}“ verstehe ich nicht als Abstand (z. B. „3 Wochen“).")


def abstand_text(tage: int) -> str:
    if tage % 30 == 0 and tage >= 30:
        monate = tage // 30
        return "jeden Monat" if monate == 1 else f"alle {monate} Monate"
    if tage % 7 == 0:
        wochen = tage // 7
        return "jede Woche" if wochen == 1 else f"alle {wochen} Wochen"
    return "jeden Tag" if tage == 1 else f"alle {tage} Tage"


# ---------------------------------------------------------------------- CardDAV


class Adressbuch:
    """Liest Kontakte aus Nextcloud (CardDAV, nur lesend)."""

    def __init__(
        self,
        url: str,
        benutzer: str,
        app_passwort: str,
        name: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.basis = url.rstrip("/")
        self.heim = f"{self.basis}/remote.php/dav/addressbooks/users/{benutzer}/"
        self.name = name.strip()
        self._client = httpx.Client(auth=(benutzer, app_passwort), timeout=30, transport=transport)
        self._cache: tuple[float, list[Kontakt]] | None = None
        self._sperre = threading.Lock()

    def _anfrage(self, methode: str, url: str, inhalt: str) -> ET.Element:
        try:
            antwort = self._client.request(
                methode,
                url,
                content=inhalt.encode("utf-8"),
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            )
        except httpx.HTTPError as fehler:
            raise WerkzeugFehler(f"Nextcloud-Kontakte nicht erreichbar ({fehler})") from fehler
        if antwort.status_code == 401:
            raise WerkzeugFehler("Nextcloud-Kontakte: Anmeldung abgelehnt (App-Passwort prüfen)")
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"Nextcloud-Kontakte antworten mit {antwort.status_code}")
        try:
            return ET.fromstring(antwort.content)
        except ET.ParseError as fehler:
            raise WerkzeugFehler("Nextcloud-Kontakte lieferten ungültiges XML") from fehler

    def buecher(self) -> list[tuple[str, str]]:
        """(Anzeigename, Adresse) aller Adressbücher."""
        wurzel = self._anfrage("PROPFIND", self.heim, PROPFIND)
        ergebnis = []
        for antwort in wurzel.iter(f"{DAV}response"):
            typ = antwort.find(f".//{DAV}resourcetype")
            if typ is None or typ.find(f"{CARD}addressbook") is None:
                continue
            href = (antwort.findtext(f"{DAV}href") or "").strip()
            name = (antwort.findtext(f".//{DAV}displayname") or "").strip()
            ergebnis.append(
                (name or href.rstrip("/").rpartition("/")[2], httpx.URL(self.basis).join(href))
            )
        return [(n, str(u)) for n, u in ergebnis]

    def kontakte(self, neu_laden: bool = False) -> list[Kontakt]:
        with self._sperre:
            if (
                self._cache
                and not neu_laden
                and uhrzeit_messung.monotonic() - self._cache[0] < CACHE
            ):
                return list(self._cache[1])
            buecher = self.buecher()
            if self.name:
                buecher = [b for b in buecher if b[0].casefold() == self.name.casefold()]
                if not buecher:
                    raise WerkzeugFehler(
                        f"Ein Adressbuch „{self.name}“ gibt es in Nextcloud nicht."
                    )
            kontakte: list[Kontakt] = []
            for _, adresse in buecher:
                wurzel = self._anfrage("REPORT", adresse, REPORT)
                for daten in wurzel.iter(f"{CARD}address-data"):
                    kontakte.extend(vcards_lesen(daten.text or ""))
            self._cache = (uhrzeit_messung.monotonic(), kontakte)
            return list(kontakte)

    def suchen(self, name: str) -> list[Kontakt]:
        return [k for k in self.kontakte() if k.passt(name)]


# ---------------------------------------------------------------------- Kontaktpflege


@dataclass(frozen=True)
class Pflege:
    schluessel: str
    name: str
    abstand_tage: int | None
    emails: tuple[str, ...]
    letzter_kontakt: date | None
    letzte_art: str | None
    letzte_erinnerung: date | None
    erstellt: date

    def zeile(self, heute: date) -> str:
        zeile = f"- {self.name}"
        if self.abstand_tage:
            zeile += f": wichtig, {abstand_text(self.abstand_tage)}"
        if self.letzter_kontakt:
            tage = (heute - self.letzter_kontakt).days
            wann = "heute" if tage == 0 else "gestern" if tage == 1 else f"vor {tage} Tagen"
            zeile += f" | zuletzt gemeldet {wann} ({self.letzter_kontakt:%d.%m.%Y}"
            zeile += f", {self.letzte_art})" if self.letzte_art else ")"
            if self.abstand_tage and tage > self.abstand_tage:
                zeile += " – überfällig"
        else:
            zeile += " | noch kein Kontakt vermerkt"
        return zeile


def _datum(text: str | None) -> date | None:
    return date.fromisoformat(text[:10]) if text else None


class Kontaktpflege:
    """Wichtige Personen, letzter Kontakt, Geburtstags- und Abstands-Erinnerungen."""

    def __init__(
        self,
        datei: Path,
        zone: ZoneInfo,
        zeitplaner: Zeitplaner,
        adressbuch: Adressbuch | None = None,
        kalender: Kalender | None = None,
        vorlauf_tage: int = 3,
        pruefzeit: time = time(8, 0),
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.zone = zone
        self.zeitplaner = zeitplaner
        self.adressbuch = adressbuch
        self.kalender = kalender
        self.vorlauf_tage = max(0, vorlauf_tage)
        self.pruefzeit = pruefzeit
        self._uhr = uhr or (lambda: datetime.now(zone))
        #: Lässt Missa Erinnerungen in ihrem Charakter formulieren (Auftrag, Ersatztext).
        self.formulierer: Formulierer | None = None
        #: Mailkonto, aus dem der letzte Kontakt gelesen wird (optional).
        self.mail_konto: MailKonto | None = None
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    # -------------------------------------------------------------- Speicher

    @staticmethod
    def _pflege(zeile: sqlite3.Row) -> Pflege:
        return Pflege(
            schluessel=zeile["schluessel"],
            name=zeile["name"],
            abstand_tage=zeile["abstand_tage"],
            emails=tuple(e for e in zeile["emails"].split(",") if e),
            letzter_kontakt=_datum(zeile["letzter_kontakt"]),
            letzte_art=zeile["letzte_art"],
            letzte_erinnerung=_datum(zeile["letzte_erinnerung"]),
            erstellt=_datum(zeile["erstellt"]),  # type: ignore[arg-type]
        )

    def alle(self, nur_wichtige: bool = False) -> list[Pflege]:
        with verbinden(self.datei) as db:
            sql = "SELECT * FROM kontakte_pflege"
            if nur_wichtige:
                sql += " WHERE abstand_tage IS NOT NULL"
            zeilen = db.execute(sql + " ORDER BY name COLLATE NOCASE").fetchall()
        return [self._pflege(z) for z in zeilen]

    def _eintrag(self, name: str, anlegen: bool = True) -> Pflege:
        """Findet (oder legt an) den Pflege-Eintrag zu ``name``."""
        name = " ".join(str(name).split())
        if not name:
            raise WerkzeugFehler("Um wen geht es? Der Name fehlt.")
        gesucht = name.casefold()
        vorhanden = self.alle()
        for eintrag in vorhanden:
            if eintrag.name.casefold() == gesucht:
                return eintrag
        kontakt = self._kontakt_finden(name)
        if kontakt is not None:
            for eintrag in vorhanden:
                if eintrag.schluessel == kontakt.schluessel:
                    return eintrag
        if not anlegen:
            raise WerkzeugFehler(f"Zu „{name}“ habe ich nichts gespeichert.")
        schluessel = kontakt.schluessel if kontakt else f"name:{gesucht}"
        anzeige = (
            name
            if kontakt is None or gesucht in (s.casefold() for s in kontakt.spitznamen)
            else kontakt.name
        )
        emails = ",".join(kontakt.emails) if kontakt else ""
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT OR IGNORE INTO kontakte_pflege (schluessel, name, emails, erstellt) "
                "VALUES (?, ?, ?, ?)",
                (schluessel, anzeige, emails, self.jetzt().date().isoformat()),
            )
            zeile = db.execute(
                "SELECT * FROM kontakte_pflege WHERE schluessel = ?", (schluessel,)
            ).fetchone()
        return self._pflege(zeile)

    def _kontakt_finden(self, name: str) -> Kontakt | None:
        if self.adressbuch is None:
            return None
        try:
            treffer = self.adressbuch.suchen(name)
        except WerkzeugFehler as fehler:
            log.warning("Kontakte nicht lesbar: %s", fehler)
            return None
        genau = [
            k
            for k in treffer
            if name.casefold() in (k.name.casefold(), *(s.casefold() for s in k.spitznamen))
        ]
        if len(genau) == 1:
            return genau[0]
        if len(treffer) == 1:
            return treffer[0]
        if len(treffer) > 1:
            namen = ", ".join(k.name for k in treffer[:6])
            raise WerkzeugFehler(f"Zu „{name}“ passen mehrere Kontakte: {namen}. Wen meinst du?")
        return None

    def _setzen(self, schluessel: str, **felder: Any) -> None:
        zuweisung = ", ".join(f"{name} = ?" for name in felder)
        with verbinden(self.datei) as db:
            db.execute(
                f"UPDATE kontakte_pflege SET {zuweisung} WHERE schluessel = ?",
                (*felder.values(), schluessel),
            )

    # -------------------------------------------------------------- Werkzeuge

    def kontakte_suchen(self, name: str) -> str:
        if self.adressbuch is None:
            raise WerkzeugFehler("Nextcloud-Kontakte sind nicht eingerichtet.")
        treffer = self.adressbuch.suchen(name)
        if not treffer:
            return f"Keine Kontakte zu „{name}“ gefunden."
        return f"Kontakte zu „{name}“:\n" + "\n".join(k.zeile() for k in treffer[:20])

    def geburtstage_anzeigen(self, tage: int = 30) -> str:
        if self.adressbuch is None:
            raise WerkzeugFehler("Nextcloud-Kontakte sind nicht eingerichtet.")
        heute = self.jetzt().date()
        bald = []
        for kontakt in self.adressbuch.kontakte():
            if kontakt.geburtstag is None:
                continue
            tag = naechster_geburtstag(kontakt.geburtstag, heute)
            if (tag - heute).days <= max(1, min(int(tage), 366)):
                bald.append((tag, kontakt))
        if not bald:
            return f"In den nächsten {tage} Tagen hat niemand Geburtstag."
        zeilen = []
        for tag, kontakt in sorted(bald, key=lambda t: t[0]):
            alter = alter_am(kontakt.geburtstag, tag)  # type: ignore[arg-type]
            zusatz = f" (wird {alter})" if alter else ""
            zeilen.append(f"- {datum_lang(tag)}: {kontakt.name}{zusatz}")
        return f"Geburtstage in den nächsten {tage} Tagen:\n" + "\n".join(zeilen)

    def wichtig_markieren(self, name: str, abstand: Any) -> str:
        tage = abstand_lesen(abstand)
        if tage < 1:
            raise WerkzeugFehler("Der Abstand muss mindestens einen Tag betragen.")
        eintrag = self._eintrag(name)
        self._setzen(eintrag.schluessel, abstand_tage=tage)
        hinweis = (
            ""
            if eintrag.emails
            else (
                " Eine E-Mail-Adresse kenne ich nicht – sag mir Bescheid, wenn ihr euch "
                "gemeldet habt."
            )
        )
        return (
            f"{eintrag.name} ist jetzt ein wichtiger Kontakt: {abstand_text(tage)} melden. "
            f"Ich erinnere dich, wenn es länger her ist.{hinweis}"
        )

    def nicht_mehr_wichtig(self, name: str) -> str:
        eintrag = self._eintrag(name, anlegen=False)
        self._setzen(eintrag.schluessel, abstand_tage=None)
        return (
            f"{eintrag.name} ist kein wichtiger Kontakt mehr – ich erinnere nicht mehr ans Melden."
        )

    def gemeldet(self, name: str, wann: str | None = None, art: str | None = None) -> str:
        heute = self.jetzt().date()
        tag = heute
        if wann:
            roh = str(wann).strip().casefold()
            if roh in ("gestern",):
                tag = heute - timedelta(days=1)
            elif roh in ("vorgestern",):
                tag = heute - timedelta(days=2)
            elif roh not in ("heute", "gerade", "eben", "jetzt"):
                try:
                    tag = date.fromisoformat(roh[:10])
                except ValueError as fehler:
                    raise WerkzeugFehler(
                        "wann bitte als heute, gestern oder 2026-11-08."
                    ) from fehler
            if tag > heute:
                raise WerkzeugFehler("Das liegt in der Zukunft.")
        eintrag = self._eintrag(name)
        self._eintragen_kontakt(eintrag, tag, (art or "Chat").strip() or "Chat")
        return f"Vermerkt: Kontakt mit {eintrag.name} am {tag:%d.%m.%Y}" + (
            f" ({art})." if art else "."
        )

    def _eintragen_kontakt(self, eintrag: Pflege, tag: date, art: str) -> None:
        if eintrag.letzter_kontakt and eintrag.letzter_kontakt > tag:
            return
        self._setzen(eintrag.schluessel, letzter_kontakt=tag.isoformat(), letzte_art=art)

    def wichtige_anzeigen(self) -> str:
        eintraege = self.alle()
        if not eintraege:
            return "Du hast noch keine wichtigen Kontakte markiert."
        heute = self.jetzt().date()
        return "Kontaktpflege:\n" + "\n".join(e.zeile(heute) for e in eintraege)

    # -------------------------------------------------------------- Mails

    def adressen(self, name: str) -> dict[str, str]:
        """Adressquelle für E-Mail-Entwürfe: Name → {Adresse: Anzeigename}."""
        if self.adressbuch is None:
            return {}
        return {e: k.name for k in self.adressbuch.suchen(name) for e in k.emails}

    def mail_gesendet(self, empfaenger: list[str]) -> None:
        """Nach dem Senden einer Mail: letzter Kontakt = heute."""
        self._mail_kontakt({e.lower() for e in empfaenger}, self.jetzt().date())

    def _mail_kontakt(self, adressen: set[str], tag: date) -> None:
        for eintrag in self.alle():
            if adressen & set(eintrag.emails):
                self._eintragen_kontakt(eintrag, tag, "E-Mail")

    def mails_auswerten(self, anzahl: int = 200) -> None:
        """Liest Posteingang und Gesendet und vermerkt den letzten Mail-Kontakt."""
        if self.mail_konto is None or not any(e.emails for e in self.alle()):
            return
        for ordner in ("INBOX", self.mail_konto.ordner_gesendet):
            try:
                koepfe = self.mail_konto.koepfe(ordner, anzahl)
            except WerkzeugFehler as fehler:
                log.warning("Kontaktpflege: %s nicht lesbar: %s", ordner, fehler)
                continue
            for kopf in koepfe:
                try:
                    tag = datetime.strptime(kopf.datum[:10], "%d.%m.%Y").date()
                except ValueError:
                    continue
                beteiligte = {a for _, a in mail_adressen(kopf.von) + mail_adressen(kopf.an)}
                self._mail_kontakt(beteiligte, tag)

    # -------------------------------------------------------------- täglicher Auftrag

    async def taeglich_pruefen(self) -> None:
        import asyncio

        await asyncio.to_thread(self.mails_auswerten)
        await asyncio.to_thread(self.geburtstage_planen)
        await self.abstaende_pruefen()

    def geburtstage_planen(self) -> int:
        """Legt Geburtstags-Erinnerungen im Zeitplaner an (vorher und am Tag, nie doppelt)."""
        if self.adressbuch is None:
            return 0
        jetzt = self.jetzt()
        heute = jetzt.date()
        anzahl = 0
        for kontakt in self.adressbuch.kontakte():
            if kontakt.geburtstag is None:
                continue
            tag = naechster_geburtstag(kontakt.geburtstag, heute)
            abstand = (tag - heute).days
            if abstand > self.vorlauf_tage:
                continue
            alter = alter_am(kontakt.geburtstag, tag)
            if abstand > 0 and self.vorlauf_tage > 0:
                wann = max(
                    jetzt,
                    wandzeit(tag - timedelta(days=self.vorlauf_tage), self.pruefzeit, self.zone),
                )
                noch = (tag - wann.date()).days
                wann_text = "morgen" if noch == 1 else f"in {noch} Tagen"
                self.zeitplaner.anlegen(
                    f"Geburtstag {kontakt.name} {wann_text}",
                    wann,
                    nachricht=(
                        f"🎂 {wann_text[0].upper()}{wann_text[1:]} ({WOCHENTAGE[tag.weekday()]}, "
                        f"{tag:%d.%m.}) hat "
                        f"{kontakt.name} Geburtstag"
                        + (f" – {alter} Jahre!" if alter else "!")
                        + " Schon an eine Karte oder ein kleines Geschenk gedacht? 🎁"
                    ),
                    schluessel=f"geburtstag:{kontakt.schluessel}:{tag.isoformat()}:vorher",
                    herkunft="geburtstag",
                )
                anzahl += 1
            am_tag = max(jetzt, wandzeit(tag, self.pruefzeit, self.zone))
            self.zeitplaner.anlegen(
                f"Geburtstag {kontakt.name}",
                am_tag,
                nachricht=(
                    f"🎉 Heute hat {kontakt.name} Geburtstag"
                    + (f" ({alter})" if alter else "")
                    + "! Ein Anruf oder ein paar liebe Zeilen freuen bestimmt. 💐"
                ),
                schluessel=f"geburtstag:{kontakt.schluessel}:{tag.isoformat()}:tag",
                herkunft="geburtstag",
            )
            anzahl += 1
        return anzahl

    def ueberfaellige(self) -> list[tuple[Pflege, int]]:
        """Wichtige Kontakte, deren Abstand überschritten ist (mit Tagen seit dem Kontakt)."""
        heute = self.jetzt().date()
        ergebnis = []
        for eintrag in self.alle(nur_wichtige=True):
            basis = eintrag.letzter_kontakt or eintrag.erstellt
            tage = (heute - basis).days
            if tage <= (eintrag.abstand_tage or 0):
                continue
            pause = max(3, (eintrag.abstand_tage or 0) // 4)
            if eintrag.letzte_erinnerung and (heute - eintrag.letzte_erinnerung).days < pause:
                continue
            ergebnis.append((eintrag, tage))
        return ergebnis

    def freier_zeitpunkt(self, dauer: timedelta = timedelta(minutes=30)) -> datetime | None:
        """Nächster freier Zeitpunkt in drei Tagen (werktags 17–20, sonst 10–19 Uhr)."""
        if self.kalender is None:
            return None
        from missa.tools.kalender import freie_luecke

        jetzt = self.jetzt()
        start = wandzeit(jetzt.date(), time(0), self.zone)
        try:
            belegt = self.kalender.belegungen(start, start + timedelta(days=4))
        except WerkzeugFehler as fehler:
            log.warning("Kontaktpflege: Kalender nicht lesbar: %s", fehler)
            return None
        for versatz in range(4):
            tag = jetzt.date() + timedelta(days=versatz)
            von, bis = (time(17), time(20)) if tag.weekday() < 5 else (time(10), time(19))
            anfang = max(wandzeit(tag, von, self.zone), jetzt.replace(second=0, microsecond=0))
            # auf die nächste Viertelstunde runden
            rest = anfang.minute % 15
            if rest:
                anfang += timedelta(minutes=15 - rest)
            if luecke := freie_luecke(belegt, anfang, wandzeit(tag, bis, self.zone), dauer):
                return luecke
        return None

    async def abstaende_pruefen(self) -> int:
        """Erinnert an wichtige Kontakte, bei denen der Abstand überschritten ist."""
        import asyncio

        anzahl = 0
        for eintrag, tage in await asyncio.to_thread(self.ueberfaellige):
            vorschlag = await asyncio.to_thread(self.freier_zeitpunkt)
            vorschlag_text = ""
            if vorschlag is not None:
                tagname = (
                    "heute"
                    if vorschlag.date() == self.jetzt().date()
                    else (
                        "morgen"
                        if vorschlag.date() == self.jetzt().date() + timedelta(days=1)
                        else f"am {WOCHENTAGE[vorschlag.weekday()]}"
                    )
                )
                vorschlag_text = f"{tagname} um {vorschlag:%H:%M} Uhr"
            seit = (
                f"seit {tage} Tagen"
                if eintrag.letzter_kontakt
                else f"seit {tage} Tagen (seit du {eintrag.name} als wichtig markiert hast)"
            )
            ersatz = (
                f"💌 Du hast dich {seit} nicht mehr bei {eintrag.name} gemeldet – du wolltest "
                f"das {abstand_text(eintrag.abstand_tage or 0)} tun. "
                + (
                    f"Wie wär's {vorschlag_text}? Da ist dein Kalender frei. "
                    if vorschlag_text
                    else ""
                )
                + f"{eintrag.name} freut sich bestimmt! 😊"
            )
            auftrag = (
                f"Erinnere Louis freundlich und kurz daran, sich bei {eintrag.name} zu melden. "
                f"Er hat sich {seit} nicht gemeldet und wollte das "
                f"{abstand_text(eintrag.abstand_tage or 0)} tun. "
                + (
                    f"Schlag ihm vor: {vorschlag_text} – da ist sein Kalender frei. "
                    if vorschlag_text
                    else ""
                )
                + "Höchstens drei Sätze, gern mit einem Emoji."
            )
            text = ersatz
            if self.formulierer is not None:
                try:
                    text = await self.formulierer(auftrag, ersatz)
                except Exception:
                    log.exception("Kontaktpflege: Formulieren fehlgeschlagen")
            heute = self.jetzt().date().isoformat()
            self.zeitplaner.anlegen(
                f"Bei {eintrag.name} melden",
                self.jetzt(),
                nachricht=text,
                schluessel=f"kontakt:{eintrag.schluessel}:{heute}",
                herkunft="kontaktpflege",
            )
            self._setzen(eintrag.schluessel, letzte_erinnerung=heute)
            anzahl += 1
        return anzahl


def kontakt_werkzeuge(pflege: Kontaktpflege) -> list[Werkzeug]:
    name = {"type": "string", "description": "Name, Spitzname oder Rolle, z. B. Oma"}
    return [
        Werkzeug(
            "kontakte_suchen",
            "Sucht Kontakte in Nextcloud (Name, Spitzname, E-Mail) mit Adresse und Geburtstag.",
            {"type": "object", "properties": {"name": name}, "required": ["name"]},
            pflege.kontakte_suchen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "geburtstage_anzeigen",
            "Zeigt, wer in den nächsten Tagen Geburtstag hat.",
            {
                "type": "object",
                "properties": {"tage": {"type": "integer", "description": "Standard 30"}},
            },
            pflege.geburtstage_anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "kontakt_wichtig_markieren",
            "Markiert eine Person als wichtig: Missa erinnert, wenn Louis sich länger als den "
            "Abstand nicht gemeldet hat („Oma mindestens alle 3 Wochen“).",
            {
                "type": "object",
                "properties": {
                    "name": name,
                    "abstand": {"type": "string", "description": "z. B. „3 Wochen“, „monatlich“"},
                },
                "required": ["name", "abstand"],
            },
            pflege.wichtig_markieren,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "kontakt_nicht_mehr_wichtig",
            "Nimmt eine Person aus der Kontaktpflege (keine Erinnerungen mehr).",
            {"type": "object", "properties": {"name": name}, "required": ["name"]},
            pflege.nicht_mehr_wichtig,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "kontakt_gemeldet",
            "Vermerkt, dass Louis Kontakt mit einer Person hatte "
            "(„hab gerade mit Oma telefoniert“).",
            {
                "type": "object",
                "properties": {
                    "name": name,
                    "wann": {
                        "type": "string",
                        "description": "heute (Standard), gestern oder Datum",
                    },
                    "art": {"type": "string", "description": "z. B. Telefon, Treffen, Nachricht"},
                },
                "required": ["name"],
            },
            pflege.gemeldet,
            stufe=Stufe.SCHREIBEN,
        ),
        Werkzeug(
            "wichtige_kontakte_anzeigen",
            "Zeigt die wichtigen Kontakte mit gewünschtem Abstand und letztem Kontakt.",
            {"type": "object", "properties": {}},
            pflege.wichtige_anzeigen,
            stufe=Stufe.LESEN,
        ),
    ]
