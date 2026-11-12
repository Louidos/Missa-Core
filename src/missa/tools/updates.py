"""Update-Verwaltung: anstehende Updates pro VM, Wartungsfenster und Einspielen nach Ja.

- **Erfassen:** Jeden Tag (``UPDATES_PRUEFZEIT``) holt Missa auf jeder VM die
  Paketlisten (``apt-get update``) und liest die anstehenden Updates (``apt list
  --upgradable``); Sicherheitsupdates weist sie getrennt aus. Neue Sicherheitsupdates
  meldet sie einmal als ``info``.
- **Wartungsfenster:** Missa schlägt einen freien Termin im Kalender vor
  (``WARTUNG_TAGE``, ``WARTUNG_VON``–``WARTUNG_BIS``). Eingetragen wird er erst nach
  Louis' Ja; zu Beginn des Fensters erinnert sie ihn mit dem Plan.
- **Einspielen:** VM für VM, jede einzeln nach Ja (``apt-get -y upgrade``). Die VM, auf
  der Missa selbst läuft (``UPDATES_EIGENER_HOST``, Standard ai01), kommt zuletzt – mit
  Warnung. Ein Neustart braucht ein eigenes Ja.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from missa.core.alarme import Alarm, Alarme, AlarmStufe
from missa.core.datenbank import verbinden
from missa.core.werkzeuge import Stufe, Werkzeug, WerkzeugFehler
from missa.core.zeitangaben import wandzeit
from missa.integrationen.apt import (
    Paket,
    apt_liste_lesen,
    neustart_lesen,
    upgrade_zusammenfassen,
)
from missa.integrationen.ssh import SshFehler, SshVerbindung
from missa.tools.nextcloud import datum_kurz

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS updates_stand (
    host TEXT PRIMARY KEY,
    geprueft TEXT NOT NULL,
    pakete TEXT NOT NULL DEFAULT '[]',
    neustart INTEGER NOT NULL DEFAULT 0,
    neustart_pakete TEXT NOT NULL DEFAULT '[]',
    fehler TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS updates_verlauf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    zeit TEXT NOT NULL,
    aktion TEXT NOT NULL CHECK (aktion IN ('upgrade', 'neustart', 'zurueck')),
    ergebnis TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wartungsfenster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    beginn TEXT NOT NULL,
    ende TEXT NOT NULL,
    hosts TEXT NOT NULL,
    erstellt TEXT NOT NULL
);
"""

WOCHENTAGE = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
WOCHENTAG_TEXT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
#: Ein Stand gilt so lange als aktuell; danach prüft updates_anzeigen neu.
STAND_ALTER = timedelta(hours=26)
#: Zeitlimits (Sekunden) für die langsamen Befehle.
ZEIT_UPDATE = 300
ZEIT_UPGRADE = 1800
#: So weit voraus sucht Missa ein Wartungsfenster.
SUCHE_TAGE = 21


def _utc(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def wochentage_lesen(text: str) -> tuple[int, ...]:
    """``Sa,So`` → (5, 6). Leer = alle Tage."""
    tage = []
    for teil in (text or "").replace(";", ",").split(","):
        kurz = teil.strip().casefold()[:2]
        if not kurz:
            continue
        if kurz not in WOCHENTAGE:
            raise ValueError(f"Unbekannter Wochentag in WARTUNG_TAGE: {teil}")
        tage.append(WOCHENTAGE[kurz])
    return tuple(sorted(set(tage))) or tuple(range(7))


@dataclass
class UpdateStand:
    """Was auf einer VM ansteht."""

    host: str
    geprueft: datetime
    pakete: list[Paket] = field(default_factory=list)
    neustart: bool = False
    neustart_pakete: list[str] = field(default_factory=list)
    fehler: str = ""

    @property
    def sicherheit(self) -> list[Paket]:
        return [p for p in self.pakete if p.sicherheit]

    def zeile(self, ausfuehrlich: bool = True) -> str:
        if self.fehler:
            return f"- {self.host}: nicht prüfbar – {self.fehler}"
        if not self.pakete:
            text = f"- {self.host}: keine Updates"
        else:
            text = f"- {self.host}: {len(self.pakete)} Update(s)"
            if self.sicherheit:
                text += f", davon {len(self.sicherheit)} SICHERHEITSUPDATE(S) 🔒"
            if ausfuehrlich:
                namen = [("🔒 " if p.sicherheit else "") + p.text() for p in self.pakete[:15]]
                text += ": " + ", ".join(namen)
                if len(self.pakete) > 15:
                    text += f" … und {len(self.pakete) - 15} weitere"
        if self.neustart:
            text += " | Neustart nötig" + (
                f" ({', '.join(self.neustart_pakete[:5])})" if self.neustart_pakete else ""
            )
        return text

    def als_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "geprueft": self.geprueft.isoformat(timespec="seconds"),
            "anzahl": len(self.pakete),
            "sicherheit": len(self.sicherheit),
            "pakete": [p.als_dict() for p in self.pakete],
            "neustart": self.neustart,
            "neustart_pakete": self.neustart_pakete,
            "fehler": self.fehler,
        }


class UpdateVerwaltung:
    """Updates erfassen, Wartungsfenster planen und Updates nach Ja einspielen."""

    def __init__(
        self,
        ssh: SshVerbindung,
        alarme: Alarme,
        datei: Path,
        zone: ZoneInfo,
        *,
        hosts: Iterable[str] = (),
        eigener_host: str = "ai01",
        kalender: Any = None,
        wartung_tage: tuple[int, ...] = (5, 6),
        wartung_von: time = time(10, 0),
        wartung_bis: time = time(17, 0),
        wartung_dauer_minuten: int = 60,
        rechte: Any = None,
        uhr: Callable[[], datetime] | None = None,
    ) -> None:
        self.ssh = ssh
        self.alarme = alarme
        self.datei = datei
        self.zone = zone
        self.eigener_host = eigener_host.strip().casefold()
        gewaehlt = [h.casefold() for h in hosts] or ssh.namen
        # Missas eigene VM kommt immer zuletzt.
        self.hosts = sorted(
            (h for h in gewaehlt if h in ssh.hosts), key=lambda h: (h == self.eigener_host, h)
        )
        self.kalender = kalender
        self.wartung_tage = wartung_tage
        self.wartung_von = wartung_von
        self.wartung_bis = wartung_bis
        self.wartung_dauer = timedelta(minutes=max(15, wartung_dauer_minuten))
        self.rechte = rechte
        self._uhr = uhr or alarme.jetzt
        with verbinden(datei) as db:
            db.executescript(SCHEMA)

    def jetzt(self) -> datetime:
        return self._uhr().astimezone(self.zone)

    def _host(self, host: Any) -> str:
        name = self.ssh.host(host).name
        if name not in self.hosts:
            raise WerkzeugFehler(
                f"Für {name} verwalte ich keine Updates. Eingerichtet: {', '.join(self.hosts)}."
            )
        return name

    # -------------------------------------------------------------- Stand

    def stand(self, host: str) -> UpdateStand | None:
        with verbinden(self.datei) as db:
            zeile = db.execute("SELECT * FROM updates_stand WHERE host = ?", (host,)).fetchone()
        if zeile is None:
            return None
        return UpdateStand(
            host=zeile["host"],
            geprueft=datetime.fromisoformat(zeile["geprueft"]).astimezone(self.zone),
            pakete=[Paket(**p) for p in json.loads(zeile["pakete"])],
            neustart=bool(zeile["neustart"]),
            neustart_pakete=json.loads(zeile["neustart_pakete"]),
            fehler=zeile["fehler"],
        )

    def _speichern(self, stand: UpdateStand) -> None:
        pakete = [
            {"name": p.name, "neu": p.neu, "alt": p.alt, "quellen": p.quellen} for p in stand.pakete
        ]
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO updates_stand (host, geprueft, pakete, neustart, neustart_pakete, "
                "fehler) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (host) DO UPDATE SET "
                "geprueft = excluded.geprueft, pakete = excluded.pakete, "
                "neustart = excluded.neustart, neustart_pakete = excluded.neustart_pakete, "
                "fehler = excluded.fehler",
                (
                    stand.host,
                    _utc(stand.geprueft),
                    json.dumps(pakete, ensure_ascii=False),
                    int(stand.neustart),
                    json.dumps(stand.neustart_pakete, ensure_ascii=False),
                    stand.fehler,
                ),
            )

    async def pruefen(self, host: str, listen_holen: bool = True) -> UpdateStand:
        """Liest, was auf ``host`` ansteht (vorher frische Paketlisten mit apt-get update)."""
        jetzt = self.jetzt()
        try:
            if listen_holen:
                await self.ssh.befehl(host, "apt-update", timeout=ZEIT_UPDATE)
            pakete = apt_liste_lesen(await self.ssh.befehl(host, "apt-liste", timeout=60))
            neustart, gruende = neustart_lesen(await self.ssh.befehl(host, "neustart-noetig"))
            stand = UpdateStand(host, jetzt, pakete, neustart, gruende)
        except SshFehler as fehler:
            alt = self.stand(host)
            stand = UpdateStand(
                host,
                jetzt,
                alt.pakete if alt else [],
                alt.neustart if alt else False,
                alt.neustart_pakete if alt else [],
                str(fehler),
            )
        self._speichern(stand)
        return stand

    async def alle_pruefen(self) -> list[UpdateStand]:
        return list(await asyncio.gather(*(self.pruefen(h) for h in self.hosts)))

    async def taeglich_pruefen(self) -> Alarm | None:
        """Täglicher Auftrag: alle VMs prüfen und neue Sicherheitsupdates einmal melden."""
        staende = await self.alle_pruefen()
        if self.rechte is not None:
            self.rechte.protokollieren(
                werkzeug="updates_pruefen",
                stufe="lesen",
                parameter={"hosts": self.hosts},
                bestaetigt=False,
                status="ausgefuehrt",
                ergebnis="; ".join(s.zeile(False) for s in staende),
                kanal="hintergrund",
                account="missa",
            )
        mit_sicherheit = [s for s in staende if s.sicherheit and not s.fehler]
        if not mit_sicherheit:
            return None
        inhalt = sorted(f"{s.host}:{p.name}:{p.neu}" for s in mit_sicherheit for p in s.sicherheit)
        schluessel = (
            "updates:sicherheit:" + hashlib.sha256("|".join(inhalt).encode()).hexdigest()[:16]
        )
        if self.alarme.zuletzt(schluessel) is not None:
            return None  # genau diese Updates wurden schon gemeldet
        zeilen = [
            f"{s.host}: "
            + ", ".join(p.name for p in s.sicherheit[:8])
            + (f" … (+{len(s.sicherheit) - 8})" if len(s.sicherheit) > 8 else "")
            for s in mit_sicherheit
        ]
        return self.alarme.melden(
            AlarmStufe.INFO,
            f"Sicherheitsupdates stehen an ({sum(len(s.sicherheit) for s in mit_sicherheit)})",
            "\n".join(zeilen)
            + "\nSag „Plan ein Wartungsfenster“, dann schlage ich einen Termin vor.",
            schluessel=schluessel,
            quelle="updates",
            ruhefenster=timedelta(days=365),
        )

    async def anzeigen(self, host: str | None = None, neu_pruefen: bool = False) -> str:
        hosts = [self._host(host)] if host else self.hosts
        jetzt = self.jetzt()
        staende = []
        for name in hosts:
            stand = self.stand(name)
            if neu_pruefen or stand is None or jetzt - stand.geprueft > STAND_ALTER:
                stand = await self.pruefen(name)
            staende.append(stand)
        sicherheit = sum(len(s.sicherheit) for s in staende)
        zeilen = [
            f"Anstehende Updates ({sum(len(s.pakete) for s in staende)}, davon {sicherheit} "
            "Sicherheitsupdates; 🔒 = Sicherheitsupdate):"
        ]
        zeilen += [s.zeile() for s in staende]
        aeltester = min(s.geprueft for s in staende)
        zeilen.append(
            f"(Stand: {aeltester:%d.%m. %H:%M} Uhr. Eingespielt wird nur nach deinem Ja.)"
        )
        if fenster := self.naechstes_fenster():
            zeilen.append(f"Geplantes Wartungsfenster: {self._fenster_text(*fenster)}.")
        return "\n".join(zeilen)

    # -------------------------------------------------------------- Wartungsfenster

    def _fenster_text(self, beginn: datetime, ende: datetime) -> str:
        return f"{datum_kurz(beginn.date())} {beginn:%H:%M}–{ende:%H:%M} Uhr"

    def naechstes_fenster(self) -> tuple[datetime, datetime] | None:
        """Laufendes oder nächstes eingetragenes Wartungsfenster."""
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT beginn, ende FROM wartungsfenster WHERE ende > ? ORDER BY beginn LIMIT 1",
                (_utc(self.jetzt()),),
            ).fetchone()
        if zeile is None:
            return None
        return (
            datetime.fromisoformat(zeile["beginn"]).astimezone(self.zone),
            datetime.fromisoformat(zeile["ende"]).astimezone(self.zone),
        )

    def _kalender_noetig(self) -> Any:
        if self.kalender is None:
            raise WerkzeugFehler(
                "Ich habe keinen Kalender (NEXTCLOUD_KALENDER) – ein Wartungsfenster kann ich "
                "darum nicht vorschlagen oder eintragen."
            )
        return self.kalender

    def _dauer(self, dauer_minuten: Any) -> timedelta:
        if dauer_minuten in (None, ""):
            return self.wartung_dauer
        try:
            return timedelta(minutes=max(15, min(int(dauer_minuten), 8 * 60)))
        except (TypeError, ValueError) as fehler:
            raise WerkzeugFehler("dauer_minuten muss eine Zahl sein.") from fehler

    def fenster_vorschlagen(self, dauer_minuten: int | None = None, tag: str | None = None) -> str:
        """Sucht den nächsten freien Termin für ein Wartungsfenster (trägt nichts ein)."""
        from missa.tools.kalender import freie_luecke

        kalender = self._kalender_noetig()
        dauer = self._dauer(dauer_minuten)
        jetzt = self.jetzt()
        if tag:
            try:
                tage = [date.fromisoformat(str(tag).strip())]
            except ValueError as fehler:
                raise WerkzeugFehler("tag muss ein Datum wie 2026-11-14 sein.") from fehler
        else:
            tage = [
                jetzt.date() + timedelta(days=i)
                for i in range(SUCHE_TAGE)
                if (jetzt.date() + timedelta(days=i)).weekday() in self.wartung_tage
            ]
        if not tage:
            return "In den nächsten Wochen gibt es keinen passenden Wartungstag."
        start = wandzeit(tage[0], time(0), self.zone)
        belegt = kalender.belegungen(
            start, wandzeit(tage[-1], time(0), self.zone) + timedelta(days=1)
        )
        for tag_ in tage:
            anfang = max(
                wandzeit(tag_, self.wartung_von, self.zone), jetzt.replace(second=0, microsecond=0)
            )
            if rest := anfang.minute % 15:
                anfang += timedelta(minutes=15 - rest)
            beginn = freie_luecke(
                belegt, anfang, wandzeit(tag_, self.wartung_bis, self.zone), dauer
            )
            if beginn is None:
                continue
            ende = beginn + dauer
            reihenfolge = self._reihenfolge_text()
            return (
                f"Vorschlag für das Wartungsfenster: {self._fenster_text(beginn, ende)} – da ist "
                f"der Kalender frei.\nReihenfolge: {reihenfolge}. Jede VM spiele ich erst nach "
                "Louis' Ja ein, einen Neustart ebenfalls nur nach eigenem Ja.\n"
                "NOCH NICHTS EINGETRAGEN. Frag Louis, ob es passt. Erst wenn er zustimmt, rufe "
                f"wartungsfenster_eintragen mit beginn={beginn:%Y-%m-%dT%H:%M} und "
                f"dauer_minuten={int(dauer.total_seconds() // 60)} auf."
            )
        return (
            f"In den nächsten {len(tage)} passenden Tagen ({WOCHENTAG_TEXT[tage[0].weekday()]} …) "
            f"habe ich zwischen {self.wartung_von:%H:%M} und {self.wartung_bis:%H:%M} Uhr keine "
            f"freie Lücke von {int(dauer.total_seconds() // 60)} Minuten gefunden. Frag Louis "
            "nach einem anderen Tag."
        )

    def _reihenfolge_text(self) -> str:
        teile = []
        for host in self.hosts:
            stand = self.stand(host)
            info = ""
            if stand is not None and not stand.fehler:
                info = f" ({len(stand.pakete)} Updates)" if stand.pakete else " (nichts zu tun)"
            if host == self.eigener_host:
                info += " – zuletzt, ich bin dabei kurz weg"
            teile.append(host + info)
        return " → ".join(teile)

    def _fenster_zeiten(self, beginn: str, dauer_minuten: Any) -> tuple[datetime, datetime]:
        from missa.tools.nextcloud import zeitpunkt_lesen

        start = zeitpunkt_lesen(beginn, self.zone, "beginn")
        if not isinstance(start, datetime):
            raise WerkzeugFehler("beginn braucht Datum und Uhrzeit, z. B. 2026-11-14T10:00.")
        if start < self.jetzt() - timedelta(minutes=5):
            raise WerkzeugFehler("Das Wartungsfenster liegt in der Vergangenheit.")
        return start, start + self._dauer(dauer_minuten)

    def fenster_vorschau(self, beginn: str, dauer_minuten: int | None = None) -> str:
        kalender = self._kalender_noetig()
        start, ende = self._fenster_zeiten(beginn, dauer_minuten)
        text = (
            f"Im Kalender wird der Termin „Wartung: Server-Updates“ am "
            f"{self._fenster_text(start, ende)} eingetragen. "
            f"Reihenfolge: {self._reihenfolge_text()}. "
            "Zu Beginn erinnere ich dich; eingespielt wird jede VM erst nach deinem Ja."
        )
        konflikte = [
            b.titel
            for b in kalender.belegungen(start, ende)
            if not b.frei and not b.ganztaegig and b.start < ende and b.ende > start
        ]
        if konflikte:
            text += f" ACHTUNG: In der Zeit steht schon etwas im Kalender: {', '.join(konflikte)}."
        return text

    def fenster_eintragen(self, beginn: str, dauer_minuten: int | None = None) -> str:
        """Trägt das Wartungsfenster ein (zerstoererisch: läuft nur nach Louis' Ja)."""
        kalender = self._kalender_noetig()
        start, ende = self._fenster_zeiten(beginn, dauer_minuten)
        notiz = (
            "Missa spielt die Updates VM für VM ein – jede erst nach Louis' Ja. Reihenfolge: "
            + self._reihenfolge_text()
            + "."
        )
        ergebnis = kalender.termin_eintragen(
            "Wartung: Server-Updates",
            f"{start:%Y-%m-%dT%H:%M}",
            f"{ende:%Y-%m-%dT%H:%M}",
            notiz=notiz,
            # Louis hat dem Wartungsfenster samt Konflikt-Hinweis schon zugestimmt.
            bestaetigt=True,
        )
        with verbinden(self.datei) as db:
            cursor = db.execute(
                "INSERT INTO wartungsfenster (beginn, ende, hosts, erstellt) VALUES (?, ?, ?, ?)",
                (_utc(start), _utc(ende), json.dumps(self.hosts), _utc(self.jetzt())),
            )
            nummer = cursor.lastrowid
        self.alarme.zeitplaner.anlegen(
            "Wartungsfenster beginnt",
            start,
            nachricht=(
                f"🔧 Das Wartungsfenster beginnt jetzt (bis {ende:%H:%M} Uhr). Plan: "
                f"{self._reihenfolge_text()}. Sag z. B. „Spiel die Updates auf {self.hosts[0]} "
                "ein“ – ich frage vor jeder VM nach."
            )
            if self.hosts
            else "🔧 Das Wartungsfenster beginnt jetzt.",
            schluessel=f"wartung:{nummer}",
            herkunft="wartung",
        )
        return ergebnis + f"\nWartungsfenster {self._fenster_text(start, ende)} gespeichert."

    def plan(self) -> str:
        zeilen = ["Plan für die Updates (jede VM erst nach Louis' Ja, Neustart mit eigenem Ja):"]
        seit = self._fenster_beginn()
        for nummer, host in enumerate(self.hosts, start=1):
            stand = self.stand(host)
            erledigt = self._erledigt(host, seit)
            status = (
                f"{host}: ✅ eingespielt"
                if erledigt
                else (stand.zeile(False)[2:] if stand else f"{host}: noch nicht geprüft")
            )
            if host == self.eigener_host:
                status += " ⚠️ zuletzt – Missa ist dabei kurz weg"
            zeilen.append(f"{nummer}. {status}")
        if fenster := self.naechstes_fenster():
            zustand = "läuft gerade" if fenster[0] <= self.jetzt() else "geplant"
            zeilen.append(f"Wartungsfenster {self._fenster_text(*fenster)} ({zustand}).")
        else:
            zeilen.append("Es ist kein Wartungsfenster eingetragen.")
        return "\n".join(zeilen)

    # -------------------------------------------------------------- Einspielen

    def _fenster_beginn(self) -> datetime:
        """Ab wann Einspielen als „in diesem Durchgang erledigt“ zählt."""
        fenster = self.naechstes_fenster()
        if fenster is not None and fenster[0] <= self.jetzt():
            return fenster[0]
        return self.jetzt() - timedelta(hours=12)

    def _erledigt(self, host: str, seit: datetime) -> bool:
        with verbinden(self.datei) as db:
            zeile = db.execute(
                "SELECT 1 FROM updates_verlauf WHERE host = ? AND aktion = 'upgrade' AND zeit >= ?",
                (host, _utc(seit)),
            ).fetchone()
        return zeile is not None

    def _reihenfolge_pruefen(self, host: str, andere_uebersprungen: bool) -> None:
        if host != self.eigener_host or andere_uebersprungen:
            return
        seit = self._fenster_beginn()
        offen = []
        for anderer in self.hosts:
            if anderer == host:
                continue
            stand = self.stand(anderer)
            if stand is not None and stand.pakete and not self._erledigt(anderer, seit):
                offen.append(anderer)
        if offen:
            raise WerkzeugFehler(
                f"{host} kommt zuletzt, weil ich darauf laufe. Zuerst noch: {', '.join(offen)}. "
                "Will Louis die auslassen, rufe mit andere_uebersprungen=true auf."
            )

    def _verlauf(self, host: str, aktion: str, ergebnis: str) -> None:
        with verbinden(self.datei) as db:
            db.execute(
                "INSERT INTO updates_verlauf (host, zeit, aktion, ergebnis) VALUES (?, ?, ?, ?)",
                (host, _utc(self.jetzt()), aktion, ergebnis[:500]),
            )

    async def einspielen_vorschau(self, host: str, andere_uebersprungen: bool = False) -> str:
        name = self._host(host)
        self._reihenfolge_pruefen(name, andere_uebersprungen)
        stand = await self.pruefen(name)
        if stand.fehler:
            raise WerkzeugFehler(f"{name} ist gerade nicht erreichbar: {stand.fehler}")
        if not stand.pakete:
            raise WerkzeugFehler(
                f"Auf {name} stehen keine Updates an – es gibt nichts einzuspielen."
            )
        text = f"Auf {name} wird „apt-get -y upgrade“ ausgeführt: {stand.zeile()[2:]}."
        if name == self.eigener_host:
            text += (
                " ⚠️ Das ist die VM, auf der ich selbst laufe: Während der Updates kann ich kurz "
                "langsam oder weg sein."
            )
        fenster = self.naechstes_fenster()
        if fenster is None or not fenster[0] <= self.jetzt() <= fenster[1]:
            text += " Hinweis: Gerade läuft kein eingetragenes Wartungsfenster."
        return text

    async def einspielen(self, host: str, andere_uebersprungen: bool = False) -> str:
        """Spielt die Updates auf einer VM ein (zerstoererisch: nur nach Louis' Ja)."""
        name = self._host(host)
        self._reihenfolge_pruefen(name, andere_uebersprungen)
        ausgabe = await self.ssh.befehl(name, "apt-upgrade", timeout=ZEIT_UPGRADE)
        zusammenfassung = upgrade_zusammenfassen(ausgabe) or "fertig"
        self._verlauf(name, "upgrade", zusammenfassung)
        nachher = await self.pruefen(name, listen_holen=False)
        zeilen = [f"Updates auf {name} eingespielt: {zusammenfassung}."]
        if nachher.pakete:
            zeilen.append(f"Noch offen (z. B. zurückgehalten): {nachher.zeile()[2:]}")
        if nachher.neustart:
            zeilen.append(
                f"{name} braucht einen NEUSTART"
                + (
                    f" ({', '.join(nachher.neustart_pakete[:5])})"
                    if nachher.neustart_pakete
                    else ""
                )
                + ". Frag Louis, ob du neu starten sollst (host_neustarten) – das braucht ein "
                "eigenes Ja."
            )
        seit = self._fenster_beginn()
        weiter = [
            h
            for h in self.hosts
            if h != name and not self._erledigt(h, seit) and (s := self.stand(h)) and s.pakete
        ]
        if weiter:
            zeilen.append(f"Nächste VM laut Plan: {weiter[0]}.")
        return "\n".join(zeilen)

    async def neustart_vorschau(self, host: str) -> str:
        name = self._host(host)
        stand = self.stand(name)
        text = f"{name} wird neu gestartet (in etwa 1 Minute, Dauer meist 1–3 Minuten)."
        if stand is not None and not stand.neustart:
            text += " Laut letzter Prüfung ist ein Neustart gerade nicht nötig."
        if name == self.eigener_host:
            text += (
                " ⚠️ Das ist die VM, auf der ich laufe – ich bin dann ein paar Minuten weg und "
                "melde mich, sobald ich wieder da bin."
            )
        return text

    async def neustarten(self, host: str) -> str:
        """Startet eine VM neu (zerstoererisch: nur nach Louis' eigenem Ja)."""
        name = self._host(host)
        await self.ssh.befehl(name, "neustart", timeout=60)
        self._verlauf(name, "neustart", "in 1 Minute")
        if name == self.eigener_host:
            return (
                f"{name} startet in etwa 1 Minute neu. Ich bin dann kurz weg und melde mich, "
                "sobald ich wieder da bin."
            )
        return f"{name} startet in etwa 1 Minute neu."

    def nach_start(self) -> None:
        """Beim Start: War das ein Neustart, den Louis bestätigt hat? Dann kurz Bescheid geben."""
        grenze = _utc(self.jetzt() - timedelta(minutes=30))
        with verbinden(self.datei) as db:
            letzter = db.execute(
                "SELECT * FROM updates_verlauf WHERE host = ? "
                "AND aktion IN ('neustart', 'zurueck') ORDER BY id DESC LIMIT 1",
                (self.eigener_host,),
            ).fetchone()
        if letzter is None or letzter["aktion"] != "neustart" or letzter["zeit"] < grenze:
            return
        self._verlauf(self.eigener_host, "zurueck", "Missa wieder gestartet")
        self.alarme.zeitplaner.anlegen(
            "Wieder da nach dem Neustart",
            self.jetzt(),
            nachricht=f"✅ Ich bin wieder da – {self.eigener_host} ist neu gestartet.",
            schluessel=f"wieder-da:{letzter['id']}",
            herkunft="wartung",
        )


def update_werkzeuge(verwaltung: UpdateVerwaltung) -> list[Werkzeug]:
    host = {"type": "string", "description": "VM, z. B. wb01"}
    return [
        Werkzeug(
            "updates_anzeigen",
            "Zeigt die anstehenden Updates pro VM, Sicherheitsupdates markiert, und ob ein "
            "Neustart nötig ist. Für „Welche Updates stehen an?“.",
            {
                "type": "object",
                "properties": {
                    "host": host,
                    "neu_pruefen": {"type": "boolean", "description": "jetzt frisch prüfen"},
                },
            },
            verwaltung.anzeigen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "wartungsfenster_vorschlagen",
            "Sucht einen freien Termin im Kalender für ein Wartungsfenster (trägt nichts ein).",
            {
                "type": "object",
                "properties": {
                    "dauer_minuten": {"type": "integer", "description": "Standard 60"},
                    "tag": {"type": "string", "description": "optional ein Datum JJJJ-MM-TT"},
                },
            },
            verwaltung.fenster_vorschlagen,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "wartungsfenster_eintragen",
            "Trägt das vorgeschlagene Wartungsfenster in den Kalender ein – nur nach Louis' Ja.",
            {
                "type": "object",
                "properties": {
                    "beginn": {"type": "string", "description": "z. B. 2026-11-14T10:00"},
                    "dauer_minuten": {"type": "integer"},
                },
                "required": ["beginn"],
            },
            verwaltung.fenster_eintragen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.fenster_vorschau,
        ),
        Werkzeug(
            "updates_plan",
            "Zeigt die Reihenfolge der VMs für die Updates (die eigene VM zuletzt), was schon "
            "eingespielt ist und das Wartungsfenster.",
            {"type": "object", "properties": {}},
            verwaltung.plan,
            stufe=Stufe.LESEN,
        ),
        Werkzeug(
            "updates_einspielen",
            "Spielt die Updates auf genau einer VM ein (apt-get -y upgrade) – nur nach Louis' Ja, "
            "VM für VM, die eigene VM zuletzt.",
            {
                "type": "object",
                "properties": {
                    "host": host,
                    "andere_uebersprungen": {
                        "type": "boolean",
                        "description": "nur wenn Louis andere VMs auslassen will",
                    },
                },
                "required": ["host"],
            },
            verwaltung.einspielen,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.einspielen_vorschau,
        ),
        Werkzeug(
            "host_neustarten",
            "Startet eine VM neu (z. B. nach Updates) – nur nach Louis' eigenem Ja.",
            {"type": "object", "properties": {"host": host}, "required": ["host"]},
            verwaltung.neustarten,
            stufe=Stufe.ZERSTOERERISCH,
            vorschau=verwaltung.neustart_vorschau,
        ),
    ]
