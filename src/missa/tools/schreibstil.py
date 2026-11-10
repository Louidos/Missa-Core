"""Schreibstil-Profil von Louis: aus gesendeten Mails abgeleitet, als Datei bearbeitbar.

Die Datei (``MAIL_STIL_DATEI``, Standard ``MISSA_DATEN/schreibstil.md``) hat zwei Teile:

- ``## Allgemein`` – wird von ``schreibstil_lernen`` aus gesendeten Mails neu erzeugt
  (Anrede, Grussformel, Unterschrift, Länge, Ton, Schreibweise).
- ``## Eigene Regeln`` – bleibt immer erhalten; hier landen Wünsche aus dem Chat
  („schreib förmlicher an Lehrer“) und alles, was Louis von Hand ergänzt.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from missa.core.werkzeuge import WerkzeugFehler

ANREDEN = re.compile(
    r"^(hallo|hoi|hi|hey|halli hallo|liebe[rs]?|sali|salü|salut|guten (?:tag|morgen|abend)|"
    r"sehr geehrte[rs]?|grüezi|grüss gott|moin|servus|ciao|bonjour)\b",
    re.IGNORECASE,
)
GRUESSE = re.compile(
    r"^((?:liebe|viele|beste|herzliche|freundliche|schöne|sonnige)?\s*gr(?:ü|ue)(?:ss|ß)e?\b.*|"
    r"(?:mit\s+)?freundliche[nm]?\s+gr(?:ü|ue)(?:ss|ß)en?.*|gruss.*|lg|mfg|bis bald.*|"
    r"bis dann.*|cheers.*|herzlich.*|en gruess.*|e gruess.*|liebs grüessli.*|merci.*)$",
    re.IGNORECASE,
)
ZITAT = re.compile(
    r"^(>|am .+ schrieb .+:|on .+ wrote:|-----\s*original|von:\s|from:\s|gesendet:\s)",
    re.IGNORECASE,
)
DU = re.compile(r"\b(du|dir|dich|dein\w*|euch|eure?\w*)\b", re.IGNORECASE)
SIE = re.compile(r"(?<![.!?]\s)(?<!^)\b(Sie|Ihnen|Ihre?\w*)\b")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")

KOPF = "# Schreibstil von Louis"
ALLGEMEIN = "## Allgemein"
REGELN = "## Eigene Regeln"
STANDARD_REGELN = "- (noch keine – sag mir z. B. „schreib förmlicher an Lehrer“)"


def ohne_zitat(text: str) -> str:
    """Schneidet zitierte Antworten und Weiterleitungen ab."""
    zeilen = []
    for zeile in text.replace("\r\n", "\n").split("\n"):
        if ZITAT.match(zeile.strip()):
            break
        zeilen.append(zeile.rstrip())
    return "\n".join(zeilen).strip()


def _anrede_muster(zeile: str) -> str:
    """„Hallo Peter,“ → „Hallo <Vorname>,“; „Sehr geehrte Frau Muster“ → „… Frau <Nachname>,“."""
    treffer = ANREDEN.match(zeile)
    if not treffer:
        return ""
    gruss = zeile[: treffer.end()]
    rest = zeile[treffer.end() :].strip().rstrip(",!").strip()
    if re.match(r"(herr|frau)\b", rest, re.IGNORECASE):
        titel = rest.split()[0].capitalize()
        return f"{gruss} {titel} <Nachname>,"
    if rest:
        return f"{gruss} <Vorname>,"
    return f"{gruss},"


def _haeufigste(werte: Iterable[str]) -> tuple[str, int, int]:
    zaehler = Counter(w for w in werte if w)
    if not zaehler:
        return "", 0, 0
    wert, anzahl = zaehler.most_common(1)[0]
    return wert, anzahl, sum(zaehler.values())


def stil_ableiten(texte: list[str]) -> list[str]:
    """Leitet aus Mail-Texten die Zeilen für ``## Allgemein`` ab."""
    mails = [t for t in (ohne_zitat(t) for t in texte) if t]
    if not mails:
        raise WerkzeugFehler("In den gesendeten Mails habe ich keinen eigenen Text gefunden.")

    anreden_du, anreden_sie, gruesse_du, gruesse_sie, unterschriften = [], [], [], [], []
    woerter, du_mails, sie_mails, emoji_mails, mit_eszett, mit_ss = [], 0, 0, 0, 0, 0
    for text in mails:
        zeilen = [z.strip() for z in text.split("\n") if z.strip()]
        ist_sie = len(SIE.findall(text)) > len(DU.findall(text))
        if ist_sie:
            sie_mails += 1
        else:
            du_mails += 1
        anrede = _anrede_muster(zeilen[0]) if zeilen else ""
        (anreden_sie if ist_sie else anreden_du).append(anrede)
        for index in range(len(zeilen) - 1, max(-1, len(zeilen) - 5), -1):
            if GRUESSE.match(zeilen[index]):
                (gruesse_sie if ist_sie else gruesse_du).append(zeilen[index].rstrip(","))
                if index + 1 < len(zeilen):
                    unterschriften.append(zeilen[index + 1])
                break
        inhalt = [z for z in zeilen if not GRUESSE.match(z) and not ANREDEN.match(z)]
        woerter.append(sum(len(z.split()) for z in inhalt))
        emoji_mails += bool(EMOJI.search(text))
        mit_eszett += "ß" in text
        mit_ss += bool(re.search(r"(grüsse|gruss|strasse|gross|heisst|weiss|muss)", text, re.I))

    def anteil(teil: int) -> str:
        return f"{round(100 * teil / len(mails))} %"

    zeilen = []
    for art, liste in (("Du", anreden_du), ("Sie", anreden_sie)):
        wert, anzahl, gesamt = _haeufigste(liste)
        if wert:
            zeilen.append(f"- Anrede ({art}-Form): „{wert}“ ({anzahl} von {gesamt})")
    for art, liste in (("Du", gruesse_du), ("Sie", gruesse_sie)):
        wert, anzahl, gesamt = _haeufigste(liste)
        if wert:
            zeilen.append(f"- Grussformel ({art}-Form): „{wert}“ ({anzahl} von {gesamt})")
    wert, _, _ = _haeufigste(unterschriften)
    if wert:
        zeilen.append(f"- Unterschrift: „{wert}“")
    median = int(statistics.median(woerter))
    laenge = "kurz" if median < 80 else "mittel" if median < 200 else "ausführlich"
    zeilen.append(f"- Länge: meist {laenge} (Median {median} Wörter ohne Anrede und Gruss)")
    ton = (
        "meist Du-Form, locker und direkt" if du_mails >= sie_mails else "meist Sie-Form, förmlich"
    )
    zeilen.append(f"- Ton: {ton} (Du-Form in {anteil(du_mails)} der Mails)")
    if mit_ss and not mit_eszett:
        zeilen.append("- Schreibweise: Schweizer Rechtschreibung – immer „ss“, nie „ß“")
    zeilen.append(
        "- Emojis: " + ("gelegentlich" if emoji_mails * 4 >= len(mails) else "selten bis nie")
    )
    return zeilen


class Schreibstil:
    """Die Stil-Datei lesen, neu ableiten und um Regeln ergänzen."""

    def __init__(self, datei: Path) -> None:
        self.datei = datei

    def lesen(self) -> str:
        try:
            return self.datei.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _teile(self) -> tuple[str, str]:
        text = self.lesen()
        if REGELN in text:
            vorne, _, regeln = text.partition(REGELN)
            return vorne.strip(), regeln.strip()
        return text, ""

    def _schreiben(self, allgemein: str, regeln: str) -> None:
        self.datei.parent.mkdir(parents=True, exist_ok=True)
        inhalt = f"{allgemein.strip()}\n\n{REGELN}\n\n{regeln.strip() or STANDARD_REGELN}\n"
        self.datei.write_text(inhalt, encoding="utf-8")

    def lernen(self, texte: list[str]) -> str:
        zeilen = stil_ableiten(texte)
        _, regeln = self._teile()
        allgemein = (
            f"{KOPF}\n\n_Automatisch abgeleitet aus {len(texte)} gesendeten Mails am "
            f"{date.today():%d.%m.%Y}. Diese Datei darfst du frei bearbeiten; der Teil "
            f"„Eigene Regeln“ bleibt beim Neu-Lernen erhalten._\n\n{ALLGEMEIN}\n\n"
            + "\n".join(zeilen)
        )
        self._schreiben(allgemein, regeln)
        return self.lesen()

    def regel_hinzufuegen(self, regel: str) -> str:
        regel = " ".join(str(regel).split()).lstrip("-• ").strip()
        if not regel:
            raise WerkzeugFehler("Die Regel ist leer.")
        allgemein, regeln = self._teile()
        if not allgemein:
            allgemein = f"{KOPF}\n\n{ALLGEMEIN}\n\n- (noch nicht gelernt – schreibstil_lernen)"
        zeilen = [z for z in regeln.splitlines() if z.strip() and z.strip() != STANDARD_REGELN]
        if f"- {regel}".casefold() not in (z.casefold() for z in zeilen):
            zeilen.append(f"- {regel}")
        self._schreiben(allgemein, "\n".join(zeilen))
        return self.lesen()

    def regel_entfernen(self, teil: str) -> str:
        allgemein, regeln = self._teile()
        gesucht = str(teil).casefold().strip()
        zeilen = [z for z in regeln.splitlines() if z.strip()]
        rest = [z for z in zeilen if gesucht not in z.casefold()]
        if len(rest) == len(zeilen):
            raise WerkzeugFehler(f"Eine Regel mit „{teil}“ finde ich nicht.")
        self._schreiben(allgemein, "\n".join(rest))
        return self.lesen()

    def kontext(self) -> str:
        text = self.lesen()
        if not text:
            return (
                "## Schreibstil von Louis\nNoch kein Profil gelernt. Schreib natürlich, freundlich "
                "und knapp in Schweizer Rechtschreibung (ss statt ß) – oder ruf "
                "schreibstil_lernen auf."
            )
        return text.replace(KOPF, "## Schreibstil von Louis (für E-Mails in seinem Namen)", 1)
