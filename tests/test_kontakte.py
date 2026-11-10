"""Beziehungspflege: vCards, Geburtstage (inkl. 29. Februar), Abstand-Prüfung, CardDAV."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.tools.bestaetigung import Bestaetigungen
from missa.tools.kalender import Kalender
from missa.tools.kontakte import (
    Adressbuch,
    Geburtstag,
    Kontaktpflege,
    abstand_lesen,
    abstand_text,
    alter_am,
    feiertag_im_jahr,
    geburtstag_lesen,
    kontakt_werkzeuge,
    naechster_geburtstag,
    vcards_lesen,
)
from missa.tools.nextcloud import NextcloudVerbindung
from tests.caldav_fake import FakeDAV, FakeSammlung, ereignis
from tests.test_zeitplaner import FakeKanal, Uhr

ZONE = ZoneInfo("Europe/Zurich")

VCARDS = """BEGIN:VCARD
VERSION:3.0
UID:oma-1
FN:Maria Degiorgi
N:Degiorgi;Maria;;;
NICKNAME:Oma
item1.EMAIL;TYPE=INTERNET:Oma@Example.ch
TEL;TYPE=CELL:+41 79 000 00 00
BDAY;VALUE=date:1941-11-13
END:VCARD
BEGIN:VCARD
VERSION:4.0
UID:lea-29
FN:Lea Schalt
BDAY:20000229
NOTE:Langer Text der über mehrere Zeilen
  gefaltet ist
END:VCARD
BEGIN:VCARD
VERSION:3.0
UID:tom-x
FN:Tom Ohnejahr
BDAY;X-APPLE-OMIT-YEAR=1604:1604-12-24
EMAIL:tom@example.ch
END:VCARD
BEGIN:VCARD
VERSION:4.0
UID:ben-0
FN:Ben Ohne
BDAY:--0705
END:VCARD
"""


# ---------------------------------------------------------------------- vCard


def test_vcards_lesen():
    oma, lea, tom, ben = vcards_lesen(VCARDS)
    assert oma.name == "Maria Degiorgi"
    assert oma.spitznamen == ("Oma",)
    assert oma.emails == ("oma@example.ch",)
    assert oma.telefon == ("+41 79 000 00 00",)
    assert oma.geburtstag == Geburtstag(11, 13, 1941)
    assert lea.geburtstag == Geburtstag(2, 29, 2000)
    assert tom.geburtstag == Geburtstag(12, 24, None)
    assert ben.geburtstag == Geburtstag(7, 5, None)
    assert oma.passt("oma") and oma.passt("degiorgi") and not oma.passt("lea")


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        ("1950-02-28", Geburtstag(2, 28, 1950)),
        ("19500228T000000Z", Geburtstag(2, 28, 1950)),
        ("--02-29", Geburtstag(2, 29, None)),
        ("1604-03-01", Geburtstag(3, 1, None)),
        ("1950-02-30", None),
        ("irgendwann", None),
    ],
)
def test_geburtstag_lesen(wert, erwartet):
    assert geburtstag_lesen(wert) == erwartet


# ---------------------------------------------------------------------- Geburtstage berechnen


def test_29_februar_im_schaltjahr():
    assert feiertag_im_jahr(Geburtstag(2, 29, 2000), 2028) == date(2028, 2, 29)


def test_29_februar_ohne_schaltjahr_am_28():
    assert feiertag_im_jahr(Geburtstag(2, 29, 2000), 2027) == date(2027, 2, 28)
    assert naechster_geburtstag(Geburtstag(2, 29, 2000), date(2026, 11, 10)) == date(2027, 2, 28)
    assert naechster_geburtstag(Geburtstag(2, 29, 2000), date(2027, 3, 1)) == date(2028, 2, 29)


def test_naechster_geburtstag_heute_und_jahreswechsel():
    oma = Geburtstag(11, 13, 1941)
    assert naechster_geburtstag(oma, date(2026, 11, 13)) == date(2026, 11, 13)
    assert naechster_geburtstag(oma, date(2026, 11, 14)) == date(2027, 11, 13)
    assert naechster_geburtstag(Geburtstag(1, 2), date(2026, 12, 30)) == date(2027, 1, 2)


def test_alter():
    assert alter_am(Geburtstag(11, 13, 1941), date(2026, 11, 13)) == 85
    assert alter_am(Geburtstag(2, 29, 2000), date(2027, 2, 28)) == 27
    assert alter_am(Geburtstag(12, 24), date(2026, 12, 24)) is None


@pytest.mark.parametrize(
    ("text", "tage"),
    [
        ("3 Wochen", 21),
        ("mindestens alle 3 Wochen", 21),
        ("alle 14 Tage", 14),
        ("alle zwei Wochen", 14),
        ("monatlich", 30),
        ("2 Monate", 60),
        ("jede Woche", 7),
        (10, 10),
    ],
)
def test_abstand_lesen(text, tage):
    assert abstand_lesen(text) == tage


def test_abstand_text():
    assert abstand_text(21) == "alle 3 Wochen"
    assert abstand_text(30) == "jeden Monat"
    assert abstand_text(10) == "alle 10 Tage"
    with pytest.raises(WerkzeugFehler):
        abstand_lesen("ab und zu")


# ---------------------------------------------------------------------- CardDAV


def carddav_transport(anfragen: list[httpx.Request], vcards: str = VCARDS) -> httpx.MockTransport:
    def bearbeiten(anfrage: httpx.Request) -> httpx.Response:
        anfragen.append(anfrage)
        if anfrage.method == "PROPFIND":
            return httpx.Response(
                207,
                text="""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
 <d:response><d:href>/remote.php/dav/addressbooks/users/louis/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
 </d:response>
 <d:response><d:href>/remote.php/dav/addressbooks/users/louis/contacts/</d:href>
  <d:propstat><d:prop><d:displayname>Kontakte</d:displayname>
   <d:resourcetype><d:collection/><card:addressbook/></d:resourcetype></d:prop></d:propstat>
 </d:response>
</d:multistatus>""",
            )
        if anfrage.method == "REPORT":
            assert anfrage.url.path == "/remote.php/dav/addressbooks/users/louis/contacts/"
            return httpx.Response(
                207,
                text=f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
 <d:response><d:href>/x.vcf</d:href><d:propstat><d:prop>
  <card:address-data><![CDATA[{vcards}]]></card:address-data>
 </d:prop></d:propstat></d:response>
</d:multistatus>""",
            )
        return httpx.Response(405)

    return httpx.MockTransport(bearbeiten)


@pytest.fixture
def anfragen() -> list[httpx.Request]:
    return []


@pytest.fixture
def adressbuch(anfragen) -> Adressbuch:
    return Adressbuch(
        "https://cloud.example.ch", "louis", "pw", transport=carddav_transport(anfragen)
    )


def test_adressbuch_liest_kontakte(adressbuch, anfragen):
    kontakte = adressbuch.kontakte()
    assert [k.name for k in kontakte] == [
        "Maria Degiorgi",
        "Lea Schalt",
        "Tom Ohnejahr",
        "Ben Ohne",
    ]
    assert anfragen[0].url == "https://cloud.example.ch/remote.php/dav/addressbooks/users/louis/"
    assert anfragen[0].headers["Depth"] == "1"
    assert anfragen[0].headers["Authorization"].startswith("Basic ")
    adressbuch.kontakte()  # aus dem Zwischenspeicher
    assert len(anfragen) == 2


def test_adressbuch_mit_namen(anfragen):
    buch = Adressbuch(
        "https://cloud.example.ch", "louis", "pw", "Arbeit", transport=carddav_transport(anfragen)
    )
    with pytest.raises(WerkzeugFehler, match="Adressbuch „Arbeit“"):
        buch.kontakte()


def test_adressbuch_falsches_passwort():
    buch = Adressbuch(
        "https://cloud.example.ch",
        "louis",
        "falsch",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    with pytest.raises(WerkzeugFehler, match="App-Passwort"):
        buch.kontakte()


# ---------------------------------------------------------------------- Kontaktpflege


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 10, 8, 5, tzinfo=ZONE))  # Dienstag


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def planer(tmp_path, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def termine() -> FakeSammlung:
    return FakeSammlung("Persönlich")


@pytest.fixture
def kalender(termine) -> Kalender:
    verbindung = NextcloudVerbindung(
        "https://cloud.example.ch", "louis", "pw", client_fabrik=FakeDAV(termine)
    )
    return Kalender(verbindung, "Persönlich", ZONE, Bestaetigungen())


@pytest.fixture
def pflege(tmp_path, planer, adressbuch, kalender, uhr) -> Kontaktpflege:
    return Kontaktpflege(
        tmp_path / "missa.db",
        ZONE,
        planer,
        adressbuch=adressbuch,
        kalender=kalender,
        vorlauf_tage=3,
        pruefzeit=time(8, 0),
        uhr=uhr,
    )


def zustellen(planer: Zeitplaner) -> int:
    return asyncio.run(planer.faellige_zustellen())


def test_geburtstag_vorher_und_am_tag(pflege, planer, kanal, uhr):
    """Oma hat am Freitag, 13.11. Geburtstag: Erinnerung am 10.11. (3 Tage vorher) und am 13.11."""
    assert pflege.geburtstage_planen() == 2
    offene = {e.schluessel: e for e in planer.offene()}
    vorher = offene["geburtstag:uid:oma-1:2026-11-13:vorher"]
    am_tag = offene["geburtstag:uid:oma-1:2026-11-13:tag"]
    assert vorher.faellig == uhr.jetzt  # 8:00 ist schon vorbei → sofort
    assert am_tag.faellig == datetime(2026, 11, 13, 8, 0, tzinfo=ZONE)
    assert "In 3 Tagen (Freitag, 13.11.) hat Maria Degiorgi Geburtstag – 85 Jahre!" in (
        vorher.nachricht or ""
    )

    # Täglich erneut geplant – trotzdem nichts doppelt.
    assert pflege.geburtstage_planen() == 2
    assert len(planer.offene()) == 2

    zustellen(planer)
    assert len(kanal.nachrichten) == 1
    uhr.jetzt = datetime(2026, 11, 13, 8, 0, tzinfo=ZONE)
    pflege.geburtstage_planen()
    zustellen(planer)
    assert kanal.nachrichten[-1].startswith("🎉 Heute hat Maria Degiorgi Geburtstag (85)!")
    assert len(kanal.nachrichten) == 2


def test_geburtstag_zu_frueh_noch_nichts(pflege, planer, uhr):
    uhr.jetzt = datetime(2026, 11, 1, 8, 5, tzinfo=ZONE)
    assert pflege.geburtstage_planen() == 0
    assert planer.offene() == []


def test_geburtstag_am_29_februar_ohne_schaltjahr(pflege, planer, uhr):
    """Lea (29.2.) – 2027 ist kein Schaltjahr: Erinnerung am 25.2. und am 28.2."""
    uhr.jetzt = datetime(2027, 2, 25, 7, 0, tzinfo=ZONE)
    pflege.geburtstage_planen()
    offene = {e.schluessel: e for e in planer.offene()}
    vorher = offene["geburtstag:uid:lea-29:2027-02-28:vorher"]
    am_tag = offene["geburtstag:uid:lea-29:2027-02-28:tag"]
    assert vorher.faellig == datetime(2027, 2, 25, 8, 0, tzinfo=ZONE)
    assert am_tag.faellig == datetime(2027, 2, 28, 8, 0, tzinfo=ZONE)
    assert "27 Jahre" in (vorher.nachricht or "")


def test_geburtstag_am_29_februar_im_schaltjahr(pflege, planer, uhr):
    uhr.jetzt = datetime(2028, 2, 26, 9, 0, tzinfo=ZONE)
    pflege.geburtstage_planen()
    schluessel = {e.schluessel for e in planer.offene()}
    assert "geburtstag:uid:lea-29:2028-02-29:tag" in schluessel


def test_geburtstag_ohne_jahr_ohne_alter(pflege, planer, uhr):
    uhr.jetzt = datetime(2026, 12, 24, 9, 0, tzinfo=ZONE)
    pflege.geburtstage_planen()
    [heute] = [e for e in planer.offene() if "Tom" in e.text]
    assert heute.nachricht.startswith("🎉 Heute hat Tom Ohnejahr Geburtstag!")


def test_wichtig_und_gemeldet(pflege, uhr):
    text = pflege.wichtig_markieren("Oma", "mindestens alle 3 Wochen")
    assert "Oma ist jetzt ein wichtiger Kontakt: alle 3 Wochen" in text
    [eintrag] = pflege.alle()
    assert eintrag.schluessel == "uid:oma-1"
    assert eintrag.emails == ("oma@example.ch",)
    assert "noch kein Kontakt vermerkt" in pflege.wichtige_anzeigen()

    assert (
        pflege.gemeldet("Oma", art="Telefon")
        == "Vermerkt: Kontakt mit Oma am 10.11.2026 (Telefon)."
    )
    assert "zuletzt gemeldet heute (10.11.2026, Telefon)" in pflege.wichtige_anzeigen()
    pflege.gemeldet("Maria Degiorgi", "2026-11-01")  # älter: ändert nichts
    assert pflege.alle()[0].letzter_kontakt == date(2026, 11, 10)
    with pytest.raises(WerkzeugFehler, match="Zukunft"):
        pflege.gemeldet("Oma", "2026-12-01")
    assert "kein wichtiger Kontakt mehr" in pflege.nicht_mehr_wichtig("oma")


def test_kontakt_ohne_adressbuch_eintrag(pflege):
    pflege.wichtig_markieren("Onkel Fritz", "monatlich")
    [eintrag] = pflege.alle()
    assert eintrag.schluessel == "name:onkel fritz"


def test_abstand_ueberschritten_erinnert_mit_freiem_zeitpunkt(pflege, planer, kanal, uhr, termine):
    uhr.jetzt = datetime(2026, 10, 1, 9, 0, tzinfo=ZONE)
    pflege.wichtig_markieren("Oma", "3 Wochen")
    pflege.gemeldet("Oma", art="Telefon")
    # Heute (Dienstag) ist 17–18:30 belegt → freier Vorschlag 18:30
    uhr.jetzt = datetime(2026, 10, 27, 10, 0, tzinfo=ZONE)
    termine.hinzufuegen(
        ereignis(
            "probe",
            "Chorprobe",
            datetime(2026, 10, 27, 17, 0, tzinfo=ZONE),
            datetime(2026, 10, 27, 18, 30, tzinfo=ZONE),
        )
    )
    assert pflege.ueberfaellige()[0][1] == 26
    assert asyncio.run(pflege.abstaende_pruefen()) == 1
    zustellen(planer)
    [nachricht] = kanal.nachrichten
    assert "seit 26 Tagen nicht mehr bei Oma gemeldet" in nachricht
    assert "alle 3 Wochen" in nachricht
    assert "heute um 18:30 Uhr" in nachricht

    # Am nächsten Tag nicht gleich nochmal …
    uhr.jetzt += timedelta(days=1)
    assert asyncio.run(pflege.abstaende_pruefen()) == 0
    # … und nach dem Melden gar nicht mehr.
    pflege.gemeldet("Oma")
    uhr.jetzt += timedelta(days=10)
    assert pflege.ueberfaellige() == []


def test_nicht_ueberschritten_keine_erinnerung(pflege, uhr):
    pflege.wichtig_markieren("Oma", "3 Wochen")
    pflege.gemeldet("Oma")
    uhr.jetzt += timedelta(days=21)
    assert pflege.ueberfaellige() == []
    uhr.jetzt += timedelta(days=1)
    assert len(pflege.ueberfaellige()) == 1


def test_missa_formuliert_in_ihrem_charakter(pflege, planer, kanal, uhr):
    async def formulierer(auftrag: str, ersatz: str) -> str:
        assert "bei Oma zu melden" in auftrag
        return "Na, wann hast du Oma zuletzt angerufen? 📞"

    pflege.formulierer = formulierer
    pflege.wichtig_markieren("Oma", "1 Woche")
    uhr.jetzt += timedelta(days=8)
    asyncio.run(pflege.abstaende_pruefen())
    zustellen(planer)
    assert kanal.nachrichten == ["Na, wann hast du Oma zuletzt angerufen? 📞"]


def test_formulieren_fehlgeschlagen_nimmt_ersatztext(pflege, planer, kanal, uhr):
    async def kaputt(auftrag: str, ersatz: str) -> str:
        raise RuntimeError("llama weg")

    pflege.formulierer = kaputt
    pflege.wichtig_markieren("Oma", "1 Woche")
    uhr.jetzt += timedelta(days=8)
    asyncio.run(pflege.abstaende_pruefen())
    zustellen(planer)
    assert kanal.nachrichten[0].startswith("💌 Du hast dich seit 8 Tagen")


def test_letzter_kontakt_aus_mails(pflege, uhr):
    from tests.test_mail import FakeKonto, mail

    konto = FakeKonto()
    nachricht = mail("Oma <oma@example.ch>", "louis@example.ch", "Hallo", "Gruss")
    nachricht.replace_header("Date", "Sun, 08 Nov 2026 10:00:00 +0100")
    konto.ordner["INBOX"].append(nachricht)
    pflege.mail_konto = konto  # type: ignore[assignment]
    pflege.wichtig_markieren("Oma", "3 Wochen")
    pflege.mails_auswerten()
    eintrag = pflege.alle()[0]
    assert (eintrag.letzter_kontakt, eintrag.letzte_art) == (date(2026, 11, 8), "E-Mail")

    pflege.mail_gesendet(["OMA@example.ch"])
    assert pflege.alle()[0].letzter_kontakt == date(2026, 11, 10)


def test_adressquelle_fuer_mails(pflege):
    assert pflege.adressen("Oma") == {"oma@example.ch": "Maria Degiorgi"}


def test_werkzeuge(pflege):
    kasten = Werkzeugkasten(kontakt_werkzeuge(pflege))
    assert [w.name for w in kasten] == [
        "kontakte_suchen",
        "geburtstage_anzeigen",
        "kontakt_wichtig_markieren",
        "kontakt_nicht_mehr_wichtig",
        "kontakt_gemeldet",
        "wichtige_kontakte_anzeigen",
    ]
    ergebnis = asyncio.run(
        kasten.ausfuehren("kontakt_wichtig_markieren", {"name": "Oma", "abstand": "3 Wochen"})
    )
    assert "wichtiger Kontakt" in ergebnis
    assert "Maria Degiorgi" in asyncio.run(kasten.ausfuehren("kontakte_suchen", {"name": "oma"}))
    geburtstage = asyncio.run(kasten.ausfuehren("geburtstage_anzeigen", {"tage": 60}))
    assert "Freitag, 13. November 2026: Maria Degiorgi (wird 85)" in geburtstage
    assert "Tom Ohnejahr" in geburtstage and "(wird" not in geburtstage.split("Tom")[1]


# ---------------------------------------------------------------------- täglicher Auftrag


def test_taeglicher_auftrag_einmal_pro_tag_und_nachgeholt(planer, uhr):
    laeufe: list[datetime] = []

    async def auftrag() -> None:
        laeufe.append(uhr.jetzt)

    planer.taeglich("test", time(8, 0), auftrag)
    uhr.jetzt = datetime(2026, 11, 10, 7, 59, tzinfo=ZONE)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == []
    uhr.jetzt = datetime(2026, 11, 10, 8, 0, tzinfo=ZONE)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == ["test"]
    uhr.jetzt = datetime(2026, 11, 10, 12, 0, tzinfo=ZONE)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == []
    # Dienst war bis 11 Uhr am nächsten Tag aus → wird nachgeholt
    uhr.jetzt = datetime(2026, 11, 11, 11, 0, tzinfo=ZONE)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == ["test"]
    assert len(laeufe) == 2


def test_taeglicher_auftrag_ueberlebt_neustart(tmp_path, kanal, uhr):
    def neuer() -> Zeitplaner:
        planer = Zeitplaner(tmp_path / "x.db", ZONE, Zustellung([kanal]), uhr=uhr)
        planer.taeglich("test", time(8, 0), lambda: None)
        return planer

    assert asyncio.run(neuer().auftraege_ausfuehren(warten=True)) == ["test"]
    assert asyncio.run(neuer().auftraege_ausfuehren(warten=True)) == []


def test_fehlerhafter_auftrag_stoert_nicht(planer, uhr):
    def kaputt() -> None:
        raise RuntimeError("kaputt")

    planer.taeglich("kaputt", time(8, 0), kaputt)
    assert asyncio.run(planer.auftraege_ausfuehren(warten=True)) == ["kaputt"]


def test_taeglich_pruefen_ganz(pflege, planer, uhr):
    pflege.wichtig_markieren("Oma", "1 Woche")
    uhr.jetzt += timedelta(days=8)  # 18.11.: Omas Geburtstag ist vorbei, Abstand überschritten
    asyncio.run(pflege.taeglich_pruefen())
    assert [e.herkunft for e in planer.offene()] == ["kontaktpflege"]


def test_einstellungen_kontakte():
    from missa.core.config import Einstellungen, KonfigurationsFehler

    e = Einstellungen.aus_umgebung(
        {
            "NEXTCLOUD_ADRESSBUCH": "Kontakte",
            "KONTAKTE_GEBURTSTAG_VORLAUF": "5",
            "KONTAKTE_PRUEFZEIT": "07:30",
        }
    )
    assert (e.nextcloud_adressbuch, e.kontakte_geburtstag_vorlauf) == ("Kontakte", 5)
    assert e.kontakte_pruefzeit == time(7, 30)
    with pytest.raises(KonfigurationsFehler):
        Einstellungen.aus_umgebung({"KONTAKTE_PRUEFZEIT": "acht"})


def test_app_registriert_kontakte(einstellungen, llama, pflege):
    from fastapi.testclient import TestClient

    from missa.core.app import create_app
    from tests.conftest import API_KEY

    app = create_app(einstellungen, llama_transport=llama.transport, kontaktpflege=pflege)
    with TestClient(app) as client:
        werkzeuge = client.get("/health").json()["werkzeuge"]
        assert "kontakt_wichtig_markieren" in werkzeuge
        assert (
            client.get(
                "/api/erinnerungen", headers={"Authorization": f"Bearer {API_KEY}"}
            ).status_code
            == 200
        )
    assert "kontaktpflege" in app.state.zeitplaner._auftraege
    assert pflege.formulierer is not None


def test_pfad(tmp_path: Path):
    assert tmp_path.exists()
