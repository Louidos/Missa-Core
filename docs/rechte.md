# Rechte, Aktionsprotokoll und Notfall-Stopp

Jede Aktion von Missa läuft durch eine zentrale Schicht (`src/missa/core/rechte.py`). Sie
sorgt für vier Dinge: Jedes Werkzeug hat eine **Stufe**, vor Zerstörerischem kommt eine
**Rückfrage**, alles landet im **Aktionsprotokoll**, und der **Notfall-Stopp** entzieht auf
einen Schlag alle Schreibrechte.

## Stufen

| Stufe            | Bedeutung                                                    | Beispiele |
|------------------|--------------------------------------------------------------|-----------|
| `lesen`          | schaut nur nach, ändert nichts                               | `termine_anzeigen`, `erinnern`, `emails_anzeigen`, `aktionen_anzeigen`, `server_werte_anzeigen`, `erreichbarkeit_pruefen` |
| `schreiben`      | legt an oder ändert – harmlos oder rückgängig machbar         | `termin_eintragen`, `termin_loeschen`*, `aufgabe_erstellen`, `merken`, `email_entwurf_erstellen`, `alarmregel_anlegen`, `alarm_einstellungen` |
| `zerstoererisch` | löscht endgültig, verwirft, versendet – oder startet, installiert, aktiviert etwas neu | `vergessen`, `aufgabe_loeschen`, `erinnerung_loeschen`, `routine_loeschen`, `nachtaufgabe_loeschen`, `email_entwurf_verwerfen`, `alarmregel_loeschen`, E-Mail senden |

\* Termine lassen sich mit „mach das rückgängig“ wiederherstellen, darum gilt Löschen im
Kalender als `schreiben` (so seit 0.3.0). Bekämen andere Personen eine Absage, fragt Missa
trotzdem vorher.

Ein Werkzeug **ohne Stufe lässt sich nicht registrieren** – der Dienst startet dann nicht.
Neue Werkzeuge bekommen ihre Stufe direkt bei der Definition:

```python
Werkzeug("aufgabe_loeschen", "…", schema, funktion, stufe=Stufe.ZERSTOERERISCH)
```

## Rückfrage vor Zerstörerischem

Ruft das Modell ein Werkzeug der Stufe `zerstoererisch` auf, passiert zunächst **nichts**.
Missa bekommt stattdessen eine Beschreibung, *was* passieren würde („Aufgabe ‚Milch kaufen‘
wird gelöscht“), und fragt dich. Ausgeführt wird erst, wenn

1. die Rückfrage in einer **früheren** Nachricht gestellt wurde (Missa kann nicht im selben
   Zug fragen und löschen),
2. deine **neue** Nachricht eine knappe, eindeutige Zustimmung ist – ein „Ja“ („Ja“,
   „Okay, mach“) **oder** ein klarer Auftrag wie „Lösch es“, „Entferne den Termin“,
   „Verwirf den Entwurf“. Ein extra „Ja“ ist also nicht nötig. Nicht zählen: „Ja, aber …“,
   „lösch es lieber nicht“, Fragen („Lösch es?“), „Vergiss es“ (heißt meist „lass es“) und
   lange Texte,
3. Missa genau **dieselbe Aktion** mit denselben Angaben noch einmal aufruft und
4. die Rückfrage höchstens **10 Minuten** alt ist (`MISSA_BESTAETIGUNG_MINUTEN`).

Jedes Ja gilt genau **einmal**. Im Hintergrund (Routinen, Nachtschicht) läuft nie etwas
Zerstörerisches – dort kann niemand Ja sagen. Nach einem Neustart sind offene Rückfragen
weg; Missa fragt dann neu.

E-Mails senden ist kein Werkzeug: Das macht weiterhin die Senden-Sperre im Code nach deinem
„Ja, abschicken“ (siehe [email.md](email.md)). Auch das steht im Protokoll und ist im
Notfall gesperrt.

## Aktionsprotokoll

Jeder Werkzeug-Aufruf – auch abgelehnte, gesperrte und fehlerhafte – kommt in die Tabelle
`aktionsprotokoll` in `MISSA_DATEN/missa.db`:

| Feld         | Inhalt |
|--------------|--------|
| `zeit`       | Zeitpunkt (Zeitzone `TIMEZONE`) |
| `kanal`      | `api`, `talk`, `hintergrund` (Routine, Nachtschicht, Pausen) oder `ai01` (Scripts) |
| `account`    | Talk-Benutzer, `user` der API-Anfrage oder `missa` im Hintergrund |
| `werkzeug`   | Name des Werkzeugs |
| `stufe`      | `lesen`, `schreiben`, `zerstoererisch` |
| `parameter`  | Angaben des Aufrufs, **Geheimnisse geschwärzt** (`***`) |
| `bestaetigt` | ob du die Aktion ausdrücklich bestätigt hast |
| `status`     | `ausgefuehrt`, `fehlgeschlagen`, `wartet_auf_bestaetigung`, `gesperrt_notfall`, `abgelehnt`, `ungueltig`, `notfall_stopp`, `notfall_freigabe` |
| `ergebnis`   | gekürzt auf 500 Zeichen, ebenfalls geschwärzt |

Geschwärzt werden Felder mit Namen wie `passwort`, `token`, `secret`, `api_key`, `auth`, alle
Zugangsdaten aus der Konfiguration (API-Key, App-Passwörter, Bot-Secret, Mail-Passwort,
llama-Key, Proxmox-Token, Monitoring-Token) und Muster wie `Bearer …` oder `passwort=…` im Text.

Die Tabelle wird beim Start automatisch angelegt – beim Update ist nichts zu tun.

**Abrufen:**

- Im Chat: „Was hast du heute gemacht?“ – Missa nutzt das Werkzeug `aktionen_anzeigen`
  (heute, gestern, die letzten 7 Tage oder ein Datum).
- Über die API (nur lesend, für das spätere Logbuch im Admin-Panel):
  `GET /api/aktionen`, siehe [api.md](api.md).

## Notfall-Stopp

Der Notfall-Stopp sperrt **sofort** alle Werkzeuge der Stufen `schreiben` und
`zerstoererisch` sowie das Senden von E-Mails und das automatische Eintragen von
Pausenblöcken. Lesen und Reden geht weiter, Erinnerungen werden weiter zugestellt.

**Auslösen** – zwei Wege:

- Im Chat (API oder Talk) „Notfall-Stopp“ schreiben (auch „Notfallstopp“, „Not-Aus-Stopp“).
  Das erkennt der **Code**, bevor das Modell die Nachricht sieht.
- Auf ai01:

  ```bash
  sudo /opt/missa/app/scripts/notfall-stopp.sh "Grund"
  ```

Der Stopp ist eine Datei, `MISSA_DATEN/notfall-stopp` (mit Zeit, Grund und Quelle). Er gilt
darum auch nach einem Neustart des Dienstes oder von ai01 und braucht keinen Neustart.

**Aufheben** geht **nur auf ai01**, nicht per Chat und nicht über die API:

```bash
sudo /opt/missa/app/scripts/notfall-freigabe.sh
```

Das Script zeigt den aktiven Stopp, fragt nach und trägt die Freigabe ins Protokoll ein
(`--ja` überspringt die Rückfrage). Im Dienst gibt es keinen Code, der die Datei löscht.

Stand prüfen: `sudo -u missa /opt/missa/venv/bin/python -m missa.core.rechte status` oder
`GET /api/aktionen` (Feld `notfall`).

## Rechte pro Dienst

Jeder Dienst bekommt eigene Zugangsdaten, die nur können, was Missa wirklich braucht. Die
Blöcke in [config/missa.env.example](../config/missa.env.example) sind danach geordnet.

| Dienst | Zugangsdaten | Nötige Rechte | Nicht nötig – nicht vergeben |
|--------|--------------|---------------|------------------------------|
| Missa-API | `MISSA_API_KEY` | Bearer-Token für Apps; die eigentliche Prüfung macht Caddy | – |
| llama-server | `LLAMA_API_KEY_FILE` | Anfragen an das lokale Modell | – |
| Nextcloud (Kalender, Aufgaben, Kontakte) | `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD` | CalDAV: Lesen und Schreiben im Kalender `NEXTCLOUD_KALENDER`, im Pausen-Kalender und in der Aufgabenliste; Kalender anlegen (für „Pausen“ und eigene Kalender). CardDAV: nur **lesen** im Adressbuch | Admin-Rechte, Dateien, andere Benutzer; am besten ein eigener Benutzer `missa`, mit dem du nur diese Kalender, die Liste und das Adressbuch teilst (Adressbuch nur lesend). Eigenes App-Passwort, damit es einzeln widerrufbar ist |
| Nextcloud Talk | `NC_TALK_BOT_SECRET` | Bot: Nachrichten empfangen, senden und reagieren – nur in Unterhaltungen, in denen der Bot aktiviert ist | Benutzer-Passwort, Moderator- oder Admin-Rechte |
| E-Mail | `MAIL_USER`, `MAIL_PASSWORD` | IMAP: Posteingang und Gesendet lesen, im Entwürfe-Ordner ablegen und entfernen. SMTP: senden (nur nach deinem Ja) | Filter, Weiterleitungen, Kontoeinstellungen; wenn möglich ein App-Passwort statt des Hauptpassworts |
| Proxmox (Server-Alarme) | `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET` | Token `missa@pve!monitor` mit Rolle **PVEAuditor** auf `/` (nur lesen), Rechte-Trennung `--privsep 1`; Einrichtung mit `scripts/einrichten/proxmox-lesetoken.sh` auf pve-id01 | Starten, Stoppen, Ändern, Löschen, Konsole, Root-Zugang; der Benutzer `missa@pve` hat kein Passwort |
| Eigenes Monitoring-Tool, Homepage | `MONITORING_TOKEN` (falls nötig) | nur lesen (GET) | Schreib- oder Admin-Zugang |
| ntfy (Push, kommt mit dem Benachrichtigungs-Update) | eigenes Token | nur Schreiben in das eine Topic | Lesen anderer Topics, Admin |

Zugangsdaten stehen nur in `/etc/missa/missa.env` (Rechte `640`, Gruppe `missa`), nie im Repo.

## Für Entwickler

- Stufe und Rückfrage prüft `Werkzeugkasten.ausfuehren` – Werkzeuge müssen dafür nichts tun.
  Hat ein zerstörerisches Werkzeug einen Parameter `bestaetigt`, wird es für die Rückfrage
  mit `bestaetigt=False` aufgerufen (es meldet dann nur, was passieren würde) und nach dem Ja
  mit `bestaetigt=True`. Alternativ beschreibt `vorschau=` die Aktion.
- Aktionen außerhalb von Werkzeugen (Senden-Sperre, Pausen im Hintergrund) prüfen
  `rechte.notfall_aktiv()` selbst und tragen sich mit `rechte.protokollieren(...)` ein.
- Kommandozeile für die Scripts: `python -m missa.core.rechte stopp|freigabe|status`.
