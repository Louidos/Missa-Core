"""Zentrale Rechte-Schicht: Bestätigung, Aktionsprotokoll und Notfall-Stopp.

Jeder Werkzeug-Aufruf läuft durch :meth:`missa.core.werkzeuge.Werkzeugkasten.ausfuehren`
und damit durch diese Schicht:

1. **Stufen** – jedes Werkzeug ist ``lesen``, ``schreiben`` oder ``zerstoererisch``
   (:class:`~missa.core.werkzeuge.Stufe`); ohne Stufe wird es nicht registriert.
2. **Bestätigung** – ein zerstörerisches Werkzeug läuft nur, wenn
   - es in einer *früheren* Anfrage mit genau denselben Angaben zurückgefragt wurde,
   - die Rückfrage höchstens :data:`STANDARD_GUELTIGKEIT_MINUTEN` Minuten alt ist und
   - Louis' *neue* Nachricht eine eindeutige Zustimmung ist (:func:`ist_zustimmung`).
   Eine Bestätigung gilt genau einmal. Im Hintergrund (Routine, Nachtschicht) nie.
3. **Aktionsprotokoll** – jeder Aufruf (auch abgelehnte) kommt mit Zeit, Kanal,
   Account, Werkzeug, Stufe, geschwärzten Parametern, Bestätigung und Ergebnis in die
   Tabelle ``aktionsprotokoll``.
4. **Notfall-Stopp** – solange die Datei ``MISSA_DATEN/notfall-stopp`` existiert, sind
   alle Werkzeuge außer ``lesen`` gesperrt, auch nach einem Neustart. Auslösen geht per
   Chat („Notfall-Stopp“) oder ``scripts/notfall-stopp.sh``; aufheben **nur** auf ai01
   mit ``scripts/notfall-freigabe.sh`` – dafür gibt es im Dienst keinen Weg.

Von Hand (für die Scripts): ``python -m missa.core.rechte stopp|freigabe|status``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from missa.core.datenbank import verbinden

if TYPE_CHECKING:
    from missa.core.chat import Vorbereitet
    from missa.core.werkzeuge import Werkzeug

log = logging.getLogger(__name__)

STANDARD_GUELTIGKEIT_MINUTEN = 10
NOTFALL_DATEINAME = "notfall-stopp"
FREIGABE_BEFEHL = "sudo /opt/missa/app/scripts/notfall-freigabe.sh"
#: So viele Zeichen eines Ergebnisses kommen ins Protokoll.
MAX_ERGEBNIS = 500
GESCHWAERZT = "***"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aktionsprotokoll (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeit TEXT NOT NULL,
    kanal TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    werkzeug TEXT NOT NULL,
    stufe TEXT NOT NULL,
    parameter TEXT NOT NULL DEFAULT '{}',
    bestaetigt INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    ergebnis TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS aktionsprotokoll_zeit ON aktionsprotokoll (zeit);
"""

#: Lesbare Namen der Status im Protokoll.
STATUS_TEXT = {
    "ausgefuehrt": "ausgeführt",
    "fehlgeschlagen": "fehlgeschlagen",
    "wartet_auf_bestaetigung": "Rückfrage gestellt",
    "gesperrt_notfall": "gesperrt (Notfall-Stopp)",
    "abgelehnt": "abgelehnt",
    "ungueltig": "ungültiger Aufruf",
    "notfall_stopp": "Notfall-Stopp ausgelöst",
    "notfall_freigabe": "Notfall-Stopp aufgehoben",
}

# ----------------------------------------------------------------------------- Zustimmung

#: Wörter, die eine Zustimmung ungültig machen.
NEIN = frozenset(
    [
        "nein",
        "nö",
        "nee",
        "ne",
        "nicht",
        "nich",
        "kein",
        "keine",
        "keinen",
        "stopp",
        "stop",
        "halt",
        "warte",
        "wart",
        "abbrechen",
        "lieber",
        "später",
        "moment",
        "doch",
        "vielleicht",
        "eventuell",
        "weiss",
        "weiß",
    ]
)
#: Wörter, die eine Zustimmung ausdrücken.
JA = frozenset(
    [
        "ja",
        "jap",
        "jep",
        "jo",
        "jup",
        "jawohl",
        "yes",
        "yep",
        "ok",
        "okay",
        "okey",
        "klar",
        "genau",
        "gerne",
        "gern",
        "mach",
        "machs",
        "los",
        "bestätigt",
        "bestätige",
        "einverstanden",
        "sicher",
        "passt",
        "unbedingt",
        "logo",
        "go",
        "lösch",
        "lösche",
        "löschen",
        "weg",
        "tu",
        "tus",
    ]
)
#: Längere Nachrichten sind keine knappe Zustimmung mehr.
MAX_ZUSTIMMUNG_WOERTER = 12


def ist_zustimmung(text: str | None) -> bool:
    """``True`` für eine knappe, eindeutige Zustimmung wie „Ja“ oder „Ja, lösch es“.

    Alles mit Verneinung, Zögern oder viel Text („Ja, aber erst morgen …“) zählt nicht.
    """
    if not text:
        return False
    woerter = re.findall(r"[a-zäöüß]+", text.casefold())
    if not woerter or len(woerter) > MAX_ZUSTIMMUNG_WOERTER:
        return False
    if any(w in NEIN for w in woerter) or "aber" in woerter:
        return False
    return any(w in JA for w in woerter)


NOTFALL_MUSTER = re.compile(r"\bnot[\s-]*(?:fall|aus)[\s-]*stop+\b", re.IGNORECASE)


def ist_notfall_stopp(text: str | None) -> bool:
    """Erkennt „Notfall-Stopp“ (auch „Notfallstopp“, „Notfall Stop“, „Not-Aus-Stopp“)."""
    return bool(text and NOTFALL_MUSTER.search(text))


# ----------------------------------------------------------------------------- Schwärzen

GEHEIMER_NAME = re.compile(
    r"pass|kennwort|token|secret|geheim|schl(?:ue|ü)ssel|api[_-]?key|auth|cookie|credential",
    re.IGNORECASE,
)
GEHEIM_IM_TEXT = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 " + GESCHWAERZT),
    (
        re.compile(
            r"(?i)\b(pass(?:wor[dt])?|kennwort|token|secret|api[_-]?key|app[_-]?passwor[dt])"
            r"(\s*[:=]\s*)\S+"
        ),
        r"\1\2" + GESCHWAERZT,
    ),
]


def schwaerzen(wert: Any, geheimnisse: Iterable[str] = ()) -> Any:
    """Ersetzt Geheimnisse (Passwörter, Tokens, Schlüssel) durch ``***``.

    Geschwärzt werden Felder mit verdächtigem Namen, bekannte Geheimnisse aus der
    Konfiguration und typische Muster im Text (``Bearer …``, ``passwort=…``).
    """
    bekannte = sorted({g for g in geheimnisse if g and len(g) >= 4}, key=len, reverse=True)

    def text(s: str) -> str:
        for geheimnis in bekannte:
            s = s.replace(geheimnis, GESCHWAERZT)
        for muster, ersatz in GEHEIM_IM_TEXT:
            s = muster.sub(ersatz, s)
        return s

    def gehen(w: Any) -> Any:
        if isinstance(w, dict):
            return {
                str(k): GESCHWAERZT if GEHEIMER_NAME.search(str(k)) and v else gehen(v)
                for k, v in w.items()
            }
        if isinstance(w, list | tuple):
            return [gehen(x) for x in w]
        if isinstance(w, str):
            return text(w)
        return w

    return gehen(wert)


# ----------------------------------------------------------------------------- Protokoll


@dataclass(frozen=True)
class Aktion:
    """Ein Eintrag im Aktionsprotokoll."""

    id: int
    zeit: str
    kanal: str
    account: str
    werkzeug: str
    stufe: str
    parameter: dict[str, Any] | Any
    bestaetigt: bool
    status: str
    ergebnis: str

    def als_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "zeit": self.zeit,
            "kanal": self.kanal,
            "account": self.account,
            "werkzeug": self.werkzeug,
            "stufe": self.stufe,
            "parameter": self.parameter,
            "bestaetigt": self.bestaetigt,
            "status": self.status,
            "ergebnis": self.ergebnis,
        }

    def zeile(self) -> str:
        uhrzeit = self.zeit[11:16] if len(self.zeit) >= 16 else self.zeit
        angaben = ""
        if isinstance(self.parameter, dict) and self.parameter:
            angaben = ", ".join(f"{k}={v}" for k, v in self.parameter.items())
            angaben = f" ({angaben[:160]})"
        wo = self.kanal + (f"/{self.account}" if self.account else "")
        ja = ", bestätigt" if self.bestaetigt else ""
        status = STATUS_TEXT.get(self.status, self.status)
        return f"{uhrzeit} {self.werkzeug}{angaben} – {status}{ja} [{self.stufe}, {wo}]"


class Aktionsprotokoll:
    """Dauerhaftes Protokoll aller Aktionen in ``missa.db`` (Tabelle ``aktionsprotokoll``)."""

    def __init__(
        self,
        datei: Path,
        zone: ZoneInfo,
        geheimnisse: Iterable[str] = (),
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.datei = datei
        self.zone = zone
        self.geheimnisse = tuple(geheimnisse)
        self._uhr = uhr or (lambda: datetime.now(zone))
        # Automatische Migration beim Start: Tabelle anlegen, falls sie fehlt.
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def eintragen(
        self,
        *,
        werkzeug: str,
        stufe: str,
        parameter: Any,
        bestaetigt: bool,
        status: str,
        ergebnis: str,
        kanal: str,
        account: str = "",
    ) -> None:
        geschwaerzt = schwaerzen(parameter, self.geheimnisse)
        try:
            parameter_text = json.dumps(geschwaerzt, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            parameter_text = json.dumps(str(geschwaerzt), ensure_ascii=False)
        ergebnis = schwaerzen(str(ergebnis), self.geheimnisse)
        if len(ergebnis) > MAX_ERGEBNIS:
            ergebnis = ergebnis[: MAX_ERGEBNIS - 1] + "…"
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO aktionsprotokoll (zeit, kanal, account, werkzeug, stufe, parameter, "
                "bestaetigt, status, ergebnis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._uhr().astimezone(self.zone).isoformat(timespec="seconds"),
                    kanal or "intern",
                    account or "",
                    werkzeug,
                    stufe,
                    parameter_text,
                    int(bool(bestaetigt)),
                    status,
                    ergebnis,
                ),
            )

    def liste(
        self,
        von: date | None = None,
        bis: date | None = None,
        anzahl: int = 100,
        werkzeug: str | None = None,
        status: str | None = None,
    ) -> list[Aktion]:
        """Einträge von ``von`` bis einschließlich ``bis`` (neueste zuerst)."""
        bedingungen, werte = [], []
        if von is not None:
            bedingungen.append("zeit >= ?")
            werte.append(von.isoformat())
        if bis is not None:
            bedingungen.append("zeit < ?")
            werte.append((bis + timedelta(days=1)).isoformat())
        if werkzeug:
            bedingungen.append("werkzeug = ?")
            werte.append(werkzeug)
        if status:
            bedingungen.append("status = ?")
            werte.append(status)
        wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""
        with verbinden(self.datei) as db:
            zeilen = db.execute(
                f"SELECT * FROM aktionsprotokoll {wo} ORDER BY zeit DESC, id DESC LIMIT ?",
                [*werte, max(1, anzahl)],
            ).fetchall()
        return [self._aktion(z) for z in zeilen]

    @staticmethod
    def _aktion(zeile: Any) -> Aktion:
        try:
            parameter = json.loads(zeile["parameter"])
        except ValueError:
            parameter = zeile["parameter"]
        return Aktion(
            id=zeile["id"],
            zeit=zeile["zeit"],
            kanal=zeile["kanal"],
            account=zeile["account"],
            werkzeug=zeile["werkzeug"],
            stufe=zeile["stufe"],
            parameter=parameter,
            bestaetigt=bool(zeile["bestaetigt"]),
            status=zeile["status"],
            ergebnis=zeile["ergebnis"],
        )


# ----------------------------------------------------------------------------- Notfall


class NotfallSchalter:
    """Der Notfall-Stopp als Datei – überlebt Neustarts und ist per Script setzbar.

    Der Dienst kann den Stopp nur **auslösen**. Aufheben geht nur mit
    :func:`freigeben`, das ausschließlich die Kommandozeile (``notfall-freigabe.sh``
    auf ai01) aufruft.
    """

    def __init__(self, datei: Path, zone: ZoneInfo | None = None) -> None:
        self.datei = datei
        self.zone = zone

    @property
    def aktiv(self) -> bool:
        return self.datei.exists()

    def info(self) -> dict[str, Any]:
        if not self.aktiv:
            return {"aktiv": False}
        try:
            daten = json.loads(self.datei.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            daten = {}
        return {"aktiv": True, **(daten if isinstance(daten, dict) else {})}

    def ausloesen(self, grund: str, quelle: str) -> bool:
        """Setzt den Stopp. Gibt ``False`` zurück, wenn er schon aktiv war."""
        if self.aktiv:
            return False
        self.datei.parent.mkdir(parents=True, exist_ok=True)
        inhalt = {
            "seit": datetime.now(self.zone).isoformat(timespec="seconds"),
            "grund": grund,
            "quelle": quelle,
        }
        zwischen = self.datei.with_name(f".{self.datei.name}.{os.getpid()}")
        zwischen.write_text(json.dumps(inhalt, ensure_ascii=False) + "\n", encoding="utf-8")
        zwischen.replace(self.datei)
        log.warning("NOTFALL-STOPP ausgelöst (%s): %s", quelle, grund)
        return True


def freigeben(schalter: NotfallSchalter) -> bool:
    """Hebt den Stopp auf. Nur für ``notfall-freigabe.sh`` – nie aus dem Dienst aufrufen."""
    if not schalter.aktiv:
        return False
    schalter.datei.unlink()
    return True


# ----------------------------------------------------------------------------- Rechte


class Rechte:
    """Die zentrale Schicht, durch die jeder Werkzeug-Aufruf läuft.

    Ohne Protokoll und Notfall-Schalter (z. B. in Tests) gelten Stufen und
    Bestätigung trotzdem; protokolliert wird dann nur ins Log.
    """

    def __init__(
        self,
        protokoll: Aktionsprotokoll | None = None,
        notfall: NotfallSchalter | None = None,
        gueltigkeit_minuten: float = STANDARD_GUELTIGKEIT_MINUTEN,
        uhr: Callable[[], float] = time.monotonic,
    ) -> None:
        self.protokoll = protokoll
        self.notfall = notfall
        self.gueltigkeit = gueltigkeit_minuten * 60
        self._uhr = uhr
        self._offen: dict[str, tuple[str, float]] = {}
        self._sperre = threading.Lock()

    # -------------------------------------------------------------- Bestätigung

    @staticmethod
    def schluessel(werkzeug: str, argumente: dict[str, Any]) -> str:
        """Genau diese Aktion: Werkzeug und Angaben (ohne ``bestaetigt``, Zahlen als Text)."""
        angaben = {
            k: v if isinstance(v, dict | list) else str(v).strip()
            for k, v in argumente.items()
            if k != "bestaetigt"
        }
        return f"{werkzeug}:{json.dumps(angaben, sort_keys=True, ensure_ascii=False)}"

    def _aufraeumen(self) -> None:
        grenze = self._uhr() - self.gueltigkeit
        for s in [s for s, (_, zeit) in self._offen.items() if zeit < grenze]:
            del self._offen[s]

    def vormerken(self, werkzeug: str, argumente: dict[str, Any], anfrage_id: str) -> None:
        """Merkt die Rückfrage vor (eine schon offene bleibt mit ihrer Zeit bestehen)."""
        with self._sperre:
            self._aufraeumen()
            self._offen.setdefault(self.schluessel(werkzeug, argumente), (anfrage_id, self._uhr()))

    def freigegeben(
        self, werkzeug: str, argumente: dict[str, Any], anfrage_id: str, nachricht: str | None
    ) -> bool:
        """``True`` (und verbraucht), wenn Louis genau diese Aktion gerade bestätigt hat."""
        if not ist_zustimmung(nachricht):
            return False
        schluessel = self.schluessel(werkzeug, argumente)
        with self._sperre:
            self._aufraeumen()
            eintrag = self._offen.get(schluessel)
            if eintrag is None or eintrag[0] == anfrage_id:
                return False
            del self._offen[schluessel]
            return True

    def rueckfrage_text(self, werkzeug: str, beschreibung: str) -> str:
        minuten = f"{self.gueltigkeit / 60:g}"
        return (
            f"NICHT AUSGEFÜHRT – {werkzeug} ist zerstörerisch und braucht Louis' ausdrückliches "
            "Ja zu genau dieser Aktion.\n"
            f"Was passieren würde:\n{beschreibung.strip()}\n"
            "Frag Louis klar und ruhig, ob du das wirklich tun sollst, und sag dabei genau, was "
            f"passiert. Erst wenn er in seiner nächsten Nachricht (innerhalb von {minuten} "
            f"Minuten) ausdrücklich Ja sagt, rufe {werkzeug} mit genau denselben Angaben noch "
            "einmal auf."
        )

    # -------------------------------------------------------------- Notfall

    def notfall_aktiv(self) -> bool:
        return self.notfall is not None and self.notfall.aktiv

    def notfall_text(self, werkzeug: str) -> str:
        return (
            f"GESPERRT – {werkzeug} wurde NICHT ausgeführt: Der Notfall-Stopp ist aktiv. "
            "Alle schreibenden und zerstörerischen Werkzeuge sind gesperrt, lesen und reden "
            "geht weiter. Aufheben kann Louis den Stopp nur direkt auf ai01 mit "
            f"„{FREIGABE_BEFEHL}“ – nicht per Chat. Sag ihm das ehrlich."
        )

    def notfall_ausloesen(self, grund: str, kanal: str, account: str = "") -> bool:
        if self.notfall is None:
            return False
        neu = self.notfall.ausloesen(grund, f"{kanal}/{account}" if account else kanal)
        if neu:
            self.protokollieren(
                werkzeug="notfall_stopp",
                stufe="lesen",
                parameter={"grund": grund},
                bestaetigt=False,
                status="notfall_stopp",
                ergebnis="Schreiben und Zerstörerisches gesperrt",
                kanal=kanal,
                account=account,
            )
        return neu

    # -------------------------------------------------------------- Protokoll

    def protokollieren(self, **eintrag: Any) -> None:
        """Schreibt einen Eintrag ins Aktionsprotokoll. Fehler stoppen die Aktion nicht."""
        log.info(
            "Aktion: %s [%s] %s (%s)",
            eintrag.get("werkzeug"),
            eintrag.get("stufe"),
            eintrag.get("status"),
            eintrag.get("kanal"),
        )
        if self.protokoll is None:
            return
        try:
            self.protokoll.eintragen(**eintrag)
        except Exception:
            log.exception("Aktionsprotokoll: Eintrag fehlgeschlagen")

    # -------------------------------------------------------------- Chat-Haken

    async def eingang(self, vorbereitet: Vorbereitet) -> str | None:
        """Eingangsprüfer: „Notfall-Stopp“ in Louis' Nachricht löst den Stopp im Code aus."""
        if not ist_notfall_stopp(vorbereitet.neue_nachricht):
            return None
        if self.notfall is None:
            return None
        neu = self.notfall_ausloesen(
            "per Chat: " + (vorbereitet.neue_nachricht or "")[:200],
            vorbereitet.kanal,
            vorbereitet.account,
        )
        zustand = "ausgelöst" if neu else "war schon aktiv und bleibt aktiv"
        return (
            f"Der Code hat den NOTFALL-STOPP {zustand}: Alle schreibenden und zerstörerischen "
            "Werkzeuge sind sofort gesperrt, auch nach einem Neustart. Lesen und reden geht "
            "weiter. Aufheben kann Louis ihn nur direkt auf ai01 mit "
            f"„{FREIGABE_BEFEHL}“, nicht per Chat. Bestätige ihm das ruhig und kurz."
        )

    def kontext(self, _frage: str) -> str | None:
        """Kontextquelle: Hinweis im Systemprompt, solange der Notfall-Stopp aktiv ist."""
        if not self.notfall_aktiv():
            return None
        return (
            "## Notfall-Stopp aktiv\n"
            "Du darfst gerade nichts anlegen, ändern, löschen oder senden – nur lesen und "
            "reden. Die Freigabe geht nur auf ai01 (nicht per Chat); versuche nie, sie zu "
            "umgehen."
        )


# ----------------------------------------------------------------------------- Werkzeug


def protokoll_werkzeuge(protokoll: Aktionsprotokoll, rechte: Rechte, zone: ZoneInfo):
    """Werkzeug, mit dem Missa „Was hast du heute gemacht?“ beantwortet."""
    from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler

    def aktionen_anzeigen(zeitraum: str = "heute", alle: bool = False) -> str:
        heute = datetime.now(zone).date()
        wort = (zeitraum or "heute").strip().casefold()
        if wort == "heute":
            von = bis = heute
        elif wort == "gestern":
            von = bis = heute - timedelta(days=1)
        elif wort in ("woche", "diese woche", "7 tage"):
            von, bis = heute - timedelta(days=6), heute
        else:
            try:
                von = bis = date.fromisoformat(wort)
            except ValueError as fehler:
                raise WerkzeugFehler(
                    "zeitraum muss „heute“, „gestern“, „woche“ oder ein Datum wie 2026-11-11 sein"
                ) from fehler
        aktionen = [
            a
            for a in reversed(protokoll.liste(von, bis, anzahl=500))
            if a.werkzeug != "aktionen_anzeigen"
            and (alle or a.stufe != "lesen" or a.status != "ausgefuehrt")
        ]
        titel = f"Aktionsprotokoll {von:%d.%m.%Y}" + (f" bis {bis:%d.%m.%Y}" if bis != von else "")
        zeilen = [f"{titel} ({len(aktionen)} Einträge):"]
        zeilen += [f"- {a.zeile()}" for a in aktionen] or ["- (keine Aktionen)"]
        if not alle:
            zeilen.append("(Reines Nachschauen ist ausgeblendet; mit alle=true alles zeigen.)")
        if rechte.notfall_aktiv():
            zeilen.append("Hinweis: Der Notfall-Stopp ist gerade aktiv.")
        return "\n".join(zeilen)

    werkzeug: Werkzeug = Werkzeug(
        "aktionen_anzeigen",
        "Zeigt Missas eigenes Aktionsprotokoll: was sie wann getan hat (Werkzeuge, Bestätigungen, "
        "Sperren). Für Fragen wie „Was hast du heute gemacht?“.",
        {
            "type": "object",
            "properties": {
                "zeitraum": {
                    "type": "string",
                    "description": "heute (Standard), gestern, woche oder ein Datum JJJJ-MM-TT",
                },
                "alle": {
                    "type": "boolean",
                    "description": "Auch reines Nachschauen (lesen) zeigen",
                },
            },
        },
        aktionen_anzeigen,
        stufe=Stufe.LESEN,
    )
    return [werkzeug]


def rechte_bauen(einstellungen: Any) -> tuple[Rechte, Aktionsprotokoll]:
    """Rechte-Schicht mit Protokoll und Notfall-Schalter aus den Einstellungen."""
    protokoll = Aktionsprotokoll(
        einstellungen.datenbank, einstellungen.zone, einstellungen.geheimnisse
    )
    schalter = NotfallSchalter(einstellungen.notfall_datei, einstellungen.zone)
    return (
        Rechte(protokoll, schalter, gueltigkeit_minuten=einstellungen.bestaetigung_minuten),
        protokoll,
    )


# ----------------------------------------------------------------------------- Kommandozeile


def main(argv: list[str] | None = None) -> int:
    """``python -m missa.core.rechte stopp|freigabe|status`` – für die Scripts auf ai01."""
    from missa.core.config import STANDARD_ENV_DATEI, Einstellungen, env_datei_laden

    parser = argparse.ArgumentParser(prog="python -m missa.core.rechte")
    parser.add_argument("befehl", choices=["stopp", "freigabe", "status"])
    parser.add_argument("--grund", default="per Script auf ai01")
    argumente = parser.parse_args(argv)

    env_datei_laden(Path(os.environ.get("MISSA_ENV_DATEI", str(STANDARD_ENV_DATEI))))
    einstellungen = Einstellungen.aus_umgebung()
    rechte, _ = rechte_bauen(einstellungen)
    schalter = rechte.notfall
    assert schalter is not None
    konto = os.environ.get("SUDO_USER") or os.environ.get("USER") or "ai01"

    if argumente.befehl == "status":
        info = schalter.info()
        if info["aktiv"]:
            print(f"NOTFALL-STOPP AKTIV seit {info.get('seit', '?')} – {info.get('grund', '')}")
            return 1
        print("Kein Notfall-Stopp aktiv.")
        return 0
    if argumente.befehl == "stopp":
        if rechte.notfall_ausloesen(argumente.grund, "ai01", konto):
            print("NOTFALL-STOPP ausgelöst: Schreiben und Zerstörerisches sind gesperrt.")
        else:
            print("Der Notfall-Stopp war schon aktiv.")
        return 0
    if freigeben(schalter):
        rechte.protokollieren(
            werkzeug="notfall_freigabe",
            stufe="lesen",
            parameter={},
            bestaetigt=True,
            status="notfall_freigabe",
            ergebnis="Notfall-Stopp auf ai01 aufgehoben",
            kanal="ai01",
            account=konto,
        )
        print("Notfall-Stopp aufgehoben. Missa darf wieder schreiben.")
    else:
        print("Es war kein Notfall-Stopp aktiv.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
