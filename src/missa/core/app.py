"""FastAPI-Anwendung von Missa-Core (OpenAI-kompatible API)."""

# Hinweis: kein ``from __future__ import annotations`` – FastAPI muss die
# Annotationen der lokal definierten Abhängigkeiten (Depends) auswerten können.

import logging
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from missa import __version__
from missa.core.chat import MODELL, AnfrageFehler, ChatDienst
from missa.core.config import Einstellungen
from missa.core.hintergrund import Hintergrund
from missa.core.llama import LlamaClient, LlamaFehler
from missa.core.verlauf import Verlauf
from missa.core.werkzeuge import Werkzeugkasten
from missa.core.zeitplaner import Zeitplaner
from missa.core.zustellung import Zustellung

log = logging.getLogger(__name__)

MAX_SITZUNG_LAENGE = 200


class Nachricht(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None


class ChatAnfrage(BaseModel):
    """Anfrage im OpenAI-Format. Unbekannte Felder werden toleriert."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[Nachricht] = Field(min_length=1)
    stream: bool = False
    user: str | None = None


def _fehler(status: int, nachricht: str, typ: str, code: str | None = None, **kopf: str):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": nachricht, "type": typ, "code": code}},
        headers=kopf or None,
    )


def create_app(
    einstellungen: Einstellungen | None = None,
    *,
    werkzeuge: Werkzeugkasten | None = None,
    llama_transport: httpx.AsyncBaseTransport | None = None,
    talk_transport: httpx.AsyncBaseTransport | None = None,
    uhr: Callable[[], datetime] | None = None,
    zustellung: Zustellung | None = None,
    mail_dienst: Any = None,
    kontaktpflege: Any = None,
    pausen: Any = None,
    server_transport: httpx.AsyncBaseTransport | None = None,
    server_waechter: Any = None,
    erreichbarkeit: Any = None,
    ssh_verbindung: Any = None,
    zertifikate: Any = None,
    homelab: Any = None,
    firewall: Any = None,
    vms: Any = None,
) -> FastAPI:
    """Baut die App. Parameter außer ``einstellungen`` sind vor allem für Tests da."""
    from missa.channels.nextcloud_talk import TalkClient, talk_einrichten
    from missa.core.rechte import rechte_bauen
    from missa.tools import (
        alarme_bauen,
        container_dienst_bauen,
        erreichbarkeit_bauen,
        fehlersuche_bauen,
        firewall_bauen,
        homelab_bauen,
        kontaktpflege_bauen,
        login_waechter_bauen,
        mail_dienst_bauen,
        nextcloud_dienste_bauen,
        pausen_waechter_bauen,
        server_waechter_bauen,
        ssh_verbindung_bauen,
        talk_melder_bauen,
        update_verwaltung_bauen,
        vm_verwaltung_bauen,
        werkzeuge_laden,
        zertifikats_waechter_bauen,
    )
    from missa.tools.erinnerungen import ErinnerungsWerkzeuge
    from missa.tools.gedaechtnis import KATEGORIEN, Gedaechtnis
    from missa.tools.talk import TalkZustellkanal

    einstellungen = einstellungen or Einstellungen.aus_umgebung()
    # Die zentrale Rechte-Schicht (Stufen, Bestätigung, Protokoll, Notfall-Stopp).
    rechte, protokoll = rechte_bauen(einstellungen)
    gedaechtnis = Gedaechtnis(einstellungen.datenbank, einstellungen.zone)
    talk_client = (
        TalkClient(
            einstellungen.talk_api_url, einstellungen.nc_talk_bot_secret, transport=talk_transport
        )
        if einstellungen.talk_aktiv
        else None
    )
    talk_melder = talk_melder_bauen(einstellungen, talk_client) if talk_client is not None else None
    if zustellung is None:
        zustellung = Zustellung()
        if talk_melder is not None and einstellungen.nc_talk_notify_conversation:
            zustellung.hinzufuegen(TalkZustellkanal(talk_melder))
    zeitplaner = Zeitplaner(einstellungen.datenbank, einstellungen.zone, zustellung)
    hintergrund = Hintergrund(
        einstellungen.datenbank,
        einstellungen.zone,
        zeitplaner,
        nacht_von=einstellungen.nachtschicht_von,
        nacht_bis=einstellungen.nachtschicht_bis,
    )
    if mail_dienst is None and einstellungen.mail_konfiguriert:
        mail_dienst = mail_dienst_bauen(einstellungen, gedaechtnis)
    nextcloud = (
        nextcloud_dienste_bauen(einstellungen) if einstellungen.nextcloud_konfiguriert else None
    )
    if kontaktpflege is None and nextcloud is not None:
        kontaktpflege = kontaktpflege_bauen(einstellungen, zeitplaner, nextcloud)
    if kontaktpflege is not None:
        zeitplaner.taeglich(
            "kontaktpflege", einstellungen.kontakte_pruefzeit, kontaktpflege.taeglich_pruefen
        )
        if mail_dienst is not None:
            kontaktpflege.mail_konto = mail_dienst.konto
            mail_dienst.adressquellen.insert(0, kontaktpflege.adressen)
            mail_dienst.nach_senden.append(kontaktpflege.mail_gesendet)
    if (
        pausen is None
        and einstellungen.pausen_aktiv
        and nextcloud is not None
        and nextcloud.kalender is not None
    ):
        pausen = pausen_waechter_bauen(einstellungen, zeitplaner, nextcloud.kalender)
    if pausen is not None:
        zeitplaner.taeglich("pausen", einstellungen.pausen_pruefzeit, pausen.abends_pruefen)
    # Server-Alarme: Stufen, Ruhezeit und Ruhefenster gelten für alle Meldungen.
    alarme = alarme_bauen(einstellungen, zeitplaner)
    if server_waechter is None and einstellungen.server_monitoring_konfiguriert:
        server_waechter = server_waechter_bauen(einstellungen, alarme, server_transport)
    if server_waechter is not None:
        zeitplaner.regelmaessig(
            "server-monitoring",
            timedelta(seconds=einstellungen.monitoring_intervall_sekunden),
            server_waechter.pruefen,
        )
    if erreichbarkeit is None:
        erreichbarkeit = erreichbarkeit_bauen(einstellungen, alarme)
    if erreichbarkeit is not None:
        zeitplaner.regelmaessig(
            "erreichbarkeit",
            timedelta(minutes=einstellungen.erreichbarkeit_intervall_minuten),
            erreichbarkeit.pruefen,
        )
    # Container, Logs und Anmeldungen auf anderen Hosts (SSH als „missa“, nur erlaubte Befehle).
    if ssh_verbindung is None:
        ssh_verbindung = ssh_verbindung_bauen(einstellungen)
    container = fehlersuche = anmeldungen = updates = None
    if ssh_verbindung is not None:
        container = container_dienst_bauen(einstellungen, ssh_verbindung, alarme)
        fehlersuche = fehlersuche_bauen(
            einstellungen, ssh_verbindung, container, server_waechter, erreichbarkeit
        )
        anmeldungen = login_waechter_bauen(einstellungen, ssh_verbindung, alarme)
        zeitplaner.regelmaessig(
            "container",
            timedelta(minutes=einstellungen.container_intervall_minuten),
            container.pruefen,
        )
        zeitplaner.regelmaessig(
            "anmeldungen",
            timedelta(minutes=einstellungen.login_intervall_minuten),
            anmeldungen.pruefen,
        )
        updates = update_verwaltung_bauen(
            einstellungen,
            ssh_verbindung,
            alarme,
            nextcloud.kalender if nextcloud is not None else None,
            rechte,
        )
        zeitplaner.taeglich("updates", einstellungen.updates_pruefzeit, updates.taeglich_pruefen)
        updates.nach_start()
    if zertifikate is None:
        zertifikate = zertifikats_waechter_bauen(einstellungen, alarme)
    if zertifikate is not None:
        zeitplaner.taeglich("zertifikate", einstellungen.zertifikate_pruefzeit, zertifikate.pruefen)
    # Homelab-Wissen (Proxmox, nur lesend) und Firewall-Logs (nur lesend).
    if homelab is None:
        homelab = homelab_bauen(einstellungen, alarme, server_transport)
    if homelab is not None:
        zeitplaner.regelmaessig(
            "homelab",
            timedelta(minutes=einstellungen.homelab_intervall_minuten),
            homelab.scannen,
        )
    if firewall is None:
        firewall = firewall_bauen(einstellungen, alarme)
    if firewall is not None:
        zeitplaner.regelmaessig("firewall-einlesen", timedelta(minutes=1), firewall.einlesen)
        zeitplaner.taeglich(
            "firewall-bericht", einstellungen.firewall_bericht_zeit, firewall.taeglich_berichten
        )
    # Eigene VMs im Pool missa (Proxmox mit Schreibrechten, nur dort).
    if vms is None:
        vms = vm_verwaltung_bauen(einstellungen, alarme, rechte, homelab, server_transport)
    if werkzeuge is None:
        werkzeuge = werkzeuge_laden(
            einstellungen,
            gedaechtnis,
            zeitplaner=zeitplaner,
            talk_melder=talk_melder,
            mail_dienst=mail_dienst,
            nextcloud=nextcloud,
            kontaktpflege=kontaktpflege,
            pausen=pausen,
            hintergrund=hintergrund,
            rechte=rechte,
            protokoll=protokoll,
            alarme=alarme,
            server=server_waechter,
            erreichbarkeit=erreichbarkeit,
            container=container,
            fehlersuche=fehlersuche,
            anmeldungen=anmeldungen,
            updates=updates,
            zertifikate=zertifikate,
            homelab=homelab,
            firewall=firewall,
            vms=vms,
        )
    else:
        werkzeuge.rechte = rechte
    # Ein absichtlich leerer Werkzeugkasten (Tests) bleibt leer.
    if len(werkzeuge) and "aktionen_anzeigen" not in werkzeuge:
        from missa.core.rechte import protokoll_werkzeuge

        for werkzeug in protokoll_werkzeuge(protokoll, rechte, einstellungen.zone):
            werkzeuge.hinzufuegen(werkzeug)
    if mail_dienst is not None:
        mail_dienst.rechte = rechte
    if pausen is not None:
        pausen.rechte = rechte
    kontext_quellen = [
        rechte.kontext,
        gedaechtnis.kontext,
        ErinnerungsWerkzeuge(zeitplaner).kontext,
        hintergrund.kontext,
    ]
    if server_waechter is not None:
        kontext_quellen.append(server_waechter.kontext)
    if erreichbarkeit is not None:
        kontext_quellen.append(erreichbarkeit.kontext)
    if homelab is not None:
        kontext_quellen.append(homelab.kontext)
    # Der Notfall-Stopp im Code kommt zuerst – noch vor der Senden-Sperre für Mails.
    eingangs_pruefer = [rechte.eingang]
    nachbearbeiter = []
    if mail_dienst is not None:
        # Die Senden-Sperre: Senden geht nur über diese Haken, nie über ein Werkzeug.
        kontext_quellen.append(mail_dienst.kontext)
        eingangs_pruefer.append(mail_dienst.eingang)
        nachbearbeiter.append(mail_dienst.nachgang)

    llama = LlamaClient(
        einstellungen.llama_url,
        einstellungen.llama_api_key_file,
        einstellungen.llama_timeout,
        transport=llama_transport,
    )
    verlauf = Verlauf(einstellungen.datenbank)
    dienst = ChatDienst(
        einstellungen,
        llama,
        werkzeuge,
        verlauf,
        uhr=uhr,
        kontext_quellen=kontext_quellen,
        eingangs_pruefer=eingangs_pruefer,
        nachbearbeiter=nachbearbeiter,
    )
    if kontaktpflege is not None:
        # Erinnerungen an wichtige Menschen formuliert Missa in ihrem Charakter.
        kontaktpflege.formulierer = dienst.formulieren
    if pausen is not None:
        pausen.formulierer = dienst.formulieren

    async def hintergrund_ausfuehren(auftrag) -> str:
        return await dienst.hintergrund_ausfuehren(auftrag.auftrag_fuer_missa())

    hintergrund.ausfuehrer = hintergrund_ausfuehren
    gestartet = int(time.time())

    @asynccontextmanager
    async def lebenszyklus(_app: FastAPI):
        log.info(
            "Missa-Core %s bereit – llama-server: %s, Werkzeuge: %s",
            __version__,
            einstellungen.llama_url,
            ", ".join(w.name for w in werkzeuge) or "keine",
        )
        if not zustellung.verfuegbar:
            log.warning(
                "Kein Kanal für Erinnerungen eingerichtet (NC_TALK_NOTIFY_CONVERSATION) – "
                "Erinnerungen werden gespeichert, aber erst zugestellt, wenn ein Kanal da ist"
            )
        zeitplaner.starten()
        hintergrund.starten()
        yield
        await hintergrund.stoppen()
        if vms is not None:
            await vms.warten()
        await zeitplaner.stoppen()
        await llama.schliessen()
        if talk_client is not None:
            await talk_client.schliessen()

    app = FastAPI(
        title="Missa-Core",
        version=__version__,
        description="OpenAI-kompatible API von Missa",
        lifespan=lebenszyklus,
    )
    app.state.einstellungen = einstellungen
    app.state.dienst = dienst
    app.state.gedaechtnis = gedaechtnis
    app.state.zeitplaner = zeitplaner
    app.state.hintergrund = hintergrund
    app.state.mail = mail_dienst
    app.state.kontaktpflege = kontaktpflege
    app.state.pausen = pausen
    app.state.rechte = rechte
    app.state.protokoll = protokoll
    app.state.alarme = alarme
    app.state.server = server_waechter
    app.state.erreichbarkeit = erreichbarkeit
    app.state.container = container
    app.state.anmeldungen = anmeldungen
    app.state.updates = updates
    app.state.zertifikate = zertifikate
    app.state.homelab = homelab
    app.state.firewall = firewall
    app.state.vms = vms

    # -------------------------------------------------------------- Fehlerformat

    @app.exception_handler(HTTPException)
    async def http_fehler(_request: Request, fehler: HTTPException):
        typ = "authentication_error" if fehler.status_code == 401 else "invalid_request_error"
        return _fehler(fehler.status_code, str(fehler.detail), typ, None, **(fehler.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def eingabe_fehler(_request: Request, fehler: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(str(t) for t in e.get('loc', ()))}: {e.get('msg')}" for e in fehler.errors()
        )
        return _fehler(400, f"Ungültige Anfrage: {details}", "invalid_request_error")

    @app.exception_handler(AnfrageFehler)
    async def anfrage_fehler(_request: Request, fehler: AnfrageFehler):
        return _fehler(400, str(fehler), "invalid_request_error")

    @app.exception_handler(LlamaFehler)
    async def llama_fehler(_request: Request, fehler: LlamaFehler):
        log.error("llama-server: %s", fehler)
        return _fehler(fehler.status, str(fehler), "llama_fehler")

    # -------------------------------------------------------------- Anmeldung

    bearer = HTTPBearer(auto_error=False)

    def anmeldung(
        zugang: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        erwartet = einstellungen.api_key
        if (
            zugang is None
            or not erwartet
            or not secrets.compare_digest(zugang.credentials.encode(), erwartet.encode())
        ):
            raise HTTPException(
                status_code=401,
                detail="Fehlender oder ungültiger API-Schlüssel (Authorization: Bearer …)",
                headers={"WWW-Authenticate": "Bearer"},
            )

    geschuetzt = [Depends(anmeldung)]

    # -------------------------------------------------------------- Endpunkte

    @app.get("/health", tags=["Betrieb"])
    async def health() -> JSONResponse:
        """Ohne Anmeldung. 200, wenn llama-server antwortet, sonst 503."""
        llama_ok = await llama.gesund()
        charakter_ok = einstellungen.charakter_datei.is_file()
        return JSONResponse(
            status_code=200 if llama_ok else 503,
            content={
                "status": "ok" if llama_ok else "gestoert",
                "version": __version__,
                "llama": "ok" if llama_ok else "nicht erreichbar",
                "charakter": "ok" if charakter_ok else "fehlt",
                "werkzeuge": [w.name for w in werkzeuge],
            },
        )

    def modell_info() -> dict[str, Any]:
        return {"id": MODELL, "object": "model", "created": gestartet, "owned_by": "missa"}

    @app.get("/v1/models", dependencies=geschuetzt, tags=["OpenAI"])
    async def modelle() -> dict[str, Any]:
        return {"object": "list", "data": [modell_info()]}

    @app.get("/v1/models/{modell}", dependencies=geschuetzt, tags=["OpenAI"])
    async def modell(modell: str) -> dict[str, Any]:
        if modell != MODELL:
            raise HTTPException(status_code=404, detail=f"Unbekanntes Modell: {modell}")
        return modell_info()

    @app.get("/api/gedaechtnis", dependencies=geschuetzt, tags=["Missa"])
    async def gedaechtnis_anzeigen(kategorie: str | None = None) -> dict[str, Any]:
        """Alle gespeicherten Fakten (optional nur eine Kategorie), damit Louis sie prüfen kann."""
        if kategorie is not None and kategorie not in KATEGORIEN:
            raise HTTPException(
                status_code=400,
                detail=f"Unbekannte Kategorie: {kategorie}. Erlaubt: {', '.join(KATEGORIEN)}",
            )
        fakten = gedaechtnis.alle(kategorie)
        return {"anzahl": len(fakten), "fakten": [f.als_dict() for f in fakten]}

    @app.get("/api/erinnerungen", dependencies=geschuetzt, tags=["Missa"])
    async def erinnerungen_anzeigen() -> dict[str, Any]:
        """Alle geplanten Erinnerungen (nächste zuerst)."""
        offene = zeitplaner.offene()
        return {
            "anzahl": len(offene),
            "kanaele": zustellung.namen,
            "erinnerungen": [e.als_dict() for e in offene],
        }

    @app.get("/api/routinen", dependencies=geschuetzt, tags=["Missa"])
    async def routinen_anzeigen() -> dict[str, Any]:
        """Alle Routinen (auch pausierte), nächster Lauf zuerst."""
        routinen = zeitplaner.offene("routine")
        return {"anzahl": len(routinen), "routinen": [r.als_dict() for r in routinen]}

    @app.get("/api/nachtschicht", dependencies=geschuetzt, tags=["Missa"])
    async def nachtschicht_anzeigen() -> dict[str, Any]:
        """Nachtfenster, vorgemerkte Nachtaufgaben und Ergebnisse der letzten 7 Tage."""
        von, bis = hintergrund.fenster()
        auftraege = hintergrund.liste("nacht")
        return {
            "fenster": {"von": f"{von:%H:%M}", "bis": f"{bis:%H:%M}"},
            "anzahl": len(auftraege),
            "auftraege": [a.als_dict() for a in auftraege],
        }

    @app.get("/api/kalender/protokoll", dependencies=geschuetzt, tags=["Missa"])
    async def kalender_protokoll(anzahl: int = 50) -> dict[str, Any]:
        """Missas letzte Kalenderänderungen mit Zustand vorher/nachher (neueste zuerst)."""
        from missa.tools.kalender_protokoll import KalenderProtokoll

        liste = KalenderProtokoll(einstellungen.datenbank, einstellungen.zone).liste(
            max(1, min(anzahl, 500))
        )
        return {"anzahl": len(liste), "aenderungen": [a.als_dict() for a in liste]}

    @app.get("/api/aktionen", dependencies=geschuetzt, tags=["Missa"])
    async def aktionen_anzeigen(
        von: str | None = None,
        bis: str | None = None,
        anzahl: int = 100,
        werkzeug: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Aktionsprotokoll (nur lesen): jeder Werkzeug-Aufruf, neueste zuerst.

        ``von``/``bis`` als Datum JJJJ-MM-TT (einschließlich), dazu der Notfall-Zustand.
        """
        from datetime import date

        try:
            von_datum = date.fromisoformat(von) if von else None
            bis_datum = date.fromisoformat(bis) if bis else None
        except ValueError as fehler:
            raise HTTPException(
                status_code=400, detail="von und bis müssen ein Datum wie 2026-11-11 sein"
            ) from fehler
        liste = protokoll.liste(
            von_datum, bis_datum, max(1, min(anzahl, 1000)), werkzeug or None, status or None
        )
        notfall = rechte.notfall.info() if rechte.notfall is not None else {"aktiv": False}
        return {
            "anzahl": len(liste),
            "notfall": notfall,
            "aktionen": [a.als_dict() for a in liste],
        }

    @app.get("/api/alarme", dependencies=geschuetzt, tags=["Missa"])
    async def alarme_anzeigen(tage: int = 1) -> dict[str, Any]:
        """Gemeldete Alarme (neueste zuerst), Alarm-Regeln, Ruhezeit und Ruhefenster."""
        seit = alarme.jetzt() - timedelta(days=max(1, min(tage, 90)))
        liste = alarme.liste(seit, 500)
        ruhe = alarme.ruhezeit()
        return {
            "anzahl": len(liste),
            "ruhezeit": {"von": f"{ruhe[0]:%H:%M}", "bis": f"{ruhe[1]:%H:%M}"} if ruhe else None,
            "ruhefenster_minuten": int(alarme.ruhefenster().total_seconds() // 60),
            "regeln": [r.als_dict() for r in server_waechter.regeln()]
            if server_waechter is not None
            else [],
            "aktiv": server_waechter.aktive_alarme() if server_waechter is not None else [],
            "alarme": [a.als_dict() for a in liste],
        }

    @app.get("/api/server", dependencies=geschuetzt, tags=["Missa"])
    async def server_anzeigen() -> dict[str, Any]:
        """Letzte Messwerte aller Quellen und der Zustand der geprüften Domains."""
        werte, gemessen, fehler = [], None, {}
        if server_waechter is not None:
            werte = [w.als_dict() for w in server_waechter.aktuell.values()]
            if server_waechter.zuletzt_gemessen is not None:
                gemessen = server_waechter.zuletzt_gemessen.isoformat(timespec="seconds")
            fehler = dict(server_waechter.quellen_fehler)
        domains = list(erreichbarkeit.zustand().values()) if erreichbarkeit is not None else []
        return {
            "monitoring": server_waechter is not None,
            "gemessen": gemessen,
            "quellen": [q.name for q in server_waechter.quellen] if server_waechter else [],
            "quellen_fehler": fehler,
            "werte": werte,
            "erreichbarkeit": domains,
        }

    @app.get("/api/container", dependencies=geschuetzt, tags=["Missa"])
    async def container_anzeigen() -> dict[str, Any]:
        """Zuletzt gelesener Stand der Docker-Container je Host (ohne neue SSH-Abfrage)."""
        if container is None:
            return {"ssh": False, "hosts": {}}
        return {
            "ssh": True,
            "hosts": {
                host: {
                    "stand": container.stand[host].isoformat(timespec="seconds")
                    if host in container.stand
                    else None,
                    "container": [c.als_dict() for c in container.zuletzt.get(host, [])],
                }
                for host in container.ssh.namen
            },
        }

    @app.get("/api/anmeldungen", dependencies=geschuetzt, tags=["Missa"])
    async def anmeldungen_anzeigen(tage: int = 1) -> dict[str, Any]:
        """Gespeicherte SSH- und Nextcloud-Anmeldungen und die bekannten IPs (nur lesen)."""
        if anmeldungen is None:
            return {"ssh": False, "anmeldungen": [], "bekannte_ips": []}
        from missa.core.datenbank import verbinden

        seit = alarme.jetzt() - timedelta(days=max(1, min(tage, 90)))
        with verbinden(einstellungen.datenbank) as db:
            zeilen = db.execute(
                "SELECT quelle, host, zeit, benutzer, ip, erfolg, art FROM anmeldungen "
                "WHERE zeit >= ? ORDER BY zeit DESC LIMIT 1000",
                (seit.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S+00:00"),),
            ).fetchall()
        return {
            "ssh": True,
            "anzahl": len(zeilen),
            "anmeldungen": [{**dict(z), "erfolg": bool(z["erfolg"])} for z in zeilen],
            "bekannte_ips": anmeldungen.bekannte(),
        }

    @app.get("/api/updates", dependencies=geschuetzt, tags=["Missa"])
    async def updates_anzeigen() -> dict[str, Any]:
        """Zuletzt erfasste Updates pro VM und das nächste Wartungsfenster (ohne neue Abfrage)."""
        if updates is None:
            return {"ssh": False, "hosts": [], "wartungsfenster": None}
        fenster = updates.naechstes_fenster()
        return {
            "ssh": True,
            "hosts": [s.als_dict() for h in updates.hosts if (s := updates.stand(h)) is not None],
            "reihenfolge": updates.hosts,
            "wartungsfenster": {
                "beginn": fenster[0].isoformat(timespec="minutes"),
                "ende": fenster[1].isoformat(timespec="minutes"),
            }
            if fenster
            else None,
        }

    @app.get("/api/homelab", dependencies=geschuetzt, tags=["Missa"])
    async def homelab_anzeigen() -> dict[str, Any]:
        """Missas Wissen über VMs und Container aus Proxmox (letzter Scan, nur lesend)."""
        if homelab is None:
            return {"proxmox": False, "stand": None, "vms": []}
        stand = homelab.stand()
        return {
            "proxmox": True,
            "stand": stand.isoformat(timespec="seconds") if stand else None,
            "vms": [e.als_dict() for e in homelab.eintraege()],
        }

    @app.get("/api/vms", dependencies=geschuetzt, tags=["Missa"])
    async def vms_anzeigen() -> dict[str, Any]:
        """Aufträge für Missas eigene VMs (ohne neue Proxmox-Abfrage)."""
        if vms is None:
            return {"vms": False, "auftraege": []}
        return {"vms": True, "pool": vms.proxmox.pool, "auftraege": vms._auftraege()[:50]}

    @app.post("/v1/chat/completions", dependencies=geschuetzt, tags=["OpenAI"])
    async def chat_completions(
        anfrage: ChatAnfrage,
        x_missa_session: Annotated[str | None, Header()] = None,
    ):
        sitzung = (x_missa_session or anfrage.user or "").strip() or None
        if sitzung and len(sitzung) > MAX_SITZUNG_LAENGE:
            raise AnfrageFehler(f"Sitzungs-ID ist länger als {MAX_SITZUNG_LAENGE} Zeichen")

        vorbereitet = dienst.vorbereiten(anfrage.model_dump(exclude_none=True), sitzung)
        vorbereitet.account = (anfrage.user or "").strip()[:MAX_SITZUNG_LAENGE]
        if anfrage.stream:
            return StreamingResponse(
                dienst.antworten_stream(vorbereitet),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return await dienst.antworten(vorbereitet)

    # -------------------------------------------------------------- Kanäle

    app.state.talk = talk_einrichten(app, einstellungen, dienst, talk_client)

    return app
