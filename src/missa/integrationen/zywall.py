"""Firewall-Logs der ZyWALL lesen (nur lesen – Missa hat keinen Zugang zur Firewall).

Die ZyWALL schickt ihre Logs per Syslog an ai01; rsyslog schreibt sie in eine Datei
(``FIREWALL_LOG_DATEI``, Einrichtung in ``docs/homelab-firewall.md``). Eine Zeile sieht
etwa so aus::

    2026-11-11T14:02:11+01:00 zywall src="203.0.113.7:54321" dst="198.51.100.10:22"
        msg="Match default rule, DROP" note="ACCESS BLOCK" cat="Security Policy" proto="ssh"

Ausgewertet werden die Felder ``src``, ``dst``, ``msg``, ``note``, ``cat`` und ``proto``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

FELD = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s")
KLASSISCH = re.compile(r"^(?:<\d+>)?([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}):(\d{2}):(\d{2})\s")
MONATE = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
)}  # fmt: skip
BLOCK = re.compile(r"\b(drop|dropped|block|blocked|deny|denied|reject|rejected)\b", re.IGNORECASE)
ERLAUBT = re.compile(r"\b(allow|allowed|accept|accepted|permit|forward)\b", re.IGNORECASE)


@dataclass(frozen=True)
class FirewallEreignis:
    zeit: datetime
    src_ip: str
    src_port: int | None
    dst_ip: str
    dst_port: int | None
    aktion: str  # blockiert, erlaubt oder sonstiges
    kategorie: str
    protokoll: str
    text: str


def _adresse(wert: str) -> tuple[str, int | None]:
    """``203.0.113.7:54321`` → (IP, Port); IPv6 in eckigen Klammern oder ohne Port."""
    wert = wert.strip()
    if wert.startswith("["):
        ip, _, rest = wert[1:].partition("]")
        port = rest.lstrip(":")
        return ip, int(port) if port.isdigit() else None
    if wert.count(":") == 1:
        ip, _, port = wert.partition(":")
        return ip, int(port) if port.isdigit() else None
    return wert, None


def zeit_lesen(zeile: str, zone: ZoneInfo, jetzt: datetime) -> datetime | None:
    if m := ISO.match(zeile):
        roh = m.group(1).replace("Z", "+00:00")
        if re.search(r"[+-]\d{4}$", roh):
            roh = roh[:-2] + ":" + roh[-2:]
        zeit = datetime.fromisoformat(roh)
        return zeit if zeit.tzinfo else zeit.replace(tzinfo=zone)
    if m := KLASSISCH.match(zeile):
        monat = MONATE.get(m.group(1))
        if monat is None:
            return None
        zeit = datetime(
            jetzt.year, monat, int(m.group(2)), int(m.group(3)), int(m.group(4)),
            int(m.group(5)), tzinfo=zone,
        )  # fmt: skip
        # Kein Jahr im klassischen Format: liegt die Zeit in der Zukunft, war es letztes Jahr.
        return zeit.replace(year=jetzt.year - 1) if zeit > jetzt.astimezone(zone) else zeit
    return None


def zeile_lesen(zeile: str, zone: ZoneInfo, jetzt: datetime) -> FirewallEreignis | None:
    """Eine Log-Zeile → Ereignis; ``None``, wenn sie keine Verbindung beschreibt."""
    felder = {m.group(1).casefold(): (m.group(2) if m.group(2) is not None else m.group(3))
              for m in FELD.finditer(zeile)}  # fmt: skip
    if "src" not in felder or "dst" not in felder:
        return None
    zeit = zeit_lesen(zeile, zone, jetzt)
    if zeit is None:
        return None
    src_ip, src_port = _adresse(felder["src"])
    dst_ip, dst_port = _adresse(felder["dst"])
    meldung = f"{felder.get('msg', '')} {felder.get('note', '')}"
    if BLOCK.search(meldung):
        aktion = "blockiert"
    elif ERLAUBT.search(meldung):
        aktion = "erlaubt"
    else:
        aktion = "sonstiges"
    return FirewallEreignis(
        zeit=zeit,
        src_ip=src_ip,
        src_port=src_port,
        dst_ip=dst_ip,
        dst_port=dst_port,
        aktion=aktion,
        kategorie=felder.get("cat", ""),
        protokoll=felder.get("proto", ""),
        text=meldung.strip()[:200],
    )
