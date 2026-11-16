# Architektur

Wie Missa-Core aufgebaut ist: welche Teile es gibt, was bei einer Nachricht passiert und
wo die Daten liegen. Für die Bedienung einzelner Funktionen siehe die übrigen Seiten in
[docs/](README.md); wie man selbst etwas ergänzt, steht in [entwicklung.md](entwicklung.md).

## Das Gesamtbild

Missa besteht aus drei Repos. Die ganze Logik liegt in Missa-Core; die anderen beiden
verbinden sich nur über Schnittstellen und verändern Missa-Core nie.

| Repo | Aufgabe | Läuft auf |
|------|---------|-----------|
| [Missa-Core](https://github.com/Louidos/Missa-Core) (dieses Repo) | Missa selbst: Charakter, Gedächtnis, Werkzeuge, Zeitplaner, Kanäle, OpenAI-kompatible API | ai01, systemd-Dienst `missa-core` |
| [missa-control](https://github.com/Louidos/missa-control) | Admin-Panel: Integrationen, Freigaben, Regeln, Geräte, Modelle – stellt Missa zusätzliche Funktionen über ein MCP-Gateway bereit | ai01, Docker Compose |
| [missa-chat](https://github.com/Louidos/missa-chat) | Chat-App (PWA) für Browser und Handy – nur ein Zugang zur API | beliebiger Webserver |

```text
   missa-chat (Browser/Handy)      andere Apps (OpenAI-kompatibel)      Nextcloud Talk
             │ HTTPS, Bearer-Key               │                           │ Webhook (HMAC)
             └──────────────► rp01 (Caddy) ◄───┘                           │
                                  │                                        │
                                  ▼                                        ▼
                     ┌──────────────────────── Missa-Core (ai01:8000) ──────────────────────┐
                     │  Schutz → FastAPI → ChatDienst ─► Werkzeugkasten ─► Rechte-Schicht   │
                     │                        │              │                              │
                     │              Zeitplaner, Hintergrund, Zustellung, Alarme             │
                     └────────┬───────────────┬───────────────┬───────────────┬─────────────┘
                              ▼               ▼               ▼               ▼
                       llama-server     Nextcloud        Homelab-Dienste   Missa Control
                       (Sprachmodell)   (CalDAV, CardDAV, (Proxmox, SSH,     (/mcp, /api/events,
                                        WebDAV, Notes,     Monitoring, Caddy, /api/config)
                                        Talk)              Git, ZyWALL-Log)
```

## Aufbau des Codes

Alles liegt im Python-Paket `missa` unter `src/missa/`.

| Paket | Inhalt |
|-------|--------|
| `missa.core` | Das Gerüst: API (`app.py`), Gesprächslogik (`chat.py`), Systemprompt (`prompt.py`), Werkzeugkasten (`werkzeuge.py`), Rechte-Schicht (`rechte.py`), Schutz-Middleware (`schutz.py`), Konfiguration (`config.py`), Datenbank (`datenbank.py`), Verlauf (`verlauf.py`), Zeitplaner (`zeitplaner.py`), Hintergrund-Aufträge (`hintergrund.py`), Zustellung (`zustellung.py`), Alarme (`alarme.py`), Sicherung (`sicherung.py`), im Gespräch änderbare Einstellungen (`vorgaben.py`), Zeitangaben aus Sprache (`zeitangaben.py`), Client für llama-server (`llama.py`) |
| `missa.tools` | Die Werkzeuge, die das Modell aufrufen kann – ein Modul pro Bereich (Kalender, Aufgaben, Gedächtnis, Mail, VMs …). `tools/__init__.py` baut daraus mit `werkzeuge_laden()` den Werkzeugkasten, je nachdem, was konfiguriert ist. |
| `missa.integrationen` | Anbindungen an fremde Dienste ohne eigene Werkzeuge: Proxmox, SSH, Docker, apt, Monitoring, Domains/RDAP, Git-Server, GitHub, RCON, ZyWALL-Logs, Missa Control |
| `missa.channels` | Kanäle neben der API – heute Nextcloud Talk (`nextcloud_talk.py`) |
| `missa.charaktertest` | Charaktertest gegen das echte Modell (`scripts/charaktertest`) |

Daneben im Repo:

| Ordner | Inhalt |
|--------|--------|
| `config/` | Vorlagen: `missa.env.example`, `charakter.example.md`, Prompt-Vorlage für Claude Code, rsyslog-Beispiel für die ZyWALL |
| `deploy/` | systemd-Unit `missa-core.service` |
| `scripts/` | Update, Sicherung, Wiederherstellen, Notfall-Stopp/-Freigabe, Talk-Bot einrichten, Prüfen |
| `scripts/einrichten/` | Einmalige Einrichtung auf anderen Hosts (Benutzer `missa`, Proxmox-Tokens) |
| `playbooks/` | Ansible-Playbooks für neue VMs (`katalog.yml` sagt, was Missa einrichten darf) |
| `tests/` | pytest; Fakes für CalDAV, Nextcloud-HTTP, SSH und Missa Control |

## Start des Dienstes

`missa-core` (Einstieg `missa.core.__main__:main`) liest die Einstellungen
(`Einstellungen.aus_umgebung()`, siehe [installation.md](installation.md)), prüft sie
(`pruefen()` – ohne `MISSA_API_KEY` oder mit dem Beispielschlüssel startet nichts) und
startet uvicorn mit `create_app()`.

`create_app()` in `core/app.py` baut alles zusammen:

1. Datenbank, Verlauf, Gedächtnis, Rechte-Schicht mit Aktionsprotokoll und Notfall-Schalter
2. Zeitplaner, Hintergrund-Warteschlange, Zustellung (Kanäle für Nachrichten von Missa aus)
3. Pro konfiguriertem Bereich die Dienste (Nextcloud, Mail, SSH, Proxmox …) und ihre
   regelmässigen Aufträge
4. Den Werkzeugkasten mit `werkzeuge_laden()` – nur Werkzeuge, deren Dienst eingerichtet ist
5. Den `ChatDienst` mit Kontextquellen, Eingangsprüfern und Nachbearbeitern
6. Die Routen der API, den Talk-Kanal und die Schutz-Middleware

Beim Hochfahren (Lifespan) starten Zeitplaner, Hintergrund-Task und – falls eingerichtet –
das Abholen der Ereignisse aus Missa Control.

## Was bei einer Nachricht passiert

```text
POST /v1/chat/completions
  │
  ├─ 1. Schutz (ASGI-Middleware, vor FastAPI)
  │       Bearer-Key prüfen, bevor der Body gelesen wird · max. 20 MB · Sperre nach Fehlversuchen
  ├─ 2. ChatDienst.vorbereiten
  │       Sitzung aus X-Missa-Session · letzte 20 Nachrichten aus dem Verlauf
  │       Systemprompt = Charakter + Datum/Uhrzeit + Werkzeugliste + Kontextquellen
  ├─ 3. Eingangsprüfer (z. B. „Notfall-Stopp“ erkennen, „Ja, abschicken“ für einen Mail-Entwurf)
  ├─ 4. Werkzeug-Schleife (höchstens 5 Runden)
  │       llama-server antwortet ─► tool_calls? ─► Werkzeugkasten.ausfuehren ─► Ergebnis zurück
  ├─ 5. Nachbearbeiter (z. B. genaue Anzeige eines Mail-Entwurfs anhängen)
  └─ 6. Frage und Antwort im Verlauf speichern · Antwort (oder Streaming als SSE) an den Client
```

Einzelheiten:

- **Systemprompt** (`core/prompt.py`): Die Charakterdatei wird bei jeder Anfrage neu gelesen,
  Änderungen wirken also sofort. Dazu kommen Datum, Wochentag, Uhrzeit und die nächsten Tage
  (für „am Freitag“), die Beschreibung der Werkzeuge und die Blöcke der **Kontextquellen**:
  Notfall-Hinweis, passende Gedächtnis-Einträge, anstehende Erinnerungen, neue Ergebnisse der
  Nachtschicht, aktive Server-Alarme, Ausfälle, Homelab-Stand, Kontaktpflege, Doku-Treffer,
  geladene Dokumente, offene Mail-Entwürfe – je nachdem, was eingerichtet ist.
- **Durchgereichte Parameter**: `temperature`, `top_p`, `max_tokens`, `stop`, `seed` und
  ähnliche Felder gehen unverändert an llama-server (Liste `DURCHGEREICHT` in `chat.py`).
- **Zu grosse Anfrage**: Lehnt llama-server die Anfrage als zu gross ab, kürzt Missa einmal
  den älteren Verlauf und lange Werkzeug-Ergebnisse (auf 1500 Zeichen) und versucht es erneut.
- **Kanäle**: Nextcloud Talk benutzt genau denselben `ChatDienst` – derselbe Charakter,
  dasselbe Gedächtnis, dieselben Werkzeuge. Nur die Zugriffsprüfung (Signatur, erlaubte
  Accounts) passiert im Kanal selbst ([nextcloud-talk.md](nextcloud-talk.md)).
- **Hintergrund**: Routinen und Nachtaufgaben laufen über `ChatDienst.hintergrund_ausfuehren`
  – wie ein Gespräch, aber ohne Louis (siehe unten).

## Werkzeuge und Rechte

Ein Werkzeug (`core/werkzeuge.py`, Klasse `Werkzeug`) hat einen Namen, eine Beschreibung für
das Modell, ein JSON-Schema der Parameter, eine Funktion und genau eine **Stufe**:

| Stufe | Bedeutung | Ablauf |
|-------|-----------|--------|
| `lesen` | schaut nur nach | läuft sofort |
| `schreiben` | legt an oder ändert (harmlos oder rückgängig machbar) | läuft sofort – ausser nach fremden Daten (siehe unten) |
| `zerstoererisch` | löscht, versendet, startet neu, ist nicht zurückzunehmen | erst Rückfrage, dann nur nach Louis' ausdrücklichem Ja zu genau dieser Aktion (gilt 10 Minuten) |

Jeder Aufruf geht durch `Werkzeugkasten.ausfuehren` und damit durch die Rechte-Schicht
(`core/rechte.py`):

1. Parameter prüfen (gültiges JSON, passende Namen) – sonst Fehlertext ans Modell
2. Notfall-Stopp aktiv? Dann läuft nur noch `lesen`.
3. Zerstörerisch – oder schreibend, nachdem in dieser Anfrage schon **fremde Daten**
   gelesen wurden (Mails, Dateien, Notizen, Logs, Git …, Liste `FREMDE_QUELLEN`)? Dann
   Rückfrage. Bestätigen kann nur Louis' neue Nachricht, nie Text des Modells oder eines
   Werkzeugs. Im Hintergrund gibt es keine Rückfrage – dort wird so etwas abgelehnt.
4. Ausführen, Ergebnis aus fremden Quellen markieren (`fremd_markieren`), damit
   eingeschleuster Text nicht als Anweisung gilt
5. Alles ins **Aktionsprotokoll** – auch abgelehnte Aufrufe, Geheimnisse geschwärzt

Bedienung, Notfall-Stopp und Rechte pro Dienst: [rechte.md](rechte.md). Die vollständige
Liste aller Werkzeuge mit Stufe: [werkzeuge.md](werkzeuge.md).

**Funktionen aus Missa Control** kommen als eigene Werkzeug-Gruppe dazu
(`tools/panel.py`): Die Liste wird beim Start und danach regelmässig vom MCP-Gateway geholt
und mit `Werkzeugkasten.gruppe_ersetzen` ausgetauscht – ohne Neustart. Ihre Stufe ist
`lesen` oder `schreiben` (aus `readOnlyHint`), nie `zerstoererisch`: Über Rückfragen
entscheidet das Panel mit seinen Freigaben ([missa-control.md](missa-control.md)).

## Zeitplaner, Hintergrund und Zustellung

Missa tut auch etwas, ohne gefragt zu werden. Dafür gibt es drei Bausteine:

- **Zeitplaner** (`core/zeitplaner.py`): Ein Task schläft bis zur nächsten fälligen
  Erinnerung (höchstens 30 Sekunden am Stück) und stellt sie zu. Daneben führt er
  **tägliche** Aufträge (einmal pro Tag ab einer Uhrzeit, nach einem Neustart nachgeholt) und
  **regelmässige** Aufträge (alle *n* Minuten, letzter Lauf in der Datenbank) aus.
- **Hintergrund** (`core/hintergrund.py`): Warteschlange für Routinen und Nachtaufgaben.
  Ein eigener Task gibt sie **einzeln nacheinander** an Missa, damit der Chat auf der CPU nicht
  blockiert. `sofort`-Aufträge (fällige Routinen) laufen, sobald der Task frei ist,
  `nacht`-Aufträge nur im Nachtfenster (Standard 01:00–06:00). Am Ende des Fensters kommt
  der Morgenbericht.
- **Zustellung** (`core/zustellung.py`): Alles, was Missa von sich aus schickt, geht über
  eine Liste von Kanälen, die der Reihe nach probiert werden. Heute: Nextcloud Talk. Ein
  späteres Telefon oder Push kommt einfach als weiterer Kanal dazu.
- **Alarme** (`core/alarme.py`): Meldungen mit Stufe (`info`, `wichtig`, `notfall`), Ruhezeit
  und Ruhefenster gegen Wiederholungen ([server-alarme.md](server-alarme.md)).

Diese Aufträge registriert `create_app()` – nur, wenn der jeweilige Bereich eingerichtet ist:

| Auftrag | Art | Standard | Einstellung |
|---------|-----|----------|-------------|
| `sicherung` | täglich | 03:30 | `MISSA_BACKUP_ZEIT` |
| `doku-index` | täglich | 02:30 | `DOKU_INDEX_ZEIT` |
| `kontaktpflege` | täglich | 08:00 | `KONTAKTE_PRUEFZEIT` |
| `pausen` | täglich | 19:00 | `PAUSEN_PRUEFZEIT` |
| `updates` | täglich | 06:40 | `UPDATES_PRUEFZEIT` |
| `zertifikate` | täglich | 07:10 | `ZERTIFIKATE_PRUEFZEIT` |
| `firewall-bericht` | täglich | 07:30 | `FIREWALL_BERICHT_ZEIT` |
| Morgenbericht der Nachtschicht | täglich | Ende des Nachtfensters | im Gespräch („Nachtschicht von 2 bis 5“) |
| `server-monitoring` | regelmässig | 60 s | `MONITORING_INTERVALL_SEKUNDEN` |
| `erreichbarkeit` | regelmässig | 5 min | `ERREICHBARKEIT_INTERVALL_MINUTEN` |
| `container` | regelmässig | 5 min | `CONTAINER_INTERVALL_MINUTEN` |
| `anmeldungen` | regelmässig | 5 min | `LOGIN_INTERVALL_MINUTEN` |
| `homelab` | regelmässig | 60 min | `HOMELAB_INTERVALL_MINUTEN` |
| `firewall-einlesen` | regelmässig | 1 min | – |
| `github` | regelmässig | 30 min | `GITHUB_INTERVALL_MINUTEN` |
| `git-alte-branches` | regelmässig | 7 Tage | – |
| `missa-control` | regelmässig | 60 s | `MISSA_CONTROL_AKTUALISIEREN` |

Alle Variablen sind in [installation.md](installation.md) erklärt.

## Daten

| Was | Wo (Standard) | Gesichert |
|-----|---------------|-----------|
| Konfiguration | `/etc/missa/missa.env` (`MISSA_ENV_DATEI`) | ja, ohne Geheimnisse |
| Charakter | `/etc/missa/charakter.md` (`MISSA_CHARAKTER_DATEI`) | ja |
| Datenbank | `/opt/missa/data/missa.db` (`MISSA_DATEN`) | ja |
| Schreibstil-Profil | `MISSA_DATEN/schreibstil.md` | ja |
| Notfall-Stopp | Datei `MISSA_DATEN/notfall-stopp` (existiert = aktiv) | – |
| Backups | `MISSA_DATEN/backup/` (`MISSA_BACKUP_ORDNER`) | – |
| Key für llama-server | `/etc/missa/llama.key` (`LLAMA_API_KEY_FILE`) | nein |
| SSH-Schlüssel für die Hosts | `/etc/missa/ssh/` | nein |

Was genau in ein Backup kommt: [sicherung.md](sicherung.md).

### Tabellen in `missa.db`

Eine SQLite-Datei für alles. Jedes Modul legt seine Tabellen selbst an
(`CREATE TABLE IF NOT EXISTS`) und ergänzt fehlende Spalten beim Start – es gibt kein
separates Migrations-Werkzeug. Der gemeinsame Zugriff läuft über `core/datenbank.py`.

| Tabelle | Modul | Inhalt |
|---------|-------|--------|
| `nachrichten` | `core/verlauf.py` | Gesprächsverlauf pro Sitzung |
| `fakten` (+ FTS5-Index) | `tools/gedaechtnis.py` | Langzeitgedächtnis mit Volltextsuche |
| `vorgaben` | `core/vorgaben.py` | im Gespräch geänderte Einstellungen |
| `stand` | `core/datenbank.py` | interne Merkwerte (z. B. letzte Ereignis-Nummer) |
| `aktionsprotokoll` | `core/rechte.py` | jeder Werkzeug-Aufruf |
| `erinnerungen` | `core/zeitplaner.py` | Erinnerungen und Routinen |
| `zeitplaner_auftraege` | `core/zeitplaner.py` | letzter Lauf der täglichen/regelmässigen Aufträge |
| `hintergrund_auftraege` | `core/hintergrund.py` | Routinen- und Nachtschicht-Aufträge mit Ergebnis |
| `alarme` | `core/alarme.py` | gemeldete Alarme |
| `alarm_regeln`, `messwert_stunden` | `tools/server_alarme.py` | Alarm-Regeln, Stundenwerte für „ungewöhnlich hoch“ |
| `kalender_aenderungen` | `tools/kalender_protokoll.py` | Kalender-Protokoll für Rückgängig |
| `kalender_farbsystem` | `tools/farben.py` | Kategorie → Farbe |
| `kontakte_pflege` | `tools/kontakte.py` | wichtige Kontakte, letzter Kontakt |
| `mail_entwuerfe` | `tools/mail.py` | offene E-Mail-Entwürfe |
| `doku_dateien` | `tools/doku.py` | Index der eigenen Doku |
| `dokumente` | `tools/dokumente.py` | geladene Dokumente für Rückfragen |
| `erreichbarkeit` | `tools/erreichbarkeit.py` | Zustand der Domains |
| `anmeldungen`, `login_ips`, `login_stand` | `tools/anmeldungen.py` | Login-Überwachung |
| `updates_stand`, `updates_verlauf`, `wartungsfenster` | `tools/updates.py` | Updates pro VM |
| `dns_zustand` | `tools/zertifikate.py` | DNS-Abgleich |
| `inventar`, `inventar_stand` | `tools/homelab.py` | VMs und IPs aus Proxmox |
| `firewall_ereignisse`, `firewall_stand` | `tools/firewall.py` | eingelesene ZyWALL-Logs |
| `vm_plaene`, `vm_auftraege` | `tools/vms.py` | geplante und erstellte VMs |
| `github_gesehen` | `tools/git.py` | schon gemeldete Builds, Releases, Issues |
| `claude_prompts` | `tools/prompts.py` | Prompt-Entwürfe |

## Sicherheit auf einen Blick

- **Anmeldung vor allem anderen**: Die Schutz-Middleware liest ohne gültigen Key nicht einmal
  den Body. Offen sind nur `/health` (ohne Anmeldung ohne Fehlertexte) und der Talk-Webhook (eigene
  HMAC-Signatur). `/docs` ist abgeschaltet.
- **Nichts Zerstörerisches ohne Ja**, nichts Zerstörerisches im Hintergrund, Notfall-Stopp
  im Code (aufheben nur auf ai01).
- **Fremde Daten sind Daten, keine Anweisungen**: markiert und mit Rückfrage vor dem nächsten
  Schreiben.
- **Geheimnisse** werden im Aktionsprotokoll, im Dienst-Log und im Backup geschwärzt bzw.
  weggelassen.
- **Enge Rechte nach aussen**: SSH nur als Benutzer `missa` mit fest erlaubten Befehlen,
  Proxmox schreibend nur im Pool `missa`, GitHub nur lesend, Firewall nur über Logs, Mails
  nur nach „Ja, abschicken“ (keine Senden-Funktion für das Modell).
- **Prüfungen in der CI**: ruff (inkl. Sicherheitsregeln `S`), shellcheck, pip-audit,
  gitleaks und alle Tests.
