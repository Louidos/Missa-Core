"""Dateien in Nextcloud (WebDAV): suchen, auflisten, lesen – schreiben nur in ``Missa/``.

- **Suchen** nach Datei- und Ordnernamen über die WebDAV-Suche von Nextcloud
  (``SEARCH`` auf ``/remote.php/dav/``), ganzer Speicher oder ein Ordner.
- **Lesen** nur von Textdateien (Markdown, Text, Konfiguration …), höchstens 1 MB.
- **Schreiben** nur im eigenen Ordner (``NEXTCLOUD_DATEIEN_ORDNER``, Standard
  ``Missa``). Jeder Pfad wird *relativ zu diesem Ordner* gebaut und danach geprüft –
  ein Pfad außerhalb ist schon im Code unmöglich. Bestehende Dateien werden nie
  überschrieben, nur ergänzt. **Löschen gibt es nicht.**
"""

from __future__ import annotations

import logging
import posixpath
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import httpx

from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler

log = logging.getLogger(__name__)

DAV = "{DAV:}"
TEXT_ENDUNGEN = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".csv",
        ".log",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".conf",
        ".cfg",
        ".env.example",
        ".sh",
        ".py",
        ".xml",
        ".html",
    }
)
MAX_BYTES = 1_000_000
MAX_ZEICHEN = 8000
MAX_TREFFER = 25
PROPFIND = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:displayname/><d:getcontentlength/><d:getlastmodified/><d:resourcetype/>
</d:prop></d:propfind>"""


def _such_xml(scope: str, wort: str) -> str:
    wort = wort.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
 <d:basicsearch>
  <d:select><d:prop><d:displayname/><d:getcontentlength/><d:getlastmodified/>
   <d:resourcetype/></d:prop></d:select>
  <d:from><d:scope><d:href>{scope}</d:href><d:depth>infinity</d:depth></d:scope></d:from>
  <d:where><d:like><d:prop><d:displayname/></d:prop><d:literal>%{wort}%</d:literal></d:like>
  </d:where>
  <d:orderby/>
  <d:limit><d:nresults>{MAX_TREFFER * 2}</d:nresults></d:limit>
 </d:basicsearch>
</d:searchrequest>"""


def ist_textdatei(pfad: str) -> bool:
    name = pfad.rsplit("/", 1)[-1].casefold()
    return any(name.endswith(endung) for endung in TEXT_ENDUNGEN)


class Dateien:
    """WebDAV-Zugriff auf die Dateien eines Nextcloud-Benutzers."""

    def __init__(
        self,
        url: str,
        benutzer: str,
        app_passwort: str,
        zone: ZoneInfo,
        eigener_ordner: str = "Missa",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.basis = url.rstrip("/")
        self.benutzer = benutzer
        self.zone = zone
        self.ordner = eigener_ordner.strip().strip("/") or "Missa"
        if "/" in self.ordner or self.ordner in (".", ".."):
            raise ValueError("Der eigene Ordner muss ein einfacher Ordnername sein, z. B. Missa")
        self.wurzel_pfad = f"/remote.php/dav/files/{quote(benutzer)}/"
        self._client = httpx.Client(auth=(benutzer, app_passwort), timeout=60, transport=transport)

    # -------------------------------------------------------------- Pfade

    def _url(self, pfad: str) -> str:
        return self.basis + self.wurzel_pfad + quote(pfad.strip("/"))

    def _relativ(self, href: str) -> str:
        pfad = unquote(httpx.URL(href).path if "://" in href else href)
        start = pfad.find(unquote(self.wurzel_pfad))
        return pfad[start + len(unquote(self.wurzel_pfad)) :].strip("/") if start >= 0 else pfad

    def eigener_pfad(self, pfad: str) -> str:
        """Pfad *innerhalb* des eigenen Ordners – oder WerkzeugFehler.

        ``notiz.md`` und ``Missa/notiz.md`` ergeben ``Missa/notiz.md``; ``../x``,
        ``/Dokumente/x`` oder ``Missa/../x`` werden abgelehnt.
        """
        roh = str(pfad).strip().replace("\\", "/")
        if not roh or "\x00" in roh:
            raise WerkzeugFehler("Der Dateiname fehlt.")
        ohne = roh.lstrip("/")
        if ohne == self.ordner or ohne.startswith(self.ordner + "/"):
            ohne = ohne[len(self.ordner) :].lstrip("/")
        elif roh.startswith("/"):
            raise WerkzeugFehler(self._nur_eigener_ordner(roh))
        ziel = posixpath.normpath(posixpath.join(self.ordner, ohne))
        if not ziel.startswith(self.ordner + "/") or ".." in ziel.split("/"):
            raise WerkzeugFehler(self._nur_eigener_ordner(roh))
        return ziel

    def _nur_eigener_ordner(self, pfad: str) -> str:
        return (
            f"„{pfad}“ liegt nicht in meinem Ordner „{self.ordner}/“. Schreiben darf ich nur "
            "dort – außerhalb ändere oder lösche ich nichts."
        )

    # -------------------------------------------------------------- HTTP

    def _anfrage(self, methode: str, pfad_oder_url: str, **weiter: Any) -> httpx.Response:
        url = pfad_oder_url if pfad_oder_url.startswith("http") else self._url(pfad_oder_url)
        try:
            antwort = self._client.request(methode, url, **weiter)
        except httpx.HTTPError as fehler:
            raise WerkzeugFehler(f"Nextcloud-Dateien nicht erreichbar ({fehler})") from fehler
        if antwort.status_code == 401:
            raise WerkzeugFehler("Nextcloud-Dateien: Anmeldung abgelehnt (App-Passwort prüfen)")
        return antwort

    def _eintraege(self, xml: bytes) -> list[dict[str, Any]]:
        try:
            wurzel = ET.fromstring(xml)
        except ET.ParseError as fehler:
            raise WerkzeugFehler("Nextcloud-Dateien lieferten ungültiges XML") from fehler
        eintraege = []
        for antwort in wurzel.iter(f"{DAV}response"):
            pfad = self._relativ(antwort.findtext(f"{DAV}href") or "")
            typ = antwort.find(f".//{DAV}resourcetype")
            ordner = typ is not None and typ.find(f"{DAV}collection") is not None
            groesse = antwort.findtext(f".//{DAV}getcontentlength")
            geaendert = antwort.findtext(f".//{DAV}getlastmodified")
            eintraege.append(
                {
                    "pfad": pfad,
                    "ordner": ordner,
                    "groesse": int(groesse) if groesse and groesse.isdigit() else None,
                    "geaendert": geaendert,
                }
            )
        return eintraege

    def _zeile(self, eintrag: dict[str, Any]) -> str:
        zeile = f"- {eintrag['pfad']}{'/' if eintrag['ordner'] else ''}"
        if eintrag.get("groesse") is not None and not eintrag["ordner"]:
            zeile += f" ({_groesse(eintrag['groesse'])})"
        if eintrag.get("geaendert"):
            try:
                zeit = parsedate_to_datetime(eintrag["geaendert"]).astimezone(self.zone)
                zeile += f", geändert {zeit:%d.%m.%Y}"
            except (TypeError, ValueError):
                pass
        return zeile

    # -------------------------------------------------------------- Werkzeuge

    def suchen(self, name: str, ordner: str | None = None) -> str:
        woerter = [w for w in str(name).replace("/", " ").split() if len(w) >= 2][:4]
        if not woerter:
            raise WerkzeugFehler("Wonach soll ich suchen? (Teil eines Datei- oder Ordnernamens)")
        bereich = str(ordner or "").strip("/")
        scope = f"/files/{self.benutzer}" + (f"/{bereich}" if bereich else "")
        gefunden: dict[str, dict[str, Any]] = {}
        for wort in woerter:
            antwort = self._anfrage(
                "SEARCH",
                f"{self.basis}/remote.php/dav/",
                content=_such_xml(scope, wort).encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
            if antwort.status_code >= 400:
                raise WerkzeugFehler(f"Dateisuche: Nextcloud antwortet mit {antwort.status_code}")
            for eintrag in self._eintraege(antwort.content):
                if eintrag["pfad"]:
                    gefunden.setdefault(eintrag["pfad"], eintrag)
        if not gefunden:
            wo = f" im Ordner „{bereich}“" if bereich else ""
            return f"Keine Datei und kein Ordner mit „{name}“ im Namen gefunden{wo}."

        def punkte(eintrag: dict[str, Any]) -> tuple[int, int, str]:
            pfad = eintrag["pfad"].casefold()
            return (
                -sum(w.casefold() in pfad for w in woerter),
                -int(ist_textdatei(pfad)),
                pfad,
            )

        treffer = sorted(gefunden.values(), key=punkte)[:MAX_TREFFER]
        return (
            f"Gefunden zu „{name}“ (die besten zuerst; Textdateien kann ich mit datei_lesen "
            "öffnen):\n" + "\n".join(self._zeile(e) for e in treffer)
        )

    def auflisten(self, ordner: str = "") -> str:
        pfad = str(ordner or "").strip("/")
        antwort = self._anfrage("PROPFIND", pfad, content=PROPFIND.encode(), headers={"Depth": "1"})
        if antwort.status_code == 404:
            raise WerkzeugFehler(f"Den Ordner „{pfad or '/'}“ gibt es nicht.")
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"Nextcloud-Dateien antworten mit {antwort.status_code}")
        eintraege = [e for e in self._eintraege(antwort.content) if e["pfad"].strip("/") != pfad]
        if not eintraege:
            return f"Der Ordner „{pfad or '/'}“ ist leer."
        eintraege.sort(key=lambda e: (not e["ordner"], e["pfad"].casefold()))
        return f"Inhalt von „{pfad or '/'}“:\n" + "\n".join(self._zeile(e) for e in eintraege)

    def lesen(self, pfad: str) -> str:
        pfad = str(pfad).strip().strip("/")
        if not pfad:
            raise WerkzeugFehler("Welche Datei? Der Pfad fehlt.")
        if not ist_textdatei(pfad):
            raise WerkzeugFehler(
                f"„{pfad}“ ist keine Textdatei – lesen kann ich nur Text und Markdown "
                "(z. B. .md, .txt, .conf, .yaml)."
            )
        antwort = self._anfrage("GET", pfad)
        if antwort.status_code == 404:
            raise WerkzeugFehler(f"Die Datei „{pfad}“ gibt es nicht. Such sie mit dateien_suchen.")
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"Nextcloud-Dateien antworten mit {antwort.status_code}")
        if len(antwort.content) > MAX_BYTES:
            raise WerkzeugFehler(f"„{pfad}“ ist größer als 1 MB – zu groß zum Lesen hier.")
        text = antwort.content.decode("utf-8", errors="replace")
        if len(text) > MAX_ZEICHEN:
            text = text[:MAX_ZEICHEN] + f"\n… (gekürzt, insgesamt {len(text)} Zeichen)"
        return f"Inhalt von „{pfad}“:\n\n{text}"

    def text_holen(self, pfad: str) -> str:
        """Ganzer Text einer Datei (für die Doku-Suche), ohne Kürzung."""
        antwort = self._anfrage("GET", str(pfad).strip("/"))
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"„{pfad}“ nicht lesbar ({antwort.status_code})")
        return antwort.content[:MAX_BYTES].decode("utf-8", errors="replace")

    def bytes_holen(self, pfad: str, max_bytes: int = 20_000_000) -> bytes:
        """Rohdaten einer Datei (z. B. PDF zum Zusammenfassen), höchstens ``max_bytes``."""
        antwort = self._anfrage("GET", str(pfad).strip("/"))
        if antwort.status_code == 404:
            raise WerkzeugFehler(f"Die Datei „{pfad}“ gibt es nicht. Such sie mit dateien_suchen.")
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"„{pfad}“ nicht lesbar ({antwort.status_code})")
        if len(antwort.content) > max_bytes:
            raise WerkzeugFehler(f"„{pfad}“ ist zu groß (über {max_bytes // 1_000_000} MB).")
        return antwort.content

    def alle_textdateien(self, ordner: str) -> list[dict[str, Any]]:
        """Alle Textdateien unter ``ordner`` (rekursiv per PROPFIND, für den Index)."""
        ergebnis: list[dict[str, Any]] = []
        offen = [str(ordner).strip("/")]
        gesehen: set[str] = set()
        while offen and len(gesehen) < 2000:
            aktuell = offen.pop()
            if aktuell in gesehen:
                continue
            gesehen.add(aktuell)
            antwort = self._anfrage(
                "PROPFIND", aktuell, content=PROPFIND.encode(), headers={"Depth": "1"}
            )
            if antwort.status_code >= 400:
                raise WerkzeugFehler(f"Ordner „{aktuell}“ nicht lesbar ({antwort.status_code})")
            for eintrag in self._eintraege(antwort.content):
                if eintrag["pfad"].strip("/") == aktuell:
                    continue
                if eintrag["ordner"]:
                    offen.append(eintrag["pfad"].strip("/"))
                elif ist_textdatei(eintrag["pfad"]):
                    ergebnis.append(eintrag)
        return ergebnis

    def _ordner_anlegen(self, pfad: str) -> None:
        teile = pfad.split("/")[:-1]
        for ende in range(1, len(teile) + 1):
            ordner = "/".join(teile[:ende])
            antwort = self._anfrage("MKCOL", ordner)
            if antwort.status_code not in (201, 405):  # 405 = gibt es schon
                raise WerkzeugFehler(
                    f"Ordner „{ordner}“ konnte nicht angelegt werden ({antwort.status_code})"
                )

    def schreiben(self, pfad: str, inhalt: str, anhaengen: bool = False) -> str:
        ziel = self.eigener_pfad(pfad)
        if not ist_textdatei(ziel):
            raise WerkzeugFehler("Ich schreibe nur Textdateien (z. B. .md oder .txt).")
        vorhanden = self._anfrage("GET", ziel)
        if vorhanden.status_code < 400:
            if not anhaengen:
                raise WerkzeugFehler(
                    f"„{ziel}“ gibt es schon – überschreiben tue ich nichts. Mit anhaengen=true "
                    "hänge ich den Text an, oder nimm einen neuen Namen."
                )
            text = vorhanden.content.decode("utf-8", errors="replace").rstrip() + "\n\n" + inhalt
        elif vorhanden.status_code == 404:
            self._ordner_anlegen(ziel)
            text = inhalt
        else:
            raise WerkzeugFehler(f"Nextcloud-Dateien antworten mit {vorhanden.status_code}")
        antwort = self._anfrage(
            "PUT",
            ziel,
            content=text.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        if antwort.status_code >= 400:
            raise WerkzeugFehler(f"Speichern fehlgeschlagen ({antwort.status_code})")
        log.info("Datei %s in Nextcloud %s", ziel, "ergänzt" if anhaengen else "angelegt")
        was = "ergänzt" if vorhanden.status_code < 400 else "angelegt"
        return f"Datei „{ziel}“ in Nextcloud {was} ({_groesse(len(text.encode()))})."


def _groesse(anzahl: int) -> str:
    if anzahl < 1024:
        return f"{anzahl} B"
    if anzahl < 1024 * 1024:
        return f"{anzahl / 1024:.0f} KB"
    return f"{anzahl / 1024 / 1024:.1f} MB"


def jetzt_text(zone: ZoneInfo) -> str:
    return f"{datetime.now(zone):%d.%m.%Y %H:%M}"


def dateien_werkzeuge(dateien: Dateien) -> list[Werkzeug]:
    return [
        Werkzeug(
            "dateien_suchen",
            "Sucht in Louis' Nextcloud-Dateien nach Datei- und Ordnernamen (z. B. „Wo ist die "
            "Doku zu rp01?“ → name=rp01). Nenne Louis den gefundenen Pfad.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Teil des Namens, z. B. rp01"},
                    "ordner": {"type": "string", "description": "Nur in diesem Ordner, optional"},
                },
                "required": ["name"],
            },
            dateien.suchen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "ordner_anzeigen",
            "Listet den Inhalt eines Nextcloud-Ordners (leer = oberste Ebene).",
            {"type": "object", "properties": {"ordner": {"type": "string"}}},
            dateien.auflisten,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "datei_lesen",
            "Liest eine Text- oder Markdown-Datei aus Nextcloud (Pfad aus dateien_suchen).",
            {
                "type": "object",
                "properties": {"pfad": {"type": "string", "description": "z. B. Doku/rp01.md"}},
                "required": ["pfad"],
            },
            dateien.lesen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "datei_schreiben",
            f"Legt eine Textdatei in deinem eigenen Ordner „{dateien.ordner}/“ an oder hängt "
            "Text an (anhaengen=true). Außerhalb dieses Ordners kannst du nichts schreiben, "
            "ändern oder löschen.",
            {
                "type": "object",
                "properties": {
                    "pfad": {
                        "type": "string",
                        "description": f"Dateiname in „{dateien.ordner}/“, z. B. notizen.md",
                    },
                    "inhalt": {"type": "string", "description": "Text bzw. Markdown"},
                    "anhaengen": {"type": "boolean", "description": "an bestehende anhängen"},
                },
                "required": ["pfad", "inhalt"],
            },
            dateien.schreiben,
            stufe=Stufe.SCHREIBEN,
        ),
    ]
