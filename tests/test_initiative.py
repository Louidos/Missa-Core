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


# ----------------------------------------------------------------------------- Budget


def melden_lassen(umgebung: Umgebung, nummer: int):
    umgebung.inhalt["Termine"] = f"- Lage {nummer}"
    umgebung.modell.antworten = [json.dumps(punkt("melden", f"thema-{nummer}", f"Punkt {nummer}"))]
    umgebung.uhr.vor(minutes=15)
    return umgebung.lauf()


def test_nach_fuenf_meldungen_wird_die_sechste_nur_notiert(umgebung):
    for nummer in range(5):
        assert melden_lassen(umgebung, nummer).gemeldet
    sechste = melden_lassen(umgebung, 5)
    assert not sechste.gemeldet
    assert sechste.punkte[0]["ergebnis"] == "notiert (Tagesbudget aufgebraucht)"
    assert "Tagesbudget von 5" in sechste.grund
    assert len(umgebung.zustellen()) == 5
    assert umgebung.initiative.verbraucht() == 5
    assert [n["schluessel"] for n in umgebung.initiative.offene_notizen()] == ["thema-5"]


def test_server_alarm_kommt_auch_bei_aufgebrauchtem_budget(umgebung):
    umgebung.initiative.vorgaben.setzen("initiative.budget_pro_tag", "0")
    assert not melden_lassen(umgebung, 1).gemeldet
    umgebung.alarme.melden("wichtig", "ai01: CPU über 90 %", schluessel="regel:1", quelle="proxmox")
    nachrichten = umgebung.zustellen()
    assert nachrichten == ["⚠️ Wichtig: ai01: CPU über 90 %"]
    assert umgebung.initiative.verbraucht() == 0  # feste Regeln zählen nicht


def test_budget_zaehlt_pro_kalendertag(umgebung):
    umgebung.initiative.vorgaben.setzen("initiative.budget_pro_tag", "1")
    assert melden_lassen(umgebung, 1).gemeldet
    assert not melden_lassen(umgebung, 2).gemeldet
    umgebung.uhr.jetzt = datetime(2026, 11, 12, 9, 0, tzinfo=ZONE)
    assert melden_lassen(umgebung, 3).gemeldet


def test_stoer_mich_hoechstens_dreimal_gilt_nach_neustart(umgebung, tmp_path):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    kasten = Werkzeugkasten(initiative_werkzeuge(umgebung.initiative))
    antwort = asyncio.run(kasten.ausfuehren("initiative_einstellen", {"budget_pro_tag": 3}))
    assert "Heute gemeldet: 0 von 3" in antwort
    neu = Initiative(
        umgebung.datei,
        ZONE,
        umgebung.lagebild,
        umgebung.modell,  # type: ignore[arg-type]
        charakter_datei=tmp_path / "charakter.md",
        budget_pro_tag=5,
    )
    assert neu.budget() == 3


def test_denk_mal_eine_stunde_nicht_nach(umgebung):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    umgebung.initiative.einplanen(umgebung.planer)
    kasten = Werkzeugkasten(initiative_werkzeuge(umgebung.initiative))
    asyncio.run(kasten.ausfuehren("initiative_einstellen", {"pause_minuten": 60}))
    lauf = umgebung.lauf()
    assert lauf.uebersprungen and lauf.ursache.startswith("pausiert bis")
    umgebung.uhr.vor(minutes=61)
    assert not umgebung.lauf().uebersprungen
    asyncio.run(
        kasten.ausfuehren("initiative_einstellen", {"aktiv": False, "intervall_minuten": 30})
    )
    assert "initiative" not in umgebung.planer._intervalle
    asyncio.run(kasten.ausfuehren("initiative_einstellen", {"aktiv": True}))
    assert umgebung.planer._intervalle["initiative"].intervall.total_seconds() == 30 * 60


def test_einstellen_prueft_werte(umgebung):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    kasten = Werkzeugkasten(initiative_werkzeuge(umgebung.initiative))
    assert "Fehler" in asyncio.run(kasten.ausfuehren("initiative_einstellen", {}))
    antwort = asyncio.run(kasten.ausfuehren("initiative_einstellen", {"intervall_minuten": 2}))
    assert "Öfter als alle 5 Minuten" in antwort


def test_initiative_status(umgebung):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    melden_lassen(umgebung, 1)
    kasten = Werkzeugkasten(initiative_werkzeuge(umgebung.initiative))
    antwort = asyncio.run(kasten.ausfuehren("initiative_status", {}))
    assert "Denkschleife: an" in antwort
    assert "alle 15 Minuten" in antwort
    assert "Heute gemeldet: 1 von 5" in antwort
    assert "Letzter Lauf: 11.11. 14:15 Uhr – melden" in antwort


def test_am_abend_kommt_eine_sammelmeldung_mit_allen_notizen(umgebung):
    umgebung.initiative.einplanen(umgebung.planer)
    umgebung.initiative.notieren("oma", "Oma hat am Samstag Geburtstag")
    umgebung.initiative.notieren("rail-kit", "Rail-Kit ist noch nicht bestellt")
    umgebung.uhr.jetzt = datetime(2026, 11, 11, 19, 30, 5, tzinfo=ZONE)
    asyncio.run(umgebung.planer.auftraege_ausfuehren(warten=True))
    nachrichten = umgebung.zustellen()
    assert len(nachrichten) == 1
    assert nachrichten[0].startswith("ℹ️ Hinweis: 2 Notizen von heute")
    assert "Oma hat am Samstag" in nachrichten[0] and "Rail-Kit" in nachrichten[0]
    assert umgebung.initiative.offene_notizen() == []
    assert umgebung.initiative.verbraucht() == 0  # zählt nicht gegen das Budget
    # ohne offene Notizen keine Sammelmeldung
    assert umgebung.initiative.sammelmeldung() is None


# ----------------------------------------------------------------------------- Journal


def test_warum_hast_du_mir_das_geschrieben_nennt_den_grund(umgebung):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    kasten = Werkzeugkasten(initiative_werkzeuge(umgebung.initiative))
    leer = asyncio.run(kasten.ausfuehren("gedanken_anzeigen", {"frage": "warum"}))
    assert "nichts von selbst" in leer
    umgebung.modell.antworten = [
        json.dumps(
            {
                "aktion": "melden",
                "stufe": "wichtig",
                "schluessel": "zahnarzt",
                "titel": "Zahnarzt um 15 Uhr",
                "grund": "Der Weg dauert 40 Minuten",
            }
        )
    ]
    umgebung.lauf()
    umgebung.uhr.vor(minutes=15)
    umgebung.inhalt["Termine"] = "- anders"
    umgebung.lauf()  # danach ein Lauf ohne Meldung
    antwort = asyncio.run(kasten.ausfuehren("gedanken_anzeigen", {"frage": "warum"}))
    assert "11.11. um 14:00 Uhr" in antwort
    assert "Zahnarzt um 15 Uhr" in antwort and "Der Weg dauert 40 Minuten" in antwort


def test_was_hast_du_heute_gedacht(umgebung):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    umgebung.modell.antworten = [
        json.dumps({"aktion": "nichts", "grund": "Alles ruhig"}),
    ]
    umgebung.lauf()
    umgebung.uhr.vor(minutes=15)
    umgebung.lauf()  # unverändert → übersprungen
    kasten = Werkzeugkasten(initiative_werkzeuge(umgebung.initiative))
    antwort = asyncio.run(kasten.ausfuehren("gedanken_anzeigen", {"frage": "heute"}))
    assert "So habe ich heute nachgedacht (2 Läufe)" in antwort
    assert "14:00: nichts – Alles ruhig" in antwort
    assert "1× Lagebild unverändert" in antwort


def test_alte_eintraege_werden_aufgeraeumt(umgebung):
    umgebung.initiative.journal_tage = 30
    umgebung.lauf()  # 11.11. entschieden
    umgebung.uhr.vor(minutes=15)
    umgebung.lauf()  # 11.11. übersprungen
    umgebung.uhr.vor(days=3)
    umgebung.inhalt["Termine"] = "- neu"
    umgebung.lauf()  # 14.11. entschieden
    assert umgebung.initiative.aufraeumen() == 1  # übersprungener nach 2 Tagen weg
    assert [lauf.uebersprungen for lauf in umgebung.initiative.laeufe()] == [False, False]
    umgebung.uhr.vor(days=28)  # 12.12.: der Lauf vom 11.11. ist älter als 30 Tage
    assert umgebung.initiative.aufraeumen() == 1
    assert [f"{lauf.zeit:%d.%m.}" for lauf in umgebung.initiative.laeufe()] == ["14.11."]


def test_aufraeumen_laeuft_taeglich_im_zeitplaner(umgebung):
    umgebung.initiative.einplanen(umgebung.planer)
    assert "initiative-aufraeumen" in umgebung.planer._auftraege


def test_journal_api_liefert_laeufe(client, llama: FakeLlama):
    initiative = client.app.state.initiative
    initiative.vorgaben.setzen("initiative.aktiv", "ja")
    llama.antworten = [
        text(json.dumps({"aktion": "notieren", "schluessel": "x", "titel": "Etwas", "grund": "g"}))
    ]

    async def laufen():
        return await initiative.lauf()

    asyncio.run(laufen())
    daten = client.get("/api/initiative/journal", params={"tage": 2, "anzahl": 10}).json()
    assert daten["anzahl"] == 1
    lauf = daten["laeufe"][0]
    assert lauf["aktion"] == "notieren" and lauf["grund"] == "g"
    assert {"dauer_ms", "gemeldet", "uebersprungen", "punkte"} <= set(lauf)
    status = client.get("/api/initiative").json()
    assert status["aktiv"] is True and status["letzter_lauf"]["id"] == lauf["id"]
    assert status["budget_pro_tag"] == 5


# ----------------------------------------------------------------------------- Rückmeldungen

RAUM = "nextcloud-talk:raum-1"


def eingang(umgebung: Umgebung, text_: str, sitzung: str = RAUM) -> str | None:
    from types import SimpleNamespace

    vorbereitet = SimpleNamespace(neue_nachricht=text_, sitzung=sitzung, kanal="talk")
    return asyncio.run(umgebung.initiative.eingang(vorbereitet))


@pytest.fixture
def mit_talk(tmp_path: Path) -> Umgebung:
    return Umgebung(tmp_path, rueckmelde_sitzung=RAUM)


def gemeldet(umgebung: Umgebung, schluessel: str, titel: str, nummer: int = 0):
    umgebung.inhalt["Termine"] = f"- {schluessel} {nummer}"
    umgebung.modell.antworten = [json.dumps(punkt("melden", schluessel, titel))]
    lauf = umgebung.lauf()
    umgebung.zustellen()
    return lauf


@pytest.mark.parametrize(
    ("text_", "bewertung"),
    [
        ("war gut", "gut"),
        ("Danke, hilfreich!", "gut"),
        ("👍", "gut"),
        ("lass das", "schlecht"),
        ("Nerv nicht damit", "schlecht"),
        ("das war nicht hilfreich", "schlecht"),
        ("danke", None),
        ("ok", None),
        ("war gut, aber erzähl mir jetzt bitte noch ausführlich was morgen alles ansteht", None),
    ],
)
def test_rueckmeldung_erkennen(text_, bewertung):
    from missa.core.initiative import rueckmeldung_erkennen

    assert rueckmeldung_erkennen(text_) == bewertung


def test_nach_lass_das_14_tage_keine_meldung_zum_thema(mit_talk):
    gemeldet(mit_talk, "kalender-pausen", "Morgen keine Pause")
    hinweis = eingang(mit_talk, "lass das")
    assert hinweis and "14 Tage stumm" in hinweis
    assert mit_talk.initiative.stumm_bis("kalender-pausen") is not None
    for tag in range(1, 14):
        mit_talk.uhr.vor(days=1)
        lauf = gemeldet(mit_talk, "kalender-pausen", "Wieder keine Pause", tag)
        assert not lauf.gemeldet and "stumm" in lauf.punkte[0]["ergebnis"]
    assert len(mit_talk.kanal.nachrichten) == 1
    assert "stumm nach Rückmeldung" in mit_talk.initiative.laeufe(1)[0].grund
    mit_talk.uhr.vor(days=1, minutes=1)  # 14 Tage vorbei
    assert gemeldet(mit_talk, "kalender-pausen", "Pause fehlt", 99).gemeldet


def test_rueckmeldung_aendert_nie_feste_alarme(mit_talk):
    gemeldet(mit_talk, "server-ai01", "ai01 ist langsam")
    eingang(mit_talk, "nerv nicht damit")
    mit_talk.alarme.melden("wichtig", "ai01: CPU über 90 %", schluessel="server-ai01")
    assert mit_talk.zustellen()[-1] == "⚠️ Wichtig: ai01: CPU über 90 %"


def test_war_gut_wird_gelernt_und_steht_im_prompt(mit_talk):
    gemeldet(mit_talk, "zahnarzt", "Zahnarzt bald")
    assert "hilfreich" in eingang(mit_talk, "war gut")
    mit_talk.uhr.vor(hours=2)
    gemeldet(mit_talk, "disk-ai01", "Disk voll", 2)
    eingang(mit_talk, "lass das")
    mit_talk.uhr.vor(minutes=15)
    mit_talk.inhalt["Termine"] = "- neu"
    mit_talk.lauf()
    system = mit_talk.modell.anfragen[-1]["messages"][0]["content"]
    assert "gern gesehen: zahnarzt" in system
    assert "nicht erwünscht: disk-ai01" in system


def test_normales_danke_oder_anderes_gespraech_zaehlt_nicht(mit_talk):
    gemeldet(mit_talk, "zahnarzt", "Zahnarzt bald")
    assert eingang(mit_talk, "danke") is None
    assert eingang(mit_talk, "danke, hilfreich", sitzung="nextcloud-talk:anderer-raum") is None
    assert eingang(mit_talk, "lass das", sitzung="api-sitzung") is None
    assert mit_talk.initiative.gelerntes() == []
    mit_talk.uhr.vor(minutes=31)  # zu spät
    assert eingang(mit_talk, "war gut") is None
    assert mit_talk.initiative.gelerntes() == []


def test_ohne_eigene_meldung_keine_rueckmeldung(mit_talk):
    mit_talk.alarme.melden("info", "Fester Alarm", schluessel="regel:1", quelle="proxmox")
    mit_talk.zustellen()
    assert eingang(mit_talk, "lass das") is None


def test_was_hast_du_ueber_meine_rueckmeldungen_gelernt(mit_talk):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    kasten = Werkzeugkasten(initiative_werkzeuge(mit_talk.initiative))
    assert "noch nichts zurückgemeldet" in asyncio.run(
        kasten.ausfuehren("initiative_gelerntes_anzeigen", {})
    )
    gemeldet(mit_talk, "zahnarzt", "Zahnarzt bald")
    eingang(mit_talk, "Danke, hilfreich")
    mit_talk.uhr.vor(hours=1)
    gemeldet(mit_talk, "kalender-pausen", "Keine Pause", 2)
    eingang(mit_talk, "lass das")
    antwort = asyncio.run(kasten.ausfuehren("initiative_gelerntes_anzeigen", {}))
    assert "Gern gesehen:\n- zahnarzt („Danke, hilfreich“" in antwort
    assert "Nicht erwünscht:\n- kalender-pausen („lass das“" in antwort
    assert "bis 25.11. nicht mehr" in antwort


def test_zuruecksetzen_erst_nach_ja(mit_talk):
    from missa.core.werkzeuge import Werkzeugkasten
    from missa.tools.initiative import initiative_werkzeuge

    gemeldet(mit_talk, "kalender-pausen", "Keine Pause")
    eingang(mit_talk, "lass das")
    kasten = Werkzeugkasten(initiative_werkzeuge(mit_talk.initiative))

    def ausfuehren(anfrage: str, nachricht: str) -> str:
        return asyncio.run(
            kasten.ausfuehren(
                "initiative_gelerntes_zuruecksetzen",
                {},
                anfrage,
                nachricht=nachricht,
                kanal="talk",
                sitzung=RAUM,
            )
        )

    antwort = ausfuehren("a1", "Vergiss, was du gelernt hast")
    assert antwort.startswith("NICHT AUSGEFÜHRT") and "1 Themen" in antwort
    assert len(mit_talk.initiative.gelerntes()) == 1
    assert ausfuehren("a2", "Ja").startswith("Erledigt: 1 Rückmeldung")
    assert mit_talk.initiative.gelerntes() == []
    assert mit_talk.initiative.stumm_bis("kalender-pausen") is None


def test_app_wertet_rueckmeldungen_im_gespraech_aus(einstellungen):
    app = create_app(einstellungen, llama_transport=FakeLlama().transport)
    initiative = app.state.initiative
    assert initiative.eingang in app.state.dienst.eingangs_pruefer
    assert initiative.rueckmelde_sitzung is None  # ohne Talk zählt jede Sitzung
