"""Logs auswerten: relevante Zeilen herausfiltern, bekannte Ursachen erkennen, in Stücke teilen.

Das Modell bekommt nie ein riesiges Log, sondern die Zeilen, die nach einem Problem
aussehen (gleiche Zeilen zusammengefasst), und – wo der Code ein bekanntes Muster
erkennt – eine verständliche Erklärung der wahrscheinlichen Ursache.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

#: So viele Zeichen gibt ein Werkzeug höchstens auf einmal zurück (der Rest in Teilen).
MAX_ZEICHEN = 6000

FEHLER_MUSTER = re.compile(
    r"\b(error|err|fatal|critical|crit|emerg|alert|panic|exception|traceback|failed|failure|"
    r"denied|refused|timed? ?out|timeout|unreachable|unavailable|cannot|can't|could not|"
    r"unable to|no space|out of memory|oom|killed|segfault|sqlstate|warn(?:ing)?|"
    r"fehler|fehlgeschlagen|abgelehnt|bad gateway|502|503|504)\b",
    re.IGNORECASE,
)
#: Zeitstempel, Hex-IDs und Zahlen fallen beim Zusammenfassen gleicher Zeilen weg.
ZEITSTEMPEL = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}[T ][\d:.,]+(?:Z|[+-]\d{2}:?\d{2})?|[A-Z][a-z]{2} +\d+ [\d:]+)\s*"
)
VARIABEL = re.compile(r"\b(0x)?[0-9a-f]{8,}\b|\b\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class Ursache:
    """Ein bekanntes Muster in Logs und was es bedeutet."""

    name: str
    muster: re.Pattern[str]
    erklaerung: str
    tipp: str


def _u(name: str, muster: str, erklaerung: str, tipp: str) -> Ursache:
    return Ursache(name, re.compile(muster, re.IGNORECASE), erklaerung, tipp)


#: Bekannte Ursachen, die wichtigsten zuerst.
URSACHEN = (
    _u(
        "disk_voll",
        r"no space left on device|disk (is )?full|ENOSPC|not enough (free )?(disk )?space",
        "Die Festplatte ist voll – Programme können nichts mehr schreiben.",
        "Platz schaffen (alte Logs, Docker-Images mit „docker system prune“, Backups) oder die "
        "Disk der VM vergrößern.",
    ),
    _u(
        "speicher",
        r"out of memory|oom[- ]?kill|OOMKilled|cannot allocate memory|"
        r"allowed memory size of \d+ bytes exhausted|killed process \d+",
        "Der Arbeitsspeicher reicht nicht – das System oder PHP hat Prozesse beendet.",
        "Speicherfresser suchen, Limits (z. B. PHP memory_limit) prüfen oder der VM mehr RAM "
        "geben.",
    ),
    _u(
        "datenbank",
        r"SQLSTATE\[HY000\] \[2002\]|SQLSTATE\[08006\]|could not connect to (the database )?server|"
        r"can't connect to (local )?mysql server|connection to (the )?database|"
        r"Doctrine\\DBAL\\Exception\\ConnectionException|database (server )?(is )?(not )?"
        r"(unavailable|unreachable)",
        "Die Datenbank ist nicht erreichbar – der Dienst kann seine Daten nicht laden.",
        "Prüfen, ob der Datenbank-Container bzw. -Dienst (MariaDB/PostgreSQL) läuft, und dessen "
        "Logs ansehen.",
    ),
    _u(
        "zu_viele_verbindungen",
        r"too many connections|remaining connection slots are reserved",
        "Die Datenbank hat keine freien Verbindungen mehr.",
        "Datenbank neu starten oder max_connections erhöhen; prüfen, wer so viele Verbindungen "
        "öffnet.",
    ),
    _u(
        "datenbank_gesperrt",
        r"database is locked",
        "Die (SQLite-)Datenbank ist gesperrt – zwei Prozesse wollen gleichzeitig schreiben.",
        "Hängenden Prozess beenden; bei Nextcloud besser MariaDB/PostgreSQL statt SQLite nutzen.",
    ),
    _u(
        "wartungsmodus",
        r"maintenance mode|in maintenance|503 Service Unavailable.*maintenance|maintenance.php",
        "Nextcloud ist im Wartungsmodus – das passiert oft, wenn ein Update hängen geblieben ist.",
        "Auf dem Nextcloud-Host „occ upgrade“ prüfen und dann „occ maintenance:mode --off“.",
    ),
    _u(
        "redis",
        r"RedisException|redis.*(connection refused|connection lost|went away)|"
        r"Connection refused.*6379",
        "Redis (der Cache) ist nicht erreichbar.",
        "Prüfen, ob der Redis-Container bzw. -Dienst läuft.",
    ),
    _u(
        "zertifikat",
        r"certificate (has )?expired|x509: certificate|SSL certificate problem|"
        r"certificate verify failed|tls: failed to verify",
        "Ein TLS-Zertifikat ist abgelaufen oder ungültig.",
        "Zertifikat erneuern (bei Caddy meist automatisch – dann dessen Log zur ACME-Erneuerung "
        "ansehen).",
    ),
    _u(
        "dns",
        r"temporary failure in name resolution|could not resolve host|no such host|"
        r"name or service not known|NXDOMAIN",
        "Ein Name lässt sich nicht auflösen (DNS-Problem).",
        "DNS-Server und den Eintrag prüfen (z. B. mit „dig“).",
    ),
    _u(
        "port_belegt",
        r"address already in use|bind\(\) to .* failed|EADDRINUSE",
        "Der Port ist schon belegt – ein anderer Prozess hört bereits darauf.",
        "Mit „ss -tlnp“ nachsehen, wer den Port belegt.",
    ),
    _u(
        "rechte",
        r"permission denied|EACCES|operation not permitted|is not writable|"
        r"cannot (create|write|open) .*denied",
        "Ein Rechteproblem: Der Dienst darf eine Datei oder einen Ordner nicht lesen oder "
        "schreiben.",
        "Besitzer und Rechte des genannten Pfads prüfen (z. B. www-data bei Nextcloud).",
    ),
    _u(
        "trusted_domain",
        r"trusted[_ ]domain|Access through untrusted domain",
        "Nextcloud wird über eine Adresse aufgerufen, die nicht in „trusted_domains“ steht.",
        "Die Adresse in der config.php unter trusted_domains eintragen.",
    ),
    _u(
        "backend",
        r"502 Bad Gateway|dial tcp .*connect: connection refused|upstream.*(refused|timed out)|"
        r"no upstreams available|connect\(\) failed .* while connecting to upstream",
        "Der Reverse-Proxy erreicht den eigentlichen Dienst dahinter nicht.",
        "Prüfen, ob der Dienst hinter Caddy läuft und unter der eingetragenen Adresse lauscht.",
    ),
    _u(
        "verbindung",
        r"connection refused|connection timed out|no route to host|network is unreachable",
        "Eine Verbindung zu einem anderen Dienst kommt nicht zustande.",
        "Prüfen, ob der Zieldienst läuft und die Firewall den Weg erlaubt.",
    ),
)


def normalisieren(zeile: str) -> str:
    """Zeile ohne Zeitstempel, IDs und Zahlen – zum Zusammenfassen gleicher Meldungen."""
    ohne_zeit = ZEITSTEMPEL.sub("", zeile)
    return VARIABEL.sub("#", ohne_zeit).strip()[:300]


def relevante_zeilen(zeilen: list[str], maximal: int = 60) -> list[tuple[str, int]]:
    """Zeilen, die nach einem Problem aussehen: gleiche zusammengefasst, (letzte, Anzahl).

    Reihenfolge wie im Log (nach dem letzten Auftreten), höchstens ``maximal`` – die neuesten.
    """
    zaehler: Counter[str] = Counter()
    letzte: dict[str, tuple[int, str]] = {}
    for index, zeile in enumerate(zeilen):
        text = zeile.rstrip()
        if not text or not ist_relevant(text):
            continue
        schluessel = normalisieren(text)
        zaehler[schluessel] += 1
        letzte[schluessel] = (index, text)
    reihenfolge = sorted(letzte, key=lambda s: letzte[s][0])
    return [(letzte[s][1][:500], zaehler[s]) for s in reihenfolge][-maximal:]


def ist_relevant(zeile: str) -> bool:
    return bool(FEHLER_MUSTER.search(zeile) or any(u.muster.search(zeile) for u in URSACHEN))


def ursachen_erkennen(zeilen: list[str]) -> list[tuple[Ursache, int, str]]:
    """Erkannte Ursachen: (Ursache, wie oft, ein Beispiel) – wichtigste zuerst, höchstens 3."""
    funde: list[tuple[Ursache, int, str]] = []
    for ursache in URSACHEN:
        treffer = [z for z in zeilen if ursache.muster.search(z)]
        if treffer:
            funde.append((ursache, len(treffer), treffer[-1].strip()[:300]))
    # „verbindung“ ist allgemein: weglassen, wenn es eine genauere Erklärung gibt
    if len(funde) > 1:
        funde = [f for f in funde if f[0].name != "verbindung"]
    return funde[:3]


def in_stuecke(zeilen: list[str], max_zeichen: int = MAX_ZEICHEN) -> list[list[str]]:
    """Teilt Zeilen von hinten in Stücke (Stück 1 = die neuesten), jedes höchstens max_zeichen."""
    stuecke: list[list[str]] = []
    aktuell: list[str] = []
    groesse = 0
    for zeile in reversed(zeilen):
        zeile = zeile[:1000]
        if aktuell and groesse + len(zeile) + 1 > max_zeichen:
            stuecke.append(list(reversed(aktuell)))
            aktuell, groesse = [], 0
        aktuell.append(zeile)
        groesse += len(zeile) + 1
    if aktuell:
        stuecke.append(list(reversed(aktuell)))
    return stuecke or [[]]
