"""Denkschleife (Missa Initiative): Läufe, Überspringen, strenge Prüfung, Sicherheit."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from missa.core.alarme import Alarme
from missa.core.app import create_app
from missa.core.initiative import Initiative, antwort_pruefen, schluessel_normalisieren
from missa.core.lagebild import Lagebild
from missa.core.llama import LlamaFehler
from missa.core.werkzeuge import im_hintergrund
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung
from tests.conftest import API_KEY, FakeLlama, text, werkzeug_aufruf
from tests.test_zeitplaner import ZONE, FakeKanal, Uhr


def entscheidung(**punkt: Any) -> str:
    return json.dumps(punkt, ensure_ascii=False)


class FakeModell:
    """Ersetzt den LlamaClient: Antworten der Reihe nach, merkt sich jede Anfrage."""

    def __init__(self) -> None:
        self.antworten: list[Any] = []
        self.anfragen: list[dict[str, Any]] = []
        self.gesund_wert = True
        self.im_hintergrund: list[bool] = []
        self.warten = 0.0

    async def gesund(self) -> bool:
        return self.gesund_wert

    async def chat(self, nutzlast: dict[str, Any]) -> dict[str, Any]:
        self.anfragen.append(nutzlast)
        self.im_hintergrund.append(im_hintergrund.get())
        if self.warten:
            await asyncio.sleep(self.warten)
        antwort = self.antworten.pop(0) if self.antworten else entscheidung(aktion="nichts")
        if isinstance(antwort, Exception):
            raise antwort
        nachricht = antwort if isinstance(antwort, dict) else text(antwort)
        return {"choices": [{"message": nachricht}]}


class Umgebung:
    def __init__(self, tmp_path: Path, **optionen: Any) -> None:
        self.uhr = Uhr(datetime(2026, 11, 11, 14, 0, tzinfo=ZONE))
        self.datei = tmp_path / "missa.db"
        self.modell = FakeModell()
        self.inhalt = {"Termine": "- 15:00 Zahnarzt"}
        self.lagebild = Lagebild(ZONE, self.uhr)
        self.lagebild.registrieren("Uhrzeit", lambda: f"{self.uhr():%H:%M}", im_fingerabdruck=False)
        self.lagebild.registrieren("Termine", lambda: self.inhalt["Termine"], fremd=True)
        self.kanal = FakeKanal()
        self.planer = Zeitplaner(self.datei, ZONE, Zustellung([self.kanal]), uhr=self.uhr)
        self.alarme = Alarme(self.datei, ZONE, self.planer)
        optionen.setdefault("alarme", self.alarme)
        self.beschaeftigt: str | None = None
        self.notfall = False
        charakter = tmp_path / "charakter.md"
        charakter.write_text("Du bist Missa, frech und herzlich.", encoding="utf-8")
        optionen.setdefault("aktiv", True)
        self.initiative = Initiative(
            self.datei,
            ZONE,
            self.lagebild,
            self.modell,  # type: ignore[arg-type]
            charakter_datei=charakter,
            beschaeftigt=lambda: self.beschaeftigt,
            notfall_aktiv=lambda: self.notfall,
            geheimnisse=("supergeheim123",),
            uhr=self.uhr,
            **optionen,
        )

    def lauf(self):
        return asyncio.run(self.initiative.lauf())

    def zustellen(self) -> list[str]:
        asyncio.run(self.planer.faellige_zustellen())
        return self.kanal.nachrichten


@pytest.fixture
def umgebung(tmp_path: Path) -> Umgebung:
    return Umgebung(tmp_path)


# ----------------------------------------------------------------------------- an/aus


def test_ohne_initiative_aktiv_kein_lauf_und_kein_modellaufruf(tmp_path):
    umgebung = Umgebung(tmp_path, aktiv=False)
    assert umgebung.lauf() is None
    assert umgebung.modell.anfragen == []
    assert umgebung.initiative.laeufe() == []
    planer = Zeitplaner(umgebung.datei, ZONE, Zustellung([FakeKanal()]), uhr=umgebung.uhr)
    umgebung.initiative.einplanen(planer)
    assert "initiative" not in planer._intervalle


def test_app_plant_die_denkschleife_nur_wenn_aktiv(einstellungen):
    from dataclasses import replace

    aus = create_app(einstellungen, llama_transport=FakeLlama().transport)
    assert "initiative" not in aus.state.zeitplaner._intervalle
    an = create_app(
        replace(einstellungen, initiative_aktiv=True, initiative_intervall_minuten=2),
        llama_transport=FakeLlama().transport,
    )
    auftrag = an.state.zeitplaner._intervalle["initiative"]
    assert auftrag.intervall.total_seconds() == 5 * 60  # mindestens 5 Minuten


def test_lauf_speichert_entscheidung(umgebung):
    umgebung.modell.antworten = [
        entscheidung(
            aktion="melden",
            stufe="wichtig",
            schluessel="Zahnarzt Termin",
            titel="Zahnarzt um 15 Uhr",
            text="Du musst in einer Stunde los!",
            grund="Termin bald, Weg dauert",
        )
    ]
    lauf = umgebung.lauf()
    assert lauf.aktion == "melden" and not lauf.uebersprungen
    assert lauf.punkte[0]["schluessel"] == "zahnarzt-termin"
    assert lauf.punkte[0]["stufe"] == "wichtig"
    assert "Termin bald" in lauf.grund
    assert "Zahnarzt" in lauf.lagebild
    # Charakter + Aufgabe + Lagebild im Systemprompt, keine Werkzeuge
    nutzlast = umgebung.modell.anfragen[0]
    system = nutzlast["messages"][0]["content"]
    assert "frech und herzlich" in system and "Lagebild" in system
    assert "<<<FREMDE_DATEN" in system
    assert "tools" not in nutzlast


def test_unveraendertes_lagebild_ruft_das_modell_nicht_auf(umgebung):
    umgebung.lauf()
    umgebung.uhr.vor(minutes=15)
    lauf = umgebung.lauf()
    assert len(umgebung.modell.anfragen) == 1
    assert lauf.uebersprungen and lauf.ursache == "Lagebild unverändert"
    umgebung.inhalt["Termine"] = "- 16:00 Coiffeur"
    umgebung.lauf()
    assert len(umgebung.modell.anfragen) == 2


def test_waehrend_chat_oder_hintergrund_wird_uebersprungen(umgebung):
    umgebung.beschaeftigt = "Chat-Anfrage läuft"
    lauf = umgebung.lauf()
    assert lauf.uebersprungen and lauf.ursache == "Chat-Anfrage läuft"
    assert umgebung.modell.anfragen == []


def test_waehrend_notfall_stopp_wird_uebersprungen(umgebung):
    umgebung.notfall = True
    lauf = umgebung.lauf()
    assert lauf.uebersprungen and lauf.ursache == "Notfall-Stopp aktiv"
    assert umgebung.modell.anfragen == []


def test_ungesunder_llama_server_wird_uebersprungen_und_spaeter_nachgeholt(umgebung):
    umgebung.modell.gesund_wert = False
    assert umgebung.lauf().ursache == "llama-server nicht gesund"
    umgebung.modell.gesund_wert = True
    umgebung.lauf()  # gleiches Lagebild, aber noch nie entschieden
    assert len(umgebung.modell.anfragen) == 1


def test_chat_anfrage_zaehlt_als_beschaeftigt(einstellungen):
    llama = FakeLlama()
    app = create_app(einstellungen, llama_transport=llama.transport)
    initiative, dienst = app.state.initiative, app.state.dienst
    gesehen = []

    def antwort(_nutzlast):
        gesehen.append((dienst.laufend, initiative.beschaeftigt()))
        return text("Hallo")

    llama.antworten = [antwort]
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert gesehen == [(1, "Chat-Anfrage läuft")]
    assert dienst.laufend == 0 and initiative.beschaeftigt() is None


# ----------------------------------------------------------------------------- Prüfung


@pytest.mark.parametrize(
    ("antwort", "vermerk"),
    [
        ("Ich glaube, alles ist gut.", "kein lesbares JSON"),
        ('{"aktion": "melden", "stufe": "wichtig", ', "kein lesbares JSON"),
        (entscheidung(aktion="anrufen", titel="x", schluessel="x"), "unbekannte Aktion"),
        (
            entscheidung(aktion="melden", stufe="notfall", titel="Server brennt", schluessel="s"),
            "notfall ist der Denkschleife nicht erlaubt",
        ),
        (entscheidung(aktion="melden", stufe="ganz-dringend", titel="x"), "unbekannte Stufe"),
    ],
)
def test_kaputte_antwort_oder_notfall_endet_als_nichts_mit_grund(umgebung, antwort, vermerk):
    umgebung.modell.antworten = [antwort]
    lauf = umgebung.lauf()
    assert lauf.aktion == "nichts" and lauf.punkte == []
    assert vermerk in lauf.grund
    gespeichert = umgebung.initiative.laeufe()[0]
    assert gespeichert.aktion == "nichts" and vermerk in gespeichert.grund


def test_mehrere_punkte_und_codeblock():
    antwort = antwort_pruefen(
        "```json\n"
        + json.dumps(
            {
                "punkte": [
                    {"aktion": "notieren", "titel": "Oma anrufen", "schluessel": "oma"},
                    {"aktion": "melden", "stufe": "info", "titel": "Disk 85 %", "text": "ai01"},
                    {"aktion": "melden", "stufe": "notfall", "titel": "x", "schluessel": "x"},
                ]
            }
        )
        + "\n```"
    )
    assert [p.aktion for p in antwort.punkte] == ["notieren", "melden"]
    assert antwort.punkte[1].schluessel == "disk-85"
    assert antwort.aktion == "melden"
    assert any("notfall" in v for v in antwort.vermerke)


def test_schluessel_normalisieren():
    assert schluessel_normalisieren("Kalender Pausen!") == "kalender-pausen"
    assert schluessel_normalisieren("Grösse über") == "groesse-ueber"


# ----------------------------------------------------------------------------- Sicherheit


def test_lauf_kann_kein_werkzeug_aufrufen_und_nichts_bestaetigen(einstellungen):
    from dataclasses import replace

    llama = FakeLlama()
    app = create_app(replace(einstellungen, initiative_aktiv=True), llama_transport=llama.transport)
    rechte, protokoll = app.state.rechte, app.state.protokoll
    assert "erinnerung_loeschen" in app.state.dienst.werkzeuge
    llama.antworten = [werkzeug_aufruf("erinnerung_loeschen", {"erinnerung_id": 1})]
    planer = app.state.zeitplaner
    erinnerung = planer.anlegen("Zahnarzt", planer.jetzt().replace(year=2030))

    async def laufen():
        return await app.state.initiative.lauf()

    lauf = asyncio.run(laufen())
    nutzlast = llama.anfragen[-1]
    assert "tools" not in nutzlast
    assert lauf.aktion == "nichts" and "kein lesbares JSON" in lauf.grund
    assert planer.holen(erinnerung.id) is not None  # nichts gelöscht
    assert protokoll.liste(None, None, 100, None, None) == []  # kein Werkzeug lief
    assert rechte._offen == {} and rechte._freigabe_in == {}  # nichts vorgemerkt/bestätigt


def test_lauf_laeuft_im_hintergrund(umgebung):
    umgebung.lauf()
    assert umgebung.modell.im_hintergrund == [True]
    assert im_hintergrund.get() is False


def test_texte_werden_geschwaerzt(umgebung):
    umgebung.inhalt["Termine"] = "WLAN passwort=supergeheim123 und Token: abc"
    umgebung.modell.antworten = [
        entscheidung(aktion="notieren", titel="Passwort supergeheim123 notiert", schluessel="wlan")
    ]
    umgebung.lauf()
    with sqlite3.connect(umgebung.datei) as db:
        zeilen = db.execute(
            "SELECT lagebild, entscheidung, antwort FROM initiative_laeufe"
        ).fetchall()
    assert zeilen and all("supergeheim123" not in feld for z in zeilen for feld in z)


def test_modellfehler_bricht_nicht_ab_und_naechster_lauf_versucht_es_neu(umgebung):
    umgebung.modell.antworten = [LlamaFehler("weg", 502, "modell_nicht_erreichbar")]
    lauf = umgebung.lauf()
    assert lauf.fehler and lauf.aktion == "nichts"
    assert "Modellaufruf fehlgeschlagen" in lauf.grund
    umgebung.uhr.vor(minutes=15)
    umgebung.lauf()
    assert len(umgebung.modell.anfragen) == 2  # trotz gleichem Lagebild neu versucht


def test_nie_zwei_laeufe_gleichzeitig(umgebung):
    umgebung.modell.warten = 0.2

    async def zwei():
        return await asyncio.gather(umgebung.initiative.lauf(), umgebung.initiative.lauf())

    ergebnisse = asyncio.run(zwei())
    assert sum(1 for e in ergebnisse if e is not None) == 1
    assert len(umgebung.modell.anfragen) == 1


# ----------------------------------------------------------------------------- Meldungen


def punkt(aktion: str, schluessel: str, titel: str, stufe: str = "info", text: str = "") -> dict:
    return {"aktion": aktion, "stufe": stufe, "schluessel": schluessel, "titel": titel,
            "text": text, "grund": "Test"}  # fmt: skip


def test_melden_kommt_als_eine_nachricht_auch_bei_drei_punkten(umgebung):
    umgebung.modell.antworten = [
        json.dumps(
            {
                "punkte": [
                    punkt("melden", "zahnarzt", "Zahnarzt um 15 Uhr", "wichtig", "Bald los!"),
                    punkt("melden", "disk-ai01", "Disk auf ai01 bei 85 %"),
                    punkt("melden", "oma", "Oma wartet auf einen Anruf"),
                ]
            }
        )
    ]
    lauf = umgebung.lauf()
    assert lauf.gemeldet and lauf.alarm_id is not None
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 1
    nachricht = nachrichten[0]
    assert nachricht.startswith("⚠️ Wichtig: 3 Dinge, die mir aufgefallen sind")
    for titel in ("Zahnarzt um 15 Uhr: Bald los!", "Disk auf ai01", "Oma wartet"):
        assert titel in nachricht
    alarm = umgebung.alarme.liste()[0]
    assert alarm.quelle == "initiative"
    assert [p["ergebnis"] for p in lauf.punkte] == ["gemeldet"] * 3


def test_einzelne_meldung_nennt_klar_worum_es_geht(umgebung):
    umgebung.modell.antworten = [
        json.dumps(punkt("melden", "zahnarzt", "Zahnarzt um 15 Uhr", text="Na los, Zähne!"))
    ]
    umgebung.lauf()
    assert umgebung.zustellen() == ["ℹ️ Hinweis: Zahnarzt um 15 Uhr\nNa los, Zähne!"]


def test_in_der_ruhezeit_wartet_die_meldung(umgebung):
    umgebung.uhr.jetzt = datetime(2026, 11, 11, 23, 30, tzinfo=ZONE)
    umgebung.modell.antworten = [json.dumps(punkt("melden", "backup", "Backup fehlt", "wichtig"))]
    lauf = umgebung.lauf()
    assert lauf.gemeldet and "Ruhezeit" in lauf.punkte[0]["ergebnis"]
    assert umgebung.zustellen() == []
    umgebung.uhr.jetzt = datetime(2026, 11, 12, 7, 0, 30, tzinfo=ZONE)
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 1 and "Backup fehlt" in nachrichten[0]


def test_gleiches_thema_nicht_noch_einmal_im_ruhefenster(umgebung):
    umgebung.modell.antworten = [
        json.dumps(punkt("melden", "disk-ai01", "Disk voll")),
        json.dumps(
            {
                "punkte": [
                    punkt("melden", "disk-ai01", "Disk immer noch voll"),
                    punkt("melden", "neu", "Etwas Neues"),
                ]
            }
        ),
    ]
    umgebung.lauf()
    umgebung.uhr.vor(minutes=20)
    umgebung.inhalt["Termine"] = "- anders"
    lauf = umgebung.lauf()
    assert "im Ruhefenster" in lauf.punkte[0]["ergebnis"]
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 2
    assert "Disk immer noch voll" not in nachrichten[1] and "Etwas Neues" in nachrichten[1]
    # nach dem Ruhefenster darf das Thema wieder kommen
    umgebung.uhr.vor(minutes=61)
    umgebung.inhalt["Termine"] = "- wieder anders"
    umgebung.modell.antworten = [json.dumps(punkt("melden", "disk-ai01", "Disk voll (Tag 2)"))]
    umgebung.lauf()
    assert "Disk voll (Tag 2)" in umgebung.zustellen()[-1]


def test_notieren_bleibt_still_und_wird_im_naechsten_gespraech_einmal_erwaehnt(umgebung):
    umgebung.modell.antworten = [
        json.dumps(punkt("notieren", "oma", "Oma", text="Oma hat bald Geburtstag"))
    ]
    lauf = umgebung.lauf()
    assert lauf.aktion == "notieren" and not lauf.gemeldet
    assert umgebung.zustellen() == []
    erstes = umgebung.initiative.kontext("Hallo")
    assert "- Oma hat bald Geburtstag" in erstes and "Erwähne" in erstes
    assert umgebung.initiative.kontext("Und sonst?") is None


def test_kontext_nicht_im_hintergrund(umgebung):
    umgebung.initiative.notieren("x", "Notiz")
    marke = im_hintergrund.set(True)
    try:
        assert umgebung.initiative.kontext("Routine") is None
    finally:
        im_hintergrund.reset(marke)
    assert umgebung.initiative.kontext("Hallo") is not None


def test_notiz_im_gespraech_genau_einmal(client, llama: FakeLlama):
    client.app.state.initiative.notieren("kalender-pausen", "Morgen hast du keine Pause.")
    for _ in range(2):
        client.post(
            "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Hallo"}]}
        )
    assert "Morgen hast du keine Pause." in llama.system(0)
    assert "Morgen hast du keine Pause." not in llama.system(1)
