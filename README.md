# Missa-Core

Missa ist eine persönliche KI-Assistentin. Missa-Core ist der zentrale Dienst:
Er spricht als Missa, führt Werkzeuge aus (z. B. Kalender, Aufgaben, Gedächtnis)
und leitet Anfragen an ein lokal laufendes Sprachmodell (llama-server) weiter.
Jede App verbindet sich nur mit der API von Missa-Core; weitere Kanäle wie Nextcloud
Talk nutzen dieselbe Logik.

## Projektstand

- Version: 0.8.0 (2026-11-12)
- Fertig: OpenAI-kompatible API (`/v1/chat/completions`, `/v1/models`, `/health`)
  mit Charakter, Werkzeug-Schleife und Gesprächsverlauf
- Fertig: Nextcloud-Kalender mit voller Kontrolle: Termine anlegen, ändern, verschieben und
  löschen (diese beiden nur nach Ja), Doppelbuchungen und zu knappe Ortswechsel werden vor
  dem Eintragen erkannt, Farben, Kategorien, Erinnerungen, Serien mit Ausnahmen, eigene Kalender –
  alles protokolliert und rückgängig machbar; Einladungen nur nach Rückfrage
  (siehe docs/kalender.md)
- Fertig: Nextcloud-Aufgaben (anzeigen, erstellen, abhaken, nach Bestätigung löschen)
- Fertig: Gedächtnis (merken, erinnern, vergessen; passende Fakten automatisch im Kontext;
  Übersicht unter `GET /api/gedaechtnis`)
- Fertig: systemd-Dienst, Installationsanleitung und API-Doku
- Fertig: Update mit einem Befehl (`sudo ./scripts/update.sh`, mit automatischem Rücksprung)
- Fertig: Nextcloud Talk als Kanal (nur für erlaubte Accounts, siehe docs/nextcloud-talk.md);
  Missa kann dort auch von sich aus schreiben (z. B. Erinnerungen)
- Fertig: Erinnerungen auf die Minute genau, auch wiederkehrend („jeden Montag um 7“),
  mit Nachholen nach einem Neustart (siehe docs/erinnerungen.md)
- Fertig: Routinen per Gespräch („jeden Freitag um 17 Uhr Wochenrückblick“) und Nachtschicht
  für lange Aufgaben (01:00–06:00, Ergebnis am Morgen), alles im Hintergrund, ohne den Chat
  zu blockieren (siehe docs/routinen-nachtschicht.md)
- Fertig: E-Mail-Entwürfe im eigenen Schreibstil (IMAP/SMTP), gesendet wird nur nach
  ausdrücklichem Ja – die Senden-Sperre steckt im Code (siehe docs/email.md)
- Fertig: Geburtstage aus Nextcloud-Kontakte (vorher und am Tag) und Kontaktpflege für
  wichtige Personen mit Vorschlag eines freien Zeitpunkts (siehe docs/kontakte.md)
- Fertig: Pausen und Puffer – Missa prüft den Kalender jeden Abend und bei jeder neuen
  Terminanlage (Standard: 15 Min. Puffer, Pause nach 3 Std.), schlägt Puffer- und
  Pausenblöcke vor und trägt sie erst nach deinem Ja ein; Regeln per Gespräch änderbar
  (siehe docs/pausen.md)
- Fertig: Rechte-Schicht – jedes Werkzeug hat eine Stufe (lesen, schreiben, zerstörerisch),
  Zerstörerisches nur nach deinem ausdrücklichen Ja (gilt 10 Minuten), lückenloses
  Aktionsprotokoll (`GET /api/aktionen`, „Was hast du heute gemacht?“) und Notfall-Stopp per
  Chat oder `scripts/notfall-stopp.sh`, aufheben nur auf ai01 (siehe docs/rechte.md)
- Fertig: Server-Alarme – Regeln per Gespräch („Ruf mich an, wenn ai01 über 90 % ist“), Stufen
  `info`/`wichtig`/`notfall` mit Ruhezeit und Ruhefenster, Hinweise auf ungewöhnliche Anstiege
  unter der Grenze und Prüfung der eigenen Domains von außen; Werte aus dem eigenen
  Monitoring-Tool, Homepage und Proxmox, alles nur lesend (siehe docs/server-alarme.md)
- Fertig: Container und Logs – „Welche Container laufen auf wb01?“, hängende Container
  melden, Neustart nur nach Ja, „Warum geht Nextcloud nicht?“ mit verständlicher Ursache aus
  den Logs und Login-Überwachung für SSH und Nextcloud (neue IPs, Fehlversuche, ungewohnte
  Zeiten); Zugang über den Benutzer `missa` mit eng begrenzten Rechten
  (siehe docs/container-logs-logins.md)
- Fertig: Updates und Zertifikate – „Welche Updates stehen an?“ pro VM mit markierten
  Sicherheitsupdates, Wartungsfenster-Vorschlag im Kalender (eingetragen erst nach Ja),
  Einspielen und Neustart VM für VM nur nach Ja (ai01 zuletzt), Warnungen 21 und 7 Tage vor
  dem Ablauf von Zertifikaten und Domains (RDAP) und DNS-Abgleich
  (siehe docs/updates-zertifikate.md)
- Fertig: Homelab-Wissen und Firewall-Logs – Missa kennt alle VMs mit IP und VLAN aus
  Proxmox („Welche IP hat rp01?“), meldet neue VMs und geänderte IPs und fasst die
  ZyWALL-Logs täglich zusammen (nur lesend, siehe docs/homelab-firewall.md)
- Fertig: Proxmox-VMs – „Richte einen Minecraft-Server ein“ zeigt einen Plan, nach Ja
  klont Missa eine VM aus dem Cloud-Init-Template und richtet sie per Ansible ein; harte
  Grenzen pro VM und Pool, Stoppen und Löschen nur im eigenen Pool
  (siehe docs/proxmox-vms.md)
- Fertig: Caddy und Minecraft – Missa bereitet Subdomains in Caddy vor (aktiv erst nach
  Ja, ungültige Einträge nimmt sie sofort zurück, Caddy läuft unverändert weiter) und
  verwaltet Minecraft per RCON: „Wer ist online?“, Whitelist, Neustart mit Warnung
  (siehe docs/caddy-minecraft.md)
- Fertig: Git und GitHub – Issues auf dem eigenen Git-Server (Gitea/Forgejo) anlegen,
  offene Pull Requests zusammenfassen, alte Branches wöchentlich melden; GitHub nur
  lesend (fehlgeschlagene Builds, neue Releases und Issues); Claude-Code-Prompts aus
  groben Ideen nach Vorlage, als Issue erst nach Ja (siehe docs/git-github-prompts.md)
- Später als eigene Aufträge: Telefon, Push, Smart Home
- Änderungen im Detail: [CHANGELOG.md](CHANGELOG.md)

## Dokumentation

- **[docs/installation.md](docs/installation.md)** – Installation auf ai01 als systemd-Dienst
  unter dem Benutzer `missa`, alle Variablen erklärt
- **[docs/api.md](docs/api.md)** – alle Endpunkte mit curl-Beispielen und wie sich eine App
  verbindet (Basis-URL, Bearer-Token, Modell `missa`, Header `X-Missa-Session`)
- **[docs/update.md](docs/update.md)** – Update auf ai01 mit `sudo ./scripts/update.sh`,
  einmalige Ersteinrichtung, Rücksprung bei Fehlern
- **[docs/nextcloud-talk.md](docs/nextcloud-talk.md)** – Missa in Nextcloud Talk: Einrichtung,
  erlaubte Accounts, occ-Befehle, Firewall
- **[docs/kalender.md](docs/kalender.md)** – Kalender: ändern, Farben, Serien, eigene
  Kalender, Protokoll und Rückgängig, Einladungen
- **[docs/email.md](docs/email.md)** – E-Mail-Entwürfe im eigenen Stil: Einrichtung
  (`MAIL_*`), Senden-Sperre, Schreibstil-Profil
- **[docs/kontakte.md](docs/kontakte.md)** – Geburtstage und Kontaktpflege
- **[docs/pausen.md](docs/pausen.md)** – Pausen und Puffer im Kalender, Grenzwerte
- **[docs/erinnerungen.md](docs/erinnerungen.md)** – Erinnerungen: was Missa versteht,
  Wiederholungen, Nachholen nach einem Neustart
- **[docs/rechte.md](docs/rechte.md)** – Rechte-Stufen, Bestätigung, Aktionsprotokoll,
  Notfall-Stopp und welche Rechte jeder Dienst braucht
- **[docs/routinen-nachtschicht.md](docs/routinen-nachtschicht.md)** – Routinen per Gespräch,
  Nachtschicht, Morgenbericht, Sicherheitsregeln im Hintergrund
- **[docs/server-alarme.md](docs/server-alarme.md)** – Server-Alarme: Stufen, Ruhezeit, Regeln
  per Gespräch, Hinweise auf Auffälliges, Erreichbarkeit, nur lesendes Proxmox-Token
- **[docs/monitoring.md](docs/monitoring.md)** – Schnittstelle und JSON-Formate der
  Monitoring-Quellen
- **[docs/netzfreigaben.md](docs/netzfreigaben.md)** – welche Verbindungen ai01 braucht
- **[docs/container-logs-logins.md](docs/container-logs-logins.md)** – Container, Fehlersuche
  in Logs, Login-Überwachung und der Benutzer `missa` auf den Hosts
- **[docs/proxmox-vms.md](docs/proxmox-vms.md)** – VMs auf Zuruf erstellen und einrichten
- **[docs/caddy-minecraft.md](docs/caddy-minecraft.md)** – Subdomains in Caddy nach Ja,
  Minecraft per RCON
- **[docs/git-github-prompts.md](docs/git-github-prompts.md)** – Git-Server, GitHub
  (nur lesend) und Claude-Code-Prompts aus groben Ideen
- **[docs/homelab-firewall.md](docs/homelab-firewall.md)** – Homelab-Wissen und
  Firewall-Logs
- **[docs/updates-zertifikate.md](docs/updates-zertifikate.md)** – Updates, Wartungsfenster,
  Zertifikate, Domains und DNS

## Installation

Für den Betrieb auf ai01: siehe [docs/installation.md](docs/installation.md). Kurz:
Code nach `/opt/missa/app`, Python-Umgebung `/opt/missa/venv`, Konfiguration in
`/etc/missa/missa.env`, Charakter in `/etc/missa/charakter.md`, Dienst aus
[deploy/missa-core.service](deploy/missa-core.service).

Für die Entwicklung (Python 3.13, Linux):

```bash
python3.13 -m venv .venv
```

```bash
. .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

## Update

Auf ai01 (Einzelheiten und einmalige Ersteinrichtung: [docs/update.md](docs/update.md)):

```bash
sudo /opt/missa/app/scripts/update.sh
```

Nur anschauen, was passieren würde: `./scripts/update.sh --dry-run`.

## Start

Im Betrieb startet systemd den Dienst:

```bash
sudo systemctl enable --now missa-core
```

Von Hand liest `missa-core` seine Einstellungen aus Umgebungsvariablen und zusätzlich aus
`/etc/missa/missa.env` (anderer Ort: `MISSA_ENV_DATEI`; schon gesetzte Variablen haben
Vorrang). Vorlage: [config/missa.env.example](config/missa.env.example),
Beispiel-Charakter: [config/charakter.example.md](config/charakter.example.md).

```bash
MISSA_API_KEY=geheim MISSA_CHARAKTER_DATEI=config/charakter.example.md MISSA_DATEN=./data missa-core
```

Ohne `MISSA_API_KEY` startet der Dienst nicht. Kurztest:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer geheim" -H "Content-Type: application/json" \
  -d '{"model": "missa", "messages": [{"role": "user", "content": "Hallo Missa!"}]}'
```

## Prüfen

```bash
scripts/pruefen.sh
```

Das Skript führt `ruff check`, `ruff format --check` und `pytest` aus – genau wie die CI.

## Aufbau

| Ordner     | Inhalt                                                    |
|------------|-----------------------------------------------------------|
| `src/missa/` | Programmcode (Python-Paket)                             |
| `tests/`   | Tests (pytest)                                            |
| `scripts/` | Hilfsskripte                                              |
| `scripts/einrichten/` | Einrichtungs-Scripts für andere Hosts (von Hand ausführen) |
| `playbooks/` | Ansible-Playbooks zum Einrichten neuer VMs (mit Katalog) |
| `docs/`    | Dokumentation                                             |
| `config/`  | Beispiel-Konfiguration (keine echten Geheimnisse!)        |
| `deploy/`  | systemd-Unit                                              |

## Lizenz

MIT, siehe [LICENSE](LICENSE).
