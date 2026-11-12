"""Gefälschtes Nextcloud für HTTP-Zugriffe: Notes-API und WebDAV (Dateien).

Merkt sich jede Anfrage, damit Tests prüfen können, dass z. B. nie außerhalb von
``Missa/`` geschrieben und nie etwas gelöscht wird.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, unquote

import httpx

BENUTZER = "louis"
WURZEL = f"/remote.php/dav/files/{BENUTZER}/"
NOTES = "/index.php/apps/notes/api/v1/notes"
GEAENDERT = "Wed, 11 Nov 2026 08:00:00 GMT"


class FakeNextcloudHttp:
    def __init__(self) -> None:
        self.dateien: dict[str, bytes] = {}
        self.ordner: set[str] = {""}
        self.notizen: dict[int, dict[str, Any]] = {}
        self.anfragen: list[tuple[str, str]] = []
        self.transport = httpx.MockTransport(self._bearbeiten)

    # -------------------------------------------------------------- Hilfen

    def datei(self, pfad: str, inhalt: str) -> None:
        self.dateien[pfad] = inhalt.encode()
        teile = pfad.split("/")[:-1]
        for ende in range(1, len(teile) + 1):
            self.ordner.add("/".join(teile[:ende]))

    def notiz(self, titel: str, inhalt: str, kategorie: str = "") -> int:
        nummer = len(self.notizen) + 1
        self.notizen[nummer] = {
            "id": nummer,
            "title": titel,
            "content": inhalt,
            "category": kategorie,
            "modified": 1_794_000_000,
        }
        return nummer

    def schreibende(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.anfragen if m in ("PUT", "MKCOL", "DELETE", "MOVE", "COPY")]

    # -------------------------------------------------------------- Antworten

    def _eintrag(self, pfad: str) -> str:
        ordner = pfad in self.ordner
        groesse = (
            "" if ordner else f"<d:getcontentlength>{len(self.dateien[pfad])}</d:getcontentlength>"
        )
        typ = "<d:collection/>" if ordner else ""
        href = WURZEL + quote(pfad) + ("/" if ordner and pfad else "")
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:displayname>{pfad.rsplit('/', 1)[-1]}</d:displayname>{groesse}"
            f"<d:getlastmodified>{GEAENDERT}</d:getlastmodified>"
            f"<d:resourcetype>{typ}</d:resourcetype></d:prop></d:propstat></d:response>"
        )

    def _multistatus(self, pfade: list[str]) -> httpx.Response:
        inhalt = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        inhalt += "".join(self._eintrag(p) for p in pfade) + "</d:multistatus>"
        return httpx.Response(207, content=inhalt.encode())

    def _bearbeiten(self, request: httpx.Request) -> httpx.Response:
        pfad = unquote(request.url.path)
        self.anfragen.append((request.method, pfad))
        if pfad.startswith(NOTES):
            return self._notes(request, pfad.removeprefix(NOTES).strip("/"))
        if request.method == "SEARCH":
            text = request.content.decode()
            wort = re.search(r"<d:literal>%(.*?)%</d:literal>", text).group(1).casefold()
            scope = re.search(r"<d:href>/files/[^/<]+/?(.*?)</d:href>", text).group(1).strip("/")
            alle = sorted(self.ordner - {""}) + sorted(self.dateien)
            treffer = [
                p
                for p in alle
                if wort in p.rsplit("/", 1)[-1].casefold()
                and (not scope or p == scope or p.startswith(scope + "/"))
            ]
            return self._multistatus(treffer)
        if not pfad.startswith(WURZEL.rstrip("/")):
            return httpx.Response(404)
        relativ = pfad[len(WURZEL) :].strip("/") if pfad != WURZEL.rstrip("/") else ""
        if request.method == "PROPFIND":
            if relativ not in self.ordner:
                return httpx.Response(404)
            kinder = [
                p
                for p in sorted(self.ordner | set(self.dateien))
                if p and p.rsplit("/", 1)[0] == relativ
                if "/" in p
            ] + [
                p
                for p in sorted(self.ordner | set(self.dateien))
                if p and "/" not in p and not relativ
            ]
            return self._multistatus([relativ, *kinder])
        if request.method == "GET":
            if relativ in self.dateien:
                return httpx.Response(200, content=self.dateien[relativ])
            return httpx.Response(404)
        if request.method == "MKCOL":
            if relativ in self.ordner:
                return httpx.Response(405)
            self.ordner.add(relativ)
            return httpx.Response(201)
        if request.method == "PUT":
            if relativ.rsplit("/", 1)[0] not in self.ordner and "/" in relativ:
                return httpx.Response(409)
            self.dateien[relativ] = request.content
            return httpx.Response(201)
        return httpx.Response(405)

    def _notes(self, request: httpx.Request, rest: str) -> httpx.Response:
        if request.method == "GET" and not rest:
            return httpx.Response(200, json=list(self.notizen.values()))
        if request.method == "GET":
            notiz = self.notizen.get(int(rest))
            return httpx.Response(200, json=notiz) if notiz else httpx.Response(404)
        if request.method == "POST":
            daten = json.loads(request.content)
            nummer = self.notiz(daten["title"], daten["content"], daten.get("category", ""))
            return httpx.Response(200, json=self.notizen[nummer])
        if request.method == "PUT":
            notiz = self.notizen.get(int(rest))
            if notiz is None:
                return httpx.Response(404)
            notiz.update(json.loads(request.content))
            return httpx.Response(200, json=notiz)
        return httpx.Response(405)
