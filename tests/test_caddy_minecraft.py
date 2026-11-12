"""Subdomains in Caddy nur nach Ja (mit Rücknahme bei Fehlern) und Minecraft per RCON."""

from __future__ import annotations

import asyncio
import struct
from datetime import datetime
from pathlib import Path

import pytest

from missa.core.config import Einstellungen, KonfigurationsFehler
from missa.core.rechte import Aktionsprotokoll, Rechte
from missa.core.werkzeuge import WerkzeugFehler, Werkzeugkasten
from missa.integrationen.monitoring import QuellenFehler
from missa.integrationen.rcon import RconClient, RconFehler, paket
from missa.tools import caddy_verwaltung_bauen, minecraft_bauen, werkzeuge_laden
from missa.tools.caddy import CaddyVerwaltung, caddy_werkzeuge, name_pruefen, ziel_pruefen
from missa.tools.minecraft import MinecraftVerwaltung, minecraft_werkzeuge
from tests.ssh_fake import FakeHosts
from tests.test_zeitplaner import ZONE

# ----------------------------------------------------------------------------- Caddy


class CaddyHost:
    """Missas Ordner auf rp01: Dateien liegen im Speicher, caddy validate ist steuerbar."""

    def __init__(self, hosts: FakeHosts) -> None:
        self.dateien: dict[str, str] = {}
        self.ungueltig = False
        self.neu_laden_kaputt = False
        hosts.antworten[("rp01", "caddy-liste")] = self.liste
        hosts.antworten[("rp01", "caddy-schreiben")] = self.schreiben
        hosts.antworten[("rp01", "caddy-loeschen")] = self.loeschen
        hosts.antworten[("rp01", "caddy-pruefen")] = self.pruefen
        hosts.antworten[("rp01", "caddy-neu-laden")] = self.neu_laden

    def liste(self, _argumente: list[str]) -> str:
        return "".join(f"### {n}\n{i}\n" for n, i in sorted(self.dateien.items()))

    def schreiben(self, argumente: list[str]) -> str:
        datei, inhalt = argumente
        self.dateien[datei] = inhalt
        return f"geschrieben: {datei}"

    def loeschen(self, argumente: list[str]) -> str:
        self.dateien.pop(argumente[0], None)
        return f"entfernt: {argumente[0]}"

    def pruefen(self, _argumente: list[str]) -> tuple[int, str, str]:
        if self.ungueltig:
            return 1, "", "Error: adapting config using caddyfile: unrecognized directive: oops"
        return 0, "Valid configuration", ""

    def neu_laden(self, _argumente: list[str]) -> tuple[int, str, str]:
        if self.neu_laden_kaputt:
            return 1, "", "Job for caddy.service failed"
        return 0, "", ""


async def kein_dns(_name: str, _typ: str) -> list[str]:
    return []


@pytest.fixture
def hosts() -> FakeHosts:
    return FakeHosts()


@pytest.fixture
def rp01(hosts: FakeHosts) -> CaddyHost:
    return CaddyHost(hosts)


@pytest.fixture
def caddy(hosts: FakeHosts, rp01: CaddyHost) -> CaddyVerwaltung:
    return CaddyVerwaltung(
        hosts.verbindung(),
        "rp01",
        "louis-degiorgi.ch",
        dns=kein_dns,
        uhr=lambda: datetime(2026, 11, 12, 10, 0, tzinfo=ZONE),
    )


def kasten_mit(werkzeuge, tmp_path: Path) -> tuple[Werkzeugkasten, Aktionsprotokoll]:
    protokoll = Aktionsprotokoll(tmp_path / "protokoll.db", ZONE)
    return Werkzeugkasten(werkzeuge, Rechte(protokoll)), protokoll


def test_vorschlag_zeigt_eintrag_und_schreibt_nichts(caddy, hosts, rp01):
    antwort = asyncio.run(caddy.vorschlagen("Spiel", "10.0.70.120:8080"))
    assert "spiel.louis-degiorgi.ch {\n\treverse_proxy 10.0.70.120:8080\n}" in antwort
    assert "missa-spiel.caddy" in antwort and "NOCH NICHT AKTIV" in antwort
    assert "DNS-EINTRAG NÖTIG: spiel.louis-degiorgi.ch" in antwort
    assert [b for _h, b, _a in hosts.aufrufe] == ["caddy-liste"]
    assert rp01.dateien == {}


def test_vorschlag_mit_dns_und_vorhandenem_eintrag(hosts, rp01):
    async def dns(name: str, typ: str) -> list[str]:
        return ["louis-degiorgi.ch."] if typ == "CNAME" else []

    rp01.dateien["missa-spiel.caddy"] = "spiel.louis-degiorgi.ch {\n\treverse_proxy alt:80\n}"
    caddy = CaddyVerwaltung(hosts.verbindung(), "rp01", "louis-degiorgi.ch", dns=dns)
    antwort = asyncio.run(caddy.vorschlagen("spiel", "10.0.70.120:8080"))
    assert "DNS: spiel.louis-degiorgi.ch zeigt schon auf louis-degiorgi.ch." in antwort
    assert "ACHTUNG: missa-spiel.caddy gibt es schon" in antwort


def test_dns_fehler_wird_ehrlich_gemeldet(hosts, rp01):
    async def dns(_name: str, _typ: str) -> list[str]:
        raise QuellenFehler("Zeitüberschreitung")

    caddy = CaddyVerwaltung(hosts.verbindung(), "rp01", "louis-degiorgi.ch", dns=dns)
    antwort = asyncio.run(caddy.vorschlagen("spiel", "10.0.70.120:8080"))
    assert "DNS konnte ich nicht prüfen (Zeitüberschreitung)" in antwort


def test_eintrag_erst_nach_ja_aktiv(caddy, hosts, rp01, tmp_path):
    kasten, protokoll = kasten_mit(caddy_werkzeuge(caddy), tmp_path)
    angaben = {"name": "spiel", "ziel": "10.0.70.120:8080"}

    rueckfrage = asyncio.run(kasten.ausfuehren("subdomain_aktivieren", angaben, "a1"))
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT")
    assert "spiel.louis-degiorgi.ch auf 10.0.70.120:8080" in rueckfrage
    assert rp01.dateien == {}
    assert hosts.befehle("caddy-neu-laden") == []

    antwort = asyncio.run(kasten.ausfuehren("subdomain_aktivieren", angaben, "a2", nachricht="Ja"))
    assert "spiel.louis-degiorgi.ch ist aktiv" in antwort
    assert "DNS-EINTRAG NÖTIG" in antwort
    assert rp01.dateien["missa-spiel.caddy"] == (
        "# Von Missa angelegt am 12.11.2026 – missa-spiel.caddy\n"
        "spiel.louis-degiorgi.ch {\n\treverse_proxy 10.0.70.120:8080\n}"
    )
    befehle = [b for _h, b, _a in hosts.aufrufe if b != "caddy-liste"]
    assert befehle == ["caddy-schreiben", "caddy-pruefen", "caddy-neu-laden"]
    status = [a.status for a in protokoll.liste() if a.werkzeug == "subdomain_aktivieren"]
    assert status[0] == "ausgefuehrt" and status[-1] == "wartet_auf_bestaetigung"


def test_ungueltiger_eintrag_wird_entfernt_caddy_laeuft_weiter(caddy, hosts, rp01, tmp_path):
    rp01.ungueltig = True
    kasten, _ = kasten_mit(caddy_werkzeuge(caddy), tmp_path)
    angaben = {"name": "spiel", "ziel": "10.0.70.120:8080"}
    asyncio.run(kasten.ausfuehren("subdomain_aktivieren", angaben, "a1"))
    antwort = asyncio.run(kasten.ausfuehren("subdomain_aktivieren", angaben, "a2", nachricht="Ja"))
    assert "abgelehnt" in antwort and "unrecognized directive" in antwort
    assert "wieder entfernt" in antwort and "unverändert" in antwort
    assert rp01.dateien == {}
    # Nie neu geladen: Caddy läuft mit der alten Konfiguration weiter.
    assert hosts.befehle("caddy-neu-laden") == []
    assert hosts.befehle("caddy-loeschen") == [("rp01", ["missa-spiel.caddy"])]


def test_ungueltiger_ersatz_stellt_alten_eintrag_wieder_her(caddy, rp01):
    alt = "spiel.louis-degiorgi.ch {\n\treverse_proxy 10.0.70.110:80\n}"
    rp01.dateien["missa-spiel.caddy"] = alt
    rp01.ungueltig = True
    with pytest.raises(WerkzeugFehler, match="zurückgesetzt"):
        asyncio.run(caddy.aktivieren("spiel", "10.0.70.120:8080"))
    assert rp01.dateien == {"missa-spiel.caddy": alt}


def test_neu_laden_fehlgeschlagen_nimmt_eintrag_zurueck(caddy, rp01):
    rp01.neu_laden_kaputt = True
    with pytest.raises(WerkzeugFehler, match="nicht neu laden"):
        asyncio.run(caddy.aktivieren("spiel", "10.0.70.120:8080"))
    assert rp01.dateien == {}


def test_entfernen_und_anzeigen(caddy, hosts, rp01, tmp_path):
    asyncio.run(caddy.aktivieren("spiel", "10.0.70.120:8080"))
    asyncio.run(caddy.aktivieren("wiki", "http://10.0.20.15:3000"))
    anzeige = asyncio.run(caddy.anzeigen())
    assert "- spiel.louis-degiorgi.ch → 10.0.70.120:8080" in anzeige
    assert "- wiki.louis-degiorgi.ch → http://10.0.20.15:3000" in anzeige

    kasten, _ = kasten_mit(caddy_werkzeuge(caddy), tmp_path)
    rueckfrage = asyncio.run(kasten.ausfuehren("subdomain_entfernen", {"name": "wiki"}, "a1"))
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT") and "missa-wiki.caddy" in rueckfrage
    asyncio.run(kasten.ausfuehren("subdomain_entfernen", {"name": "wiki"}, "a2", nachricht="Ja"))
    assert list(rp01.dateien) == ["missa-spiel.caddy"]
    fremd = asyncio.run(kasten.ausfuehren("subdomain_entfernen", {"name": "gibtsnicht"}, "a3"))
    assert "gibt es nicht" in fremd


def test_keine_eintraege(caddy):
    assert "noch keine eigenen Subdomains" in asyncio.run(caddy.anzeigen())


@pytest.mark.parametrize("name", ["", "-spiel", "spiel-", "sp iel", "spiel.ch", "a" * 41, "ä"])
def test_ungueltige_namen(name):
    with pytest.raises(WerkzeugFehler):
        name_pruefen(name)


@pytest.mark.parametrize(
    "ziel",
    [
        "",
        "10.0.70.120",
        "10.0.70.120:0",
        "10.0.70.120:70000",
        "10.0.70.120:80 { }",
        "10.0.70.120:80\nimport /etc/passwd",
        "ftp://10.0.70.120:21",
        "host;rm:80",
    ],
)
def test_ungueltige_ziele(ziel):
    with pytest.raises(WerkzeugFehler):
        ziel_pruefen(ziel)


def test_gueltige_ziele():
    assert ziel_pruefen("http://mc01.lan:8080") == "http://mc01.lan:8080"
    assert ziel_pruefen("[fd00::5]:443") == "[fd00::5]:443"


def test_caddy_braucht_host_domain_und_ssh(tmp_path):
    basis = {"MISSA_API_KEY": "k", "MISSA_DATEN": str(tmp_path)}
    hosts = FakeHosts()
    ohne = Einstellungen.aus_umgebung(basis)
    assert caddy_verwaltung_bauen(ohne, hosts.verbindung()) is None
    mit = Einstellungen.aus_umgebung(
        {**basis, "CADDY_HOST": "rp01", "CADDY_DOMAIN": "Louis-Degiorgi.ch.", "SSH_HOSTS": "rp01"}
    )
    assert mit.caddy_domain == "louis-degiorgi.ch"
    assert caddy_verwaltung_bauen(mit, None) is None
    assert caddy_verwaltung_bauen(mit, hosts.verbindung()) is not None


def test_caddy_host_muss_in_ssh_hosts_stehen(tmp_path):
    einstellungen = Einstellungen.aus_umgebung(
        {
            "MISSA_API_KEY": "k",
            "MISSA_DATEN": str(tmp_path),
            "CADDY_HOST": "rp01",
            "CADDY_DOMAIN": "louis-degiorgi.ch",
            "SSH_HOSTS": "wb01=10.0.20.10",
        }
    )
    with pytest.raises(KonfigurationsFehler, match="CADDY_HOST=rp01 fehlt in SSH_HOSTS"):
        einstellungen.pruefen()


# ----------------------------------------------------------------------------- RCON


class FakeMinecraft:
    """Ein winziger RCON-Server (Source-RCON) auf localhost, wie ihn Minecraft spricht."""

    def __init__(self, passwort: str = "geheim") -> None:
        self.passwort = passwort
        self.spieler: list[str] = ["Louis", "Mia"]
        self.whitelist: list[str] = ["Louis", "Mia", "Tom"]
        self.befehle: list[str] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    def antwort(self, befehl: str) -> str:
        self.befehle.append(befehl)
        if befehl == "list":
            return f"There are {len(self.spieler)} of a max of 20 players online: " + ", ".join(
                self.spieler
            )
        if befehl == "whitelist list":
            return f"There are {len(self.whitelist)} whitelisted player(s): " + ", ".join(
                self.whitelist
            )
        if befehl.startswith("whitelist add "):
            name = befehl.split()[-1]
            self.whitelist.append(name)
            return f"Added {name} to the whitelist"
        if befehl.startswith("whitelist remove "):
            name = befehl.split()[-1]
            self.whitelist.remove(name)
            return f"Removed {name} from the whitelist"
        if befehl == "save-all":
            return "Saved the game"
        if befehl == "stop":
            return "Stopping the server"
        return ""

    async def verbindung(self, leser: asyncio.StreamReader, schreiber: asyncio.StreamWriter):
        try:
            angemeldet = False
            while True:
                laenge = struct.unpack("<i", await leser.readexactly(4))[0]
                daten = await leser.readexactly(laenge)
                anfrage_id, typ = struct.unpack("<ii", daten[:8])
                text = daten[8:-2].decode()
                if typ == 3:
                    angemeldet = text == self.passwort
                    schreiber.write(paket(anfrage_id if angemeldet else -1, 2, ""))
                elif typ == 2 and angemeldet:
                    schreiber.write(paket(anfrage_id, 0, self.antwort(text)))
                await schreiber.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            schreiber.close()

    async def __aenter__(self) -> FakeMinecraft:
        self.server = await asyncio.start_server(self.verbindung, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_fehler) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    def verwaltung(self, passwort: str | None = None) -> MinecraftVerwaltung:
        return MinecraftVerwaltung(
            RconClient("127.0.0.1", self.port, passwort or self.passwort, timeout=5)
        )


def mit_server(ablauf, passwort: str = "geheim"):
    async def laufen():
        async with FakeMinecraft(passwort) as server:
            return server, await ablauf(server)

    return asyncio.run(laufen())


def test_paket_format():
    assert paket(7, 2, "list") == struct.pack("<iii", 14, 7, 2) + b"list\x00\x00"


def test_wer_ist_online():
    async def ablauf(server: FakeMinecraft):
        return await server.verwaltung().spieler()

    server, antwort = mit_server(ablauf)
    assert antwort == "Online (2 von 20): Louis, Mia"
    assert server.befehle == ["list"]


def test_niemand_online():
    async def ablauf(server: FakeMinecraft):
        server.spieler = []
        return await server.verwaltung().spieler()

    _, antwort = mit_server(ablauf)
    assert antwort.startswith("Gerade ist niemand online.")


def test_whitelist_anzeigen_hinzufuegen_entfernen(tmp_path):
    async def ablauf(server: FakeMinecraft):
        kasten, protokoll = kasten_mit(minecraft_werkzeuge(server.verwaltung()), tmp_path)
        liste = await kasten.ausfuehren("minecraft_whitelist", {}, "a1")
        dazu = await kasten.ausfuehren("minecraft_whitelist_hinzufuegen", {"spieler": "Lea"}, "a2")
        weg = await kasten.ausfuehren("minecraft_whitelist_entfernen", {"spieler": "Tom"}, "a3")
        falsch = await kasten.ausfuehren(
            "minecraft_whitelist_hinzufuegen", {"spieler": "Lea; op Lea"}, "a4"
        )
        return liste, dazu, weg, falsch, protokoll

    server, (liste, dazu, weg, falsch, protokoll) = mit_server(ablauf)
    assert liste == "Auf der Whitelist (3): Louis, Mia, Tom"
    assert "Added Lea" in dazu and "Removed Tom" in weg
    assert "kein gültiger Minecraft-Name" in falsch
    assert server.whitelist == ["Louis", "Mia", "Lea"]
    assert server.befehle == ["whitelist list", "whitelist add Lea", "whitelist remove Tom"]
    stufen = {a.werkzeug: a.stufe for a in protokoll.liste()}
    assert stufen["minecraft_whitelist_hinzufuegen"] == "schreiben"
    assert stufen["minecraft_whitelist"] == "lesen"


def test_neustart_mit_spielern_warnt_und_fragt_nach(tmp_path):
    async def ablauf(server: FakeMinecraft):
        kasten, _ = kasten_mit(minecraft_werkzeuge(server.verwaltung()), tmp_path)
        rueckfrage = await kasten.ausfuehren("minecraft_neustarten", {}, "a1")
        vor_ja = list(server.befehle)
        antwort = await kasten.ausfuehren("minecraft_neustarten", {}, "a2", nachricht="Ja")
        return rueckfrage, vor_ja, antwort

    server, (rueckfrage, vor_ja, antwort) = mit_server(ablauf)
    assert rueckfrage.startswith("NICHT AUSGEFÜHRT")
    assert "ACHTUNG: Gerade sind 2 Spieler online (Louis, Mia)" in rueckfrage
    assert "stop" not in vor_ja
    assert "startet neu" in antwort
    assert server.befehle[-3:] == [
        "say Der Server startet gleich neu – bis gleich!",
        "save-all",
        "stop",
    ]


def test_neustart_ohne_spieler_ohne_warnung(tmp_path):
    async def ablauf(server: FakeMinecraft):
        server.spieler = []
        kasten, _ = kasten_mit(minecraft_werkzeuge(server.verwaltung()), tmp_path)
        return await kasten.ausfuehren("minecraft_neustarten", {}, "a1")

    _, rueckfrage = mit_server(ablauf)
    assert "Gerade ist niemand online" in rueckfrage and "ACHTUNG" not in rueckfrage


def test_falsches_passwort():
    async def ablauf(server: FakeMinecraft):
        with pytest.raises(RconFehler, match="lehnt das RCON-Passwort ab"):
            await server.verwaltung("falsch").spieler()

    server, _ = mit_server(ablauf)
    assert server.befehle == []


def test_server_nicht_erreichbar():
    async def ablauf():
        async with FakeMinecraft() as server:
            port = server.port
        return await MinecraftVerwaltung(RconClient("127.0.0.1", port, "x", timeout=2)).spieler()

    with pytest.raises(RconFehler, match="per RCON nicht erreichbar"):
        asyncio.run(ablauf())


def test_minecraft_aus_ohne_host_oder_passwort(tmp_path):
    basis = {"MISSA_API_KEY": "k", "MISSA_DATEN": str(tmp_path)}
    assert minecraft_bauen(Einstellungen.aus_umgebung(basis)) is None
    nur_host = Einstellungen.aus_umgebung({**basis, "MINECRAFT_RCON_HOST": "10.0.70.120"})
    assert minecraft_bauen(nur_host) is None
    assert not (tmp_path / "minecraft-rcon.txt").exists()
    (tmp_path / "minecraft-rcon.txt").write_text("ausdatei\n", encoding="utf-8")
    aus_datei = minecraft_bauen(nur_host)
    assert aus_datei is not None and aus_datei.rcon.passwort == "ausdatei"
    assert aus_datei.rcon.port == 25575
    mit_env = Einstellungen.aus_umgebung(
        {
            **basis,
            "MINECRAFT_RCON_HOST": "10.0.70.120",
            "MINECRAFT_RCON_PORT": "25576",
            "MINECRAFT_RCON_PASSWORT": "ausenv",
        }
    )
    verwaltung = minecraft_bauen(mit_env)
    assert verwaltung is not None
    assert (verwaltung.rcon.passwort, verwaltung.rcon.port) == ("ausenv", 25576)
    assert "ausenv" in mit_env.geheimnisse


def test_werkzeuge_nur_mit_konfiguration(caddy, tmp_path):
    einstellungen = Einstellungen.aus_umgebung({"MISSA_API_KEY": "k", "MISSA_DATEN": str(tmp_path)})
    ohne = werkzeuge_laden(einstellungen)
    assert "subdomain_aktivieren" not in ohne and "minecraft_spieler" not in ohne
    mit = werkzeuge_laden(
        einstellungen,
        caddy=caddy,
        minecraft=MinecraftVerwaltung(RconClient("127.0.0.1", 25575, "x")),
    )
    for name in ("subdomain_vorschlagen", "subdomain_aktivieren", "minecraft_neustarten"):
        assert name in mit
