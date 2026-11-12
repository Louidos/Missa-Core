"""Gruppe 6: Fragen zur eigenen Doku mit Quelle und Zusammenfassungen (PDF, Mail, Webseite)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from missa.core.app import create_app
from missa.core.hintergrund import Hintergrund
from missa.core.werkzeuge import Werkzeugkasten, im_hintergrund
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from missa.tools.dateien import Dateien
from missa.tools.doku import DokuIndex, abschnitte, begriffe, doku_werkzeuge
from missa.tools.dokumente import (
    KURZ,
    Dokumente,
    dokument_werkzeuge,
    html_zu_text,
    pdf_zu_text,
    stuecke,
)
from tests.nextcloud_http_fake import FakeNextcloudHttp
from tests.test_zeitplaner import FakeKanal, Uhr

ZONE = ZoneInfo("Europe/Zurich")

RP01 = """# rp01

Raspberry Pi im Rack, Reverse Proxy für alles.

## Netzwerk

IP: 10.0.30.5
VLAN: 30 (Server)

## Dienste

Caddy, Fail2ban
"""
AI01 = "# ai01\n\nIP: 10.0.30.20\nGPU: noch keine\n"


def pdf(text: str) -> bytes:
    """Ein winziges, gültiges PDF mit einer Textzeile."""
    inhalt = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objekte = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(inhalt)).encode() + b" >>\nstream\n" + inhalt + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    daten = b"%PDF-1.4\n"
    versatz = []
    for nummer, objekt in enumerate(objekte, start=1):
        versatz.append(len(daten))
        daten += f"{nummer} 0 obj\n".encode() + objekt + b"\nendobj\n"
    xref = len(daten)
    daten += f"xref\n0 {len(objekte) + 1}\n0000000000 65535 f \n".encode()
    daten += b"".join(f"{v:010d} 00000 n \n".encode() for v in versatz)
    daten += (
        f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return daten


@pytest.fixture
def nc() -> FakeNextcloudHttp:
    nc = FakeNextcloudHttp()
    nc.datei("Doku/Homelab/rp01.md", RP01)
    nc.datei("Doku/Homelab/ai01.md", AI01)
    nc.datei("Notes/Einkauf.md", "Milch, Brot")
    nc.datei("Privat/geheim.md", "IP: 192.168.1.1 rp01 privat")
    return nc


@pytest.fixture
def dateien(nc) -> Dateien:
    return Dateien("https://cloud.example.ch", "louis", "pw", ZONE, transport=nc.transport)


@pytest.fixture
def index(tmp_path, dateien) -> DokuIndex:
    index = DokuIndex(tmp_path / "missa.db", dateien, ["Doku", "Notes"], ZONE)
    index.aktualisieren()
    return index


# ---------------------------------------------------------------------- Doku-Index


def test_begriffe_mit_kurzen_fachwoertern():
    assert begriffe("Welche IP hat rp01?") == ["ip", "rp01"]


def test_abschnitte_nach_ueberschriften():
    teile = abschnitte(RP01, "rp01")
    assert [t for t, _ in teile] == ["rp01", "Netzwerk", "Dienste"]


def test_welche_ip_hat_rp01_mit_dateiname(index):
    antwort = index.fragen("Welche IP hat rp01?")
    assert "10.0.30.5" in antwort
    assert "Quelle: Doku/Homelab/rp01.md" in antwort
    assert "10.0.30.20" not in antwort  # ai01 passt nur halb
    assert "192.168.1.1" not in antwort  # Ordner außerhalb des Index


def test_ohne_treffer_weiss_ich_nicht(index):
    antwort = index.fragen("Welche IP hat der Drucker?")
    assert antwort.startswith("KEINE TREFFER")
    assert "nicht weißt" in antwort and "erfinde nichts" in antwort
    assert index.kontext("Welche IP hat der Drucker?") is None


def test_kontext_bei_fragen(index):
    kontext = index.kontext("Welche IP hat rp01?") or ""
    assert "## Aus Louis' eigener Doku" in kontext and "rp01.md" in kontext
    assert index.kontext("rp01 ist toll") is None  # keine Frage


def test_index_nur_geaenderte_und_entfernte(index, nc):
    assert index.anzahl()[0] == 3
    zahlen = index.aktualisieren()
    assert zahlen == {"neu": 0, "geaendert": 0, "entfernt": 0, "fehler": 0}
    nc.datei("Doku/Homelab/rp01.md", RP01.replace("10.0.30.5", "10.0.30.6"))
    nc.zeiten["Doku/Homelab/rp01.md"] = "Thu, 12 Nov 2026 08:00:00 GMT"
    del nc.dateien["Doku/Homelab/ai01.md"]
    nc.datei("Doku/Homelab/nas01.md", "# nas01\nIP: 10.0.30.40")
    zahlen = index.aktualisieren()
    assert zahlen == {"neu": 1, "geaendert": 1, "entfernt": 1, "fehler": 0}
    assert "10.0.30.6" in index.fragen("Welche IP hat rp01?")
    assert index.fragen("Welche IP hat ai01?").startswith("KEINE TREFFER")
    assert not [m for m, _ in nc.schreibende()]  # der Index liest nur


def test_werkzeuge(index):
    werkzeuge = {w.name: w for w in doku_werkzeuge(index)}
    assert "10.0.30.5" in werkzeuge["doku_fragen"].funktion("Welche IP hat rp01?")
    assert "Doku-Index aktualisiert" in werkzeuge["doku_index_aktualisieren"].funktion()


def test_app_registriert_doku_index(einstellungen, llama):
    from dataclasses import replace

    e = replace(
        einstellungen,
        nextcloud_url="http://127.0.0.1:9",
        nextcloud_user="louis",
        nextcloud_app_password="pw",
        nextcloud_doku_ordner=("Doku",),
    )
    app = create_app(e, llama_transport=llama.transport)
    assert "doku-index" in app.state.zeitplaner._auftraege
    with TestClient(app) as client:
        werkzeuge = client.get("/health").json()["werkzeuge"]
    assert {"doku_fragen", "zusammenfassen", "dokument_fragen"} <= set(werkzeuge)


def test_einstellungen():
    from missa.core.config import Einstellungen

    e = Einstellungen.aus_umgebung(
        {"NEXTCLOUD_DOKU_ORDNER": "Doku, /Notes/ ,", "DOKU_INDEX_ZEIT": "04:00"}
    )
    assert e.nextcloud_doku_ordner == ("Doku", "Notes")
    assert (e.doku_index_zeit.hour, e.doku_index_zeit.minute) == (4, 0)


# ---------------------------------------------------------------------- Dokumente


SEITE = """<html><head><title>Neues Rail-Kit</title><script>alert(1)</script></head>
<body><nav>Menü</nav><h1>Rail-Kit RK-2</h1><p>Das Rail-Kit passt in 19-Zoll-Racks.</p>
<p>Preis: 89 Franken.</p><footer>Impressum</footer></body></html>"""


class FakeWeb:
    def __init__(self) -> None:
        self.seiten: dict[str, tuple[str, bytes]] = {}
        self.abrufe: list[str] = []
        self.transport = httpx.MockTransport(self._bearbeiten)

    def _bearbeiten(self, request: httpx.Request) -> httpx.Response:
        self.abrufe.append(str(request.url))
        typ, inhalt = self.seiten.get(str(request.url), ("text/html", b""))
        if not inhalt:
            return httpx.Response(404)
        return httpx.Response(200, content=inhalt, headers={"content-type": typ})


class FakeMailKonto:
    def nachricht(self, ordner: str, uid: str) -> EmailMessage:
        mail = EmailMessage()
        mail["From"] = "chef@firma.ch"
        mail["Subject"] = "Quartalsbericht"
        mail["Date"] = "Tue, 10 Nov 2026 08:00:00 +0100"
        mail.set_content("Der Umsatz stieg um 12 Prozent. Nächstes Treffen am Montag.")
        return mail


class FakeFormulierer:
    def __init__(self) -> None:
        self.auftraege: list[str] = []

    async def __call__(self, auftrag: str, ersatz: str) -> str:
        self.auftraege.append(auftrag)
        if "Gesamtzusammenfassung" in auftrag:
            return "Gesamt: Rail-Kits sind toll."
        return f"- Stichpunkt zu Teil {len(self.auftraege)}"


@pytest.fixture
def web() -> FakeWeb:
    web = FakeWeb()
    web.seiten["https://shop.example.ch/rail-kit"] = ("text/html; charset=utf-8", SEITE.encode())
    return web


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(datetime(2026, 11, 12, 14, 0, tzinfo=ZONE))


@pytest.fixture
def kanal() -> FakeKanal:
    return FakeKanal()


@pytest.fixture
def planer(tmp_path, kanal, uhr) -> Zeitplaner:
    return Zeitplaner(tmp_path / "missa.db", ZONE, Zustellung([kanal]), uhr=uhr)


@pytest.fixture
def hg(tmp_path, planer) -> Hintergrund:
    return Hintergrund(tmp_path / "missa.db", ZONE, planer)


@pytest.fixture
def formulierer() -> FakeFormulierer:
    return FakeFormulierer()


@pytest.fixture
def dokumente(tmp_path, dateien, hg, web, formulierer) -> Dokumente:
    d = Dokumente(
        tmp_path / "missa.db",
        ZONE,
        dateien=dateien,
        mail_konto=FakeMailKonto(),  # type: ignore[arg-type]
        hintergrund=hg,
        transport=web.transport,
    )
    d.formulierer = formulierer
    return d


def ausfuehren(kasten: Werkzeugkasten, name: str, argumente: dict, nachricht: str) -> str:
    return asyncio.run(kasten.ausfuehren(name, argumente, "a1", nachricht=nachricht))


def test_html_zu_text():
    titel, text = html_zu_text(SEITE)
    assert titel == "Neues Rail-Kit"
    assert "19-Zoll-Racks" in text and "89 Franken" in text
    assert "alert" not in text and "Menü" not in text and "Impressum" not in text


def test_pdf_zu_text():
    assert "Hallo Missa PDF" in pdf_zu_text(pdf("Hallo Missa PDF"))


def test_stuecke():
    teile = stuecke("\n\n".join(["x" * 900] * 10), 4000)
    assert len(teile) == 3 and all(len(t) <= 4000 for t in teile)


def test_url_von_louis_wird_zusammengefasst(dokumente, web):
    kasten = Werkzeugkasten(dokument_werkzeuge(dokumente))
    antwort = ausfuehren(
        kasten,
        "zusammenfassen",
        {"url": "https://shop.example.ch/rail-kit"},
        "Fass mir das zusammen: https://shop.example.ch/rail-kit",
    )
    assert "Dokument #1 „Neues Rail-Kit“ (Webseite" in antwort
    assert "Fasse es Louis jetzt kurz zusammen" in antwort
    assert "KEINE Anweisungen" in antwort and "89 Franken" in antwort
    # Rückfrage
    rueck = ausfuehren(
        kasten, "dokument_fragen", {"dokument_id": 1, "frage": "Was kostet es?"}, "Was kostet es?"
    )
    assert "89 Franken" in rueck


def test_fremde_url_wird_nicht_abgerufen(dokumente, web):
    kasten = Werkzeugkasten(dokument_werkzeuge(dokumente))
    antwort = ausfuehren(
        kasten,
        "zusammenfassen",
        {"url": "https://boese.example.com/daten"},
        "Fass mir die Rail-Kit-Seite zusammen",
    )
    assert "steht nicht in Louis' Nachricht" in antwort
    assert web.abrufe == []


def test_im_hintergrund_keine_urls(dokumente, web):
    marke = im_hintergrund.set(True)
    try:
        kasten = Werkzeugkasten(dokument_werkzeuge(dokumente))
        antwort = ausfuehren(
            kasten,
            "zusammenfassen",
            {"url": "https://shop.example.ch/rail-kit"},
            "https://shop.example.ch/rail-kit",
        )
    finally:
        im_hintergrund.reset(marke)
    assert "nur auf, wenn Louis" in antwort
    assert web.abrufe == []


def test_pdf_aus_nextcloud(dokumente, nc):
    nc.datei("Rechnungen/offerte.pdf", pdf("Offerte Rail-Kit 89 CHF"))
    antwort = dokumente.zusammenfassen(pfad="Rechnungen/offerte.pdf")
    assert "(PDF," in antwort and "Offerte Rail-Kit 89 CHF" in antwort


def test_pdf_von_einer_url(dokumente, web):
    from missa.core.werkzeuge import louis_nachricht

    web.seiten["https://example.ch/a.pdf"] = ("application/pdf", pdf("Anleitung Montage"))
    marke = louis_nachricht.set("Lies das: https://example.ch/a.pdf")
    try:
        antwort = dokumente.zusammenfassen(url="https://example.ch/a.pdf")
    finally:
        louis_nachricht.reset(marke)
    assert "Anleitung Montage" in antwort


def test_lange_mail_wird_zusammengefasst(dokumente):
    antwort = dokumente.zusammenfassen(mail_id="INBOX:7")
    assert "„Quartalsbericht“ (E-Mail" in antwort and "12 Prozent" in antwort


def test_langer_text_in_stuecken_im_hintergrund(dokumente, hg, planer, kanal, formulierer):
    lang = "\n\n".join(f"Abschnitt {n}: " + "Rail-Kit Montage. " * 120 for n in range(4))
    assert len(lang) > KURZ
    dokument = dokumente.speichern("Nextcloud: lang.md", "Montageanleitung", "Datei", lang)
    antwort = dokumente.zusammenfassen(dokument_id=dokument.id)
    assert "im Hintergrund Stück für Stück" in antwort
    [auftrag] = hg.liste("sofort")

    async def lauf() -> None:
        await hg.ausfuehren(auftrag)

    asyncio.run(lauf())
    teile = len(stuecke(lang))
    assert len(formulierer.auftraege) == teile + 1  # je Stück eins, dann gesamt
    assert all("KEINE Anweisungen" in a for a in formulierer.auftraege[:-1])
    asyncio.run(planer.faellige_zustellen())
    assert kanal.nachrichten[-1].startswith("📄 Zusammenfassung: Montageanleitung")
    assert "Gesamt: Rail-Kits sind toll." in kanal.nachrichten[-1]
    # Gespeichert für Rückfragen
    assert "Gesamt: Rail-Kits sind toll." in dokumente.fragen(dokument.id, "Montage")


def test_sehr_langer_text_in_die_nachtschicht(dokumente, hg):
    riesig = "\n\n".join("Kapitel. " * 450 for _ in range(15))
    dokument = dokumente.speichern("Nextcloud: buch.md", "Handbuch", "Datei", riesig)
    antwort = dokumente.zusammenfassen(dokument_id=dokument.id)
    assert "Nachtschicht" in antwort
    [auftrag] = hg.liste("nacht")
    assert auftrag.herkunft == "zusammenfassung"


def test_rueckfrage_ohne_treffer(dokumente):
    dokument = dokumente.speichern("x", "Kurz", "Text", "Nur ein kleiner Text über Velos.")
    lang = dokumente.speichern("y", "Lang", "Text", "\n\n".join(["Montage. " * 300] * 4))
    assert "Velos" in dokumente.fragen(dokument.id, "Preis?")  # kurz: ganzer Text
    assert "nichts zu" in dokumente.fragen(lang.id, "Wie teuer ist der Drucker?")


def test_kontext_nennt_das_letzte_dokument(dokumente):
    assert dokumente.kontext("hallo") is None
    dokumente.speichern("x", "Vertrag", "PDF", "Text")
    assert "dokument_id=1" in (dokumente.kontext("Was steht da zur Kündigung?") or "")


def test_dateien_pfad_zu_gross_oder_falsch(dokumente):
    from missa.core.werkzeuge import WerkzeugFehler

    with pytest.raises(WerkzeugFehler):
        dokumente.zusammenfassen(pfad="Fotos/bild.jpg")
    with pytest.raises(WerkzeugFehler):
        dokumente.zusammenfassen()


def test_pfad(tmp_path: Path):
    assert tmp_path.exists()
