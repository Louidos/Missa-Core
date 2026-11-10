"""Zugriff auf das eigene Mailkonto über IMAP (lesen, Entwürfe) und SMTP (senden).

Zugangsdaten kommen nur aus der Einstellungsdatei (``MAIL_*``), nie aus dem Chat.
Dieses Modul kennt keine Regeln – wer senden darf, entscheidet
:class:`missa.tools.email.MailDienst` (Senden-Sperre).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from missa.core.werkzeuge import WerkzeugFehler

log = logging.getLogger(__name__)

KOPFZEILEN = "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"


class MailFehler(WerkzeugFehler):
    """Der Mailserver ist nicht erreichbar oder lehnt ab."""


@dataclass(frozen=True)
class MailKopf:
    """Kopfdaten einer Mail in einem Ordner."""

    ordner: str
    uid: str
    von: str
    an: str
    betreff: str
    datum: str
    message_id: str

    @property
    def kennung(self) -> str:
        """Kurze ID für das Modell, z. B. ``INBOX:42``."""
        return f"{self.ordner}:{self.uid}"


def _ordner(name: str) -> str:
    """IMAP-Ordnername in Anführungszeichen (für Namen mit Leerzeichen)."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def text_aus_mail(nachricht: email.message.Message) -> str:
    """Der lesbare Text einer Mail (text/plain bevorzugt, sonst HTML ohne Tags)."""
    if not isinstance(nachricht, EmailMessage):
        nachricht = email.message_from_bytes(nachricht.as_bytes(), policy=email.policy.default)
    teil = nachricht.get_body(preferencelist=("plain", "html"))
    if teil is None:
        return ""
    try:
        inhalt = teil.get_content()
    except (LookupError, ValueError):
        inhalt = teil.get_payload(decode=True).decode("utf-8", "replace")
    if teil.get_content_subtype() == "html":
        inhalt = re.sub(r"(?is)<(script|style).*?</\1>", "", inhalt)
        inhalt = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", inhalt)
        inhalt = re.sub(r"<[^>]+>", "", inhalt)
    return inhalt.strip()


def adressen(feld: str | None) -> list[tuple[str, str]]:
    """(Name, Adresse) aus einem Kopffeld."""
    return [(n, a.lower()) for n, a in getaddresses([feld or ""]) if a]


class MailKonto:
    """IMAP und SMTP des eigenen Mailservers."""

    def __init__(
        self,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        benutzer: str,
        passwort: str,
        absender: str,
        ordner_entwuerfe: str = "Drafts",
        ordner_gesendet: str = "Sent",
        imap_fabrik: Callable[..., Any] | None = None,
        smtp_fabrik: Callable[..., Any] | None = None,
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.benutzer = benutzer
        self._passwort = passwort
        self.absender = absender or benutzer
        self.ordner_entwuerfe = ordner_entwuerfe
        self.ordner_gesendet = ordner_gesendet
        self._imap_fabrik = imap_fabrik
        self._smtp_fabrik = smtp_fabrik

    # -------------------------------------------------------------- IMAP

    @contextmanager
    def _imap(self, aktion: str) -> Iterator[Any]:
        try:
            if self._imap_fabrik is not None:
                verbindung = self._imap_fabrik(self.imap_host, self.imap_port)
            else:
                verbindung = imaplib.IMAP4_SSL(
                    self.imap_host, self.imap_port, ssl_context=ssl.create_default_context()
                )
            verbindung.login(self.benutzer, self._passwort)
        except imaplib.IMAP4.error as fehler:
            raise MailFehler(
                f"{aktion}: Der Mailserver lehnt die Anmeldung ab ({fehler})"
            ) from fehler
        except OSError as fehler:
            raise MailFehler(f"{aktion}: Mailserver nicht erreichbar ({fehler})") from fehler
        try:
            yield verbindung
        except (imaplib.IMAP4.error, OSError) as fehler:
            log.exception("%s fehlgeschlagen", aktion)
            raise MailFehler(f"{aktion}: {fehler}") from fehler
        finally:
            try:
                verbindung.logout()
            except Exception:
                log.debug("IMAP-Logout fehlgeschlagen", exc_info=True)

    @staticmethod
    def _pruefen(antwort: tuple[str, Any], aktion: str) -> Any:
        status, daten = antwort
        if status != "OK":
            raise MailFehler(f"{aktion}: Der Mailserver meldet {status} ({daten})")
        return daten

    def _waehlen(self, imap: Any, ordner: str, nur_lesen: bool = True) -> None:
        self._pruefen(imap.select(_ordner(ordner), readonly=nur_lesen), f"Ordner „{ordner}“")

    def koepfe(
        self, ordner: str = "INBOX", anzahl: int = 10, nur_ungelesen: bool = False
    ) -> list[MailKopf]:
        """Die neuesten ``anzahl`` Mails eines Ordners (neueste zuerst)."""
        with self._imap("Mails lesen") as imap:
            self._waehlen(imap, ordner)
            treffer = self._pruefen(
                imap.uid("SEARCH", None, "UNSEEN" if nur_ungelesen else "ALL"), "Suchen"
            )
            uids = (treffer[0] or b"").split()[-max(1, anzahl) :]
            ergebnis = []
            for uid in reversed(uids):
                daten = self._pruefen(imap.uid("FETCH", uid, KOPFZEILEN), "Lesen")
                roh = next((t[1] for t in daten if isinstance(t, tuple)), b"")
                kopf = email.message_from_bytes(roh, policy=email.policy.default)
                ergebnis.append(
                    MailKopf(
                        ordner=ordner,
                        uid=uid.decode(),
                        von=str(kopf.get("From", "")),
                        an=str(kopf.get("To", "")),
                        betreff=str(kopf.get("Subject", "")),
                        datum=_datum(kopf.get("Date")),
                        message_id=str(kopf.get("Message-ID", "")).strip(),
                    )
                )
            return ergebnis

    def nachricht(self, ordner: str, uid: str) -> EmailMessage:
        with self._imap("Mail lesen") as imap:
            self._waehlen(imap, ordner)
            daten = self._pruefen(imap.uid("FETCH", str(uid), "(BODY.PEEK[])"), "Lesen")
            roh = next((t[1] for t in daten if isinstance(t, tuple)), None)
            if not roh:
                raise MailFehler(f"Die Mail {ordner}:{uid} gibt es nicht (mehr).")
            return email.message_from_bytes(roh, policy=email.policy.default)  # type: ignore[return-value]

    def gesendete(self, anzahl: int = 40) -> list[EmailMessage]:
        """Die neuesten gesendeten Mails (für das Schreibstil-Profil)."""
        with self._imap("Gesendete Mails lesen") as imap:
            self._waehlen(imap, self.ordner_gesendet)
            treffer = self._pruefen(imap.uid("SEARCH", None, "ALL"), "Suchen")
            uids = (treffer[0] or b"").split()[-max(1, anzahl) :]
            ergebnis = []
            for uid in uids:
                daten = self._pruefen(imap.uid("FETCH", uid, "(BODY.PEEK[])"), "Lesen")
                if roh := next((t[1] for t in daten if isinstance(t, tuple)), None):
                    ergebnis.append(email.message_from_bytes(roh, policy=email.policy.default))
            return ergebnis  # type: ignore[return-value]

    def anhaengen(self, ordner: str, nachricht: EmailMessage, flags: str) -> None:
        with self._imap(f"In „{ordner}“ ablegen") as imap:
            self._pruefen(
                imap.append(
                    _ordner(ordner),
                    flags,
                    imaplib.Time2Internaldate(time.time()),
                    nachricht.as_bytes(policy=email.policy.SMTP),
                ),
                "Ablegen",
            )

    def entfernen(self, ordner: str, message_id: str) -> int:
        """Löscht Mails mit dieser Message-ID aus einem Ordner (z. B. alte Entwurfsfassung)."""
        with self._imap(f"Aus „{ordner}“ entfernen") as imap:
            self._waehlen(imap, ordner, nur_lesen=False)
            treffer = self._pruefen(
                imap.uid("SEARCH", None, "HEADER", "Message-ID", message_id), "Suchen"
            )
            uids = (treffer[0] or b"").split()
            for uid in uids:
                self._pruefen(imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)"), "Markieren")
            if uids:
                imap.expunge()
            return len(uids)

    def adressen_suchen(self, name: str, anzahl: int = 200) -> dict[str, str]:
        """Adressen, deren Anzeigename ``name`` enthält (aus Posteingang und Gesendet)."""
        gesucht = name.casefold().strip()
        gefunden: dict[str, str] = {}
        for ordner in ("INBOX", self.ordner_gesendet):
            try:
                koepfe = self.koepfe(ordner, anzahl)
            except MailFehler:
                continue
            for kopf in koepfe:
                for anzeige, adresse in adressen(kopf.von) + adressen(kopf.an):
                    if gesucht and (gesucht in anzeige.casefold() or gesucht in adresse):
                        gefunden.setdefault(adresse, anzeige or adresse)
        gefunden.pop(self.benutzer.lower(), None)
        return gefunden

    # -------------------------------------------------------------- SMTP

    def smtp_senden(self, nachricht: EmailMessage) -> None:
        """Schickt eine fertige Mail. Nur über die Senden-Sperre aufrufen!"""
        try:
            if self._smtp_fabrik is not None:
                smtp = self._smtp_fabrik(self.smtp_host, self.smtp_port)
            elif self.smtp_port == 465:
                smtp = smtplib.SMTP_SSL(
                    self.smtp_host, self.smtp_port, context=ssl.create_default_context(), timeout=30
                )
            else:
                smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
                smtp.starttls(context=ssl.create_default_context())
            with smtp:
                smtp.login(self.benutzer, self._passwort)
                smtp.send_message(nachricht)
        except smtplib.SMTPAuthenticationError as fehler:
            raise MailFehler("Senden: Der Mailserver lehnt die Anmeldung ab") from fehler
        except (smtplib.SMTPException, OSError) as fehler:
            raise MailFehler(f"Senden fehlgeschlagen: {fehler}") from fehler


def _datum(wert: Any) -> str:
    if not wert:
        return ""
    try:
        return parsedate_to_datetime(str(wert)).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(wert)
