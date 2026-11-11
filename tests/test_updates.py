"""Update-Verwaltung: Updates pro VM, Wartungsfenster nach Ja, Einspielen und Neustart pro VM."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from missa.core.alarme import Alarme, AlarmStufe
from missa.core.rechte import Aktionsprotokoll, Rechte
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.integrationen.apt import apt_liste_lesen, neustart_lesen, upgrade_zusammenfassen
from missa.tools.kalender import Belegung
from missa.tools.updates import UpdateVerwaltung, update_werkzeuge, wochentage_lesen
from tests.ssh_fake import FakeHosts
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr

APT_WB01 = """Listing... Done
openssl/stable-security 3.0.15-1~deb12u1 amd64 [upgradable from: 3.0.14-1~deb12u2]
libssl3/stable-security 3.0.15-1~deb12u1 amd64 [upgradable from: 3.0.14-1~deb12u2]
curl/stable 7.88.1-10+deb12u8 amd64 [upgradable from: 7.88.1-10+deb12u7]
"""
APT_AI01 = (
    "Listing...\ntzdata/stable-updates 2026a-0+deb12u1 all [upgradable from: 2025b-0+deb12u1]\n"
)
UPGRADE = (
    "Reading package lists...\nThe following packages will be upgraded:\n  curl libssl3 openssl\n"
    "3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\nSetting up openssl ...\n"
)


class FakeKalender:
    """Kalender mit festen Belegungen; merkt sich eingetragene Termine."""

    def __init__(self) -> None:
        self.belegt: list[Belegung] = []
        self.eingetragen: list[tuple[str, str, str]] = []

    def belegen(self, start: datetime, ende: datetime, titel: str = "Termin") -> None:
        self.belegt.append(Belegung(start, ende, titel, "", "Persönlich", titel, False, False, ""))

    def belegungen(self, start: datetime, ende: datetime, kalender=None) -> list[Belegung]:
        return [b for b in self.belegt if b.ende > start and b.start < ende]

    def termin_eintragen(self, titel: str, start: str, ende: str | None = None, **_extra) -> str:
        self.eingetragen.append((titel, start, ende or ""))
        return f"Eingetragen: {titel} {start}–{ende}"


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))  # Mittwoch


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def planer(tmp_path: Path, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def alarme(planer) -> Alarme:
    return Alarme(planer.datei, ZONE, planer)


@pytest.fixture
def hosts() -> FakeHosts:
    fake = FakeHosts("ai01=127.0.0.1,wb01=10.0.20.10,int01=10.0.30.20")
    fake.antworten[("wb01", "apt-liste")] = APT_WB01
    fake.antworten[("ai01", "apt-liste")] = APT_AI01
    fake.antworten[("int01", "apt-liste")] = "Listing... Done\n"
    fake.antworten[("wb01", "neustart-noetig")] = "nein"
    fake.antworten[("int01", "neustart-noetig")] = "nein"
    fake.antworten[("ai01", "neustart-noetig")] = "nein"
    fake.antworten[("wb01", "apt-upgrade")] = UPGRADE
    fake.antworten[("ai01", "apt-upgrade")] = "1 upgraded, 0 newly installed, 0 to remove"
    return fake


@pytest.fixture
def kalender() -> FakeKalender:
    return FakeKalender()


@pytest.fixture
def verwaltung(hosts, alarme, planer, kalender, uhr) -> UpdateVerwaltung:
    return UpdateVerwaltung(
        hosts.verbindung(), alarme, planer.datei, ZONE, kalender=kalender, uhr=uhr
    )


@pytest.fixture
def kasten(verwaltung, tmp_path) -> Werkzeugkasten:
    return Werkzeugkasten(
        update_werkzeuge(verwaltung), Rechte(Aktionsprotokoll(tmp_path / "p.db", ZONE))
    )


def rufen(kasten: Werkzeugkasten, name: str, argumente: dict, anfrage: str = "", ja: bool = False):
    return asyncio.run(
        kasten.ausfuehren(name, argumente, anfrage, nachricht="Ja, mach" if ja else None)
    )


# ----------------------------------------------------------------------------- Lesen


def test_apt_liste_lesen():
    pakete = apt_liste_lesen(APT_WB01)
    assert [(p.name, p.sicherheit) for p in pakete] == [
        ("libssl3", True),
        ("openssl", True),
        ("curl", False),
    ]
    assert pakete[1].text() == "openssl (3.0.14-1~deb12u2 → 3.0.15-1~deb12u1)"
    ubuntu = apt_liste_lesen(
        "bash/jammy-updates,jammy-security 5.1-6ubuntu1.1 amd64 [upgradable from: 5.1-6ubuntu1]"
    )
    assert ubuntu[0].sicherheit
    assert neustart_lesen("ja\nlinux-image-amd64\nlibc6\n") == (
        True,
        ["libc6", "linux-image-amd64"],
    )
    assert neustart_lesen("nein") == (False, [])
    assert upgrade_zusammenfassen(UPGRADE).startswith("3 upgraded")


def test_wochentage_lesen():
    assert wochentage_lesen("Sa,So") == (5, 6)
    assert wochentage_lesen("") == tuple(range(7))
    with pytest.raises(ValueError):
        wochentage_lesen("Feiertag")


def test_welche_updates_stehen_an(kasten, hosts):
    antwort = rufen(kasten, "updates_anzeigen", {})
    assert "Anstehende Updates (4, davon 2 Sicherheitsupdates" in antwort
    assert "- wb01: 3 Update(s), davon 2 SICHERHEITSUPDATE(S) 🔒" in antwort
    assert "🔒 openssl (3.0.14-1~deb12u2 → 3.0.15-1~deb12u1)" in antwort
    assert "- int01: keine Updates" in antwort
    assert antwort.index("int01") < antwort.index("wb01") < antwort.index("- ai01")  # ai01 zuletzt
    assert ("wb01", []) in hosts.befehle("apt-update")  # vorher frische Paketlisten
    assert hosts.befehle("apt-upgrade") == []


def test_taeglich_sicherheitsupdates_einmal_melden(verwaltung):
    alarm = asyncio.run(verwaltung.taeglich_pruefen())
    assert alarm is not None and alarm.stufe is AlarmStufe.INFO
    assert "wb01: libssl3, openssl" in alarm.text
    assert asyncio.run(verwaltung.taeglich_pruefen()) is None  # dieselben Updates: nicht nochmal


# ----------------------------------------------------------------------------- Wartungsfenster


def test_wartungsfenster_erscheint_erst_nach_ja(kasten, kalender, planer, uhr):
    samstag = datetime(2026, 11, 14, 10, 0, tzinfo=ZONE)
    kalender.belegen(samstag, samstag + timedelta(hours=1, minutes=30), "Training")
    vorschlag = rufen(kasten, "wartungsfenster_vorschlagen", {})
    assert "Sa 14.11.2026 11:30–12:30 Uhr" in vorschlag
    assert "NOCH NICHTS EINGETRAGEN" in vorschlag and "beginn=2026-11-14T11:30" in vorschlag
    assert kalender.eingetragen == []

    angaben = {"beginn": "2026-11-14T11:30", "dauer_minuten": 60}
    rueckfrage = rufen(kasten, "wartungsfenster_eintragen", angaben, "a1")
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and "Wartung: Server-Updates" in rueckfrage
    assert kalender.eingetragen == []  # erst nach Ja

    antwort = rufen(kasten, "wartungsfenster_eintragen", angaben, "a2", ja=True)
    assert "gespeichert" in antwort
    assert kalender.eingetragen == [
        ("Wartung: Server-Updates", "2026-11-14T11:30", "2026-11-14T12:30")
    ]
    # zu Beginn kommt eine Erinnerung mit dem Plan
    erinnerung = [e for e in planer.offene() if e.herkunft == "wartung"]
    assert erinnerung and erinnerung[0].faellig == datetime(2026, 11, 14, 11, 30, tzinfo=ZONE)
    assert "ai01" in erinnerung[0].nachricht
    assert "Wartungsfenster Sa 14.11.2026 11:30–12:30 Uhr (geplant)" in rufen(
        kasten, "updates_plan", {}
    )


def test_konflikt_wird_in_der_rueckfrage_genannt(kasten, kalender):
    beginn = datetime(2026, 11, 15, 10, 0, tzinfo=ZONE)
    kalender.belegen(beginn, beginn + timedelta(hours=2), "Geburtstag Oma")
    rueckfrage = rufen(kasten, "wartungsfenster_eintragen", {"beginn": "2026-11-15T10:00"}, "a1")
    assert "Geburtstag Oma" in rueckfrage


def test_ohne_kalender(hosts, alarme, planer, uhr):
    verwaltung = UpdateVerwaltung(hosts.verbindung(), alarme, planer.datei, ZONE, uhr=uhr)
    kasten = Werkzeugkasten(update_werkzeuge(verwaltung))
    assert "keinen Kalender" in rufen(kasten, "wartungsfenster_vorschlagen", {})


# ----------------------------------------------------------------------------- Einspielen


def test_updates_und_neustart_nur_nach_ja_pro_vm(kasten, hosts):
    rufen(kasten, "updates_anzeigen", {})
    rueckfrage = rufen(kasten, "updates_einspielen", {"host": "wb01"}, "a1")
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and "apt-get -y upgrade" in rueckfrage
    assert hosts.befehle("apt-upgrade") == []

    # Ein Ja für wb01 gilt nicht für int01 oder ai01.
    andere = rufen(kasten, "updates_einspielen", {"host": "ai01"}, "a2", ja=True)
    assert hosts.befehle("apt-upgrade") == []
    assert "ai01 kommt zuletzt" in andere and "wb01" in andere

    hosts.antworten[("wb01", "apt-liste")] = "Listing...\n"
    hosts.antworten[("wb01", "neustart-noetig")] = "ja\nlinux-image-amd64"
    antwort = rufen(kasten, "updates_einspielen", {"host": "wb01"}, "a3", ja=True)
    assert hosts.befehle("apt-upgrade") == [("wb01", [])]
    assert "Updates auf wb01 eingespielt: 3 upgraded" in antwort
    assert "NEUSTART" in antwort and "linux-image-amd64" in antwort
    assert hosts.befehle("neustart") == []  # Neustart braucht ein eigenes Ja

    neustart = rufen(kasten, "host_neustarten", {"host": "wb01"}, "a4")
    assert neustart.startswith("NICHT AUSGEFÜHRT") and hosts.befehle("neustart") == []
    rufen(kasten, "host_neustarten", {"host": "wb01"}, "a5", ja=True)
    assert hosts.befehle("neustart") == [("wb01", [])]


def test_eigener_host_zuletzt_mit_warnung(kasten, hosts):
    rufen(kasten, "updates_anzeigen", {})
    rufen(kasten, "updates_einspielen", {"host": "wb01"}, "a1")
    hosts.antworten[("wb01", "apt-liste")] = "Listing...\n"
    rufen(kasten, "updates_einspielen", {"host": "wb01"}, "a2", ja=True)
    rueckfrage = rufen(kasten, "updates_einspielen", {"host": "ai01"}, "a3")
    assert "⚠️ Das ist die VM, auf der ich selbst laufe" in rueckfrage
    rufen(kasten, "updates_einspielen", {"host": "ai01"}, "a4", ja=True)
    assert hosts.befehle("apt-upgrade") == [("wb01", []), ("ai01", [])]
    plan = rufen(kasten, "updates_plan", {})
    assert "2. wb01: ✅ eingespielt" in plan and "3. ai01: ✅ eingespielt ⚠️ zuletzt" in plan
    neustart = rufen(kasten, "host_neustarten", {"host": "ai01"}, "a5")
    assert "ich bin dann ein paar Minuten weg" in neustart


def test_eigener_host_vorher_nur_wenn_andere_ausgelassen(kasten, hosts):
    rufen(kasten, "updates_anzeigen", {})
    angaben = {"host": "ai01", "andere_uebersprungen": True}
    assert rufen(kasten, "updates_einspielen", angaben, "a1").startswith("NICHT AUSGEFÜHRT")
    rufen(kasten, "updates_einspielen", angaben, "a2", ja=True)
    assert hosts.befehle("apt-upgrade") == [("ai01", [])]


def test_nichts_zu_tun_und_unbekannter_host(kasten):
    rufen(kasten, "updates_anzeigen", {})
    assert "keine Updates an" in rufen(kasten, "updates_einspielen", {"host": "int01"}, "a1")
    assert "kenne ich nicht" in rufen(kasten, "updates_einspielen", {"host": "pve-id01"}, "a2")


def test_im_hintergrund_nie(kasten, hosts):
    from missa.core.werkzeuge import im_hintergrund

    async def nachts():
        marke = im_hintergrund.set(True)
        try:
            return await kasten.ausfuehren("updates_einspielen", {"host": "wb01"}, "n1")
        finally:
            im_hintergrund.reset(marke)

    assert "im Hintergrund" in asyncio.run(nachts())
    assert hosts.befehle("apt-upgrade") == []


def test_wieder_da_nach_eigenem_neustart(verwaltung, hosts, planer, uhr, alarme):
    asyncio.run(verwaltung.neustarten("ai01"))
    uhr.vor(minutes=3)
    neu = UpdateVerwaltung(hosts.verbindung(), alarme, planer.datei, ZONE, uhr=uhr)
    neu.nach_start()
    assert any("Ich bin wieder da" in (e.nachricht or "") for e in planer.offene())
    neu.nach_start()  # nur einmal
    assert sum("Ich bin wieder da" in (e.nachricht or "") for e in planer.offene()) == 1


def test_nicht_erreichbare_vm(kasten, hosts):
    hosts.offline.add("int01")
    antwort = rufen(kasten, "updates_anzeigen", {})
    assert "- int01: nicht prüfbar – Keine SSH-Verbindung zu int01" in antwort


def test_app_api_und_werkzeuge(einstellungen, llama, hosts):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from tests.conftest import API_KEY

    app = create_app(einstellungen, llama_transport=llama.transport)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        assert client.get("/api/updates").json() == {
            "ssh": False,
            "hosts": [],
            "wartungsfenster": None,
        }

    app = create_app(
        einstellungen, llama_transport=llama.transport, ssh_verbindung=hosts.verbindung()
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        antwort = client.get("/api/updates").json()
        assert antwort["ssh"] is True and antwort["reihenfolge"] == ["int01", "wb01", "ai01"]
        werkzeuge = set(client.get("/health").json()["werkzeuge"])
        assert {
            "updates_anzeigen",
            "wartungsfenster_vorschlagen",
            "wartungsfenster_eintragen",
            "updates_einspielen",
            "host_neustarten",
            "updates_plan",
        } <= werkzeuge
