# Sicherheit

Was an Missa-Core auf Sicherheit geprüft wurde, welche Lücken behoben sind, welche
Schutzmassnahmen es gibt und was du als Besitzer selbst tun musst. Hier stehen nur Lücken,
die schon behoben sind. Eine neue Lücke meldest du vertraulich, siehe
[../.github/SECURITY.md](../.github/SECURITY.md).

Stand: Sicherheitsprüfung vom 2026-11-16 (nach Version 0.10.0, PRs #85–#98).

## Was geprüft wurde

### Prüfwerkzeuge und Geheimnisse (#85, #98)

- **pip-audit** prüft die Abhängigkeiten auf bekannte Lücken, in der CI und in
  `scripts/pruefen.sh`. Ergebnis der Prüfung: keine bekannten Lücken.
- **gitleaks** sucht in der CI in der ganzen Historie des geprüften Stands und in allen
  Tags nach eingecheckten Passwörtern und Schlüsseln. Ausgenommen sind nur die erfundenen
  Platzhalter in `.gitleaks.toml`.
- **Einmalige Suche über alle 78 Commits aller Branches und Tags:** kein echtes Geheimnis
  gefunden. Der einzige Treffer war der Platzhalter `DEIN_MISSA_API_KEY` in der Doku.
- **Dependabot** schlägt wöchentlich neue Versionen für `pip` und GitHub Actions vor.
- Der CI-Workflow hat nur Leserechte (`contents: read`). Die Actions sind auf feste Commits
  gesetzt, und es werden keine Zugangsdaten im Checkout gespeichert.

### Der Code, Punkt für Punkt (#89, #91)

| Nr. | Bereich | Ergebnis |
|---|---|---|
| 1 | API-Zugang (`core/app.py`, `core/schutz.py`) | Funde behoben (siehe unten). Jede Route ausser `/health` und dem Talk-Webhook verlangt den Bearer-Token; der Vergleich läuft in konstanter Zeit. |
| 2 | Nextcloud Talk (`channels/nextcloud_talk.py`) | Funde behoben. Die Signatur (HMAC) wird am rohen Inhalt geprüft, bevor etwas verarbeitet wird. Nur erlaubte Accounts lösen etwas aus. |
| 3 | Eingeschleuste Anweisungen | Funde behoben, siehe „Fremde Inhalte“ in [rechte.md](rechte.md). |
| 4 | Rechte-Schicht (`core/rechte.py`, `core/werkzeuge.py`, `tools/bestaetigung.py`) | Funde behoben. Jedes Werkzeug hat eine Stufe und läuft durch die Rechte-Schicht. |
| 5 | Mail (`tools/mail.py`, `tools/mail_konto.py`) | Die Senden-Sperre lässt sich nicht umgehen. Zwei kleinere Funde bei Empfängern und Ordnernamen sind behoben. |
| 6 | Befehle und SSH | Feste Liste erlaubter Ziele und Befehle, `--` vor dem Befehl, kein `shell=True`, `StrictHostKeyChecking=yes`. Ein kleinerer Fund ist behoben. |
| 7 | SQL | Alle zusammengesetzten Statements geprüft: Namen kommen aus dem Code, Werte immer als Parameter. Zusätzlich gibt es jetzt eine gemeinsame, geprüfte Hilfsfunktion (`core/datenbank.py`, #96). Ein kleiner Fund beim Vergessen ist behoben. |
| 8 | Abrufe ins Netz | Funde behoben. Die TLS-Prüfung ist nirgends fest abgeschaltet. |
| 9 | Dateien | Schreiben geht nur in `Missa/`. Das Wiederherstellen entpackt nichts ausserhalb des Zielordners (`filter="data"`). Funde beim Lesen und bei der PDF-Grösse sind behoben. |
| 10 | Geheimnisse zur Laufzeit | Aktionsprotokoll und Backups waren schon geschwärzt. Behoben: das Dienst-Log und die Dateirechte. |
| 11 | Anbindungen mit viel Macht | Token stehen nur im Header, die Rechte sind minimal (Proxmox-Pool, Lese-Token). Behoben: Proxmox-Antworten werden jetzt geprüft. |
| 12 | ruff-Regelgruppe `S` | Dauerhaft aktiv. Jede Ausnahme steht mit Begründung als `noqa` in der Zeile. |

### Betrieb (#93)

- **systemd-Unit:** zusätzliche Absicherungen. `systemd-analyze security` bewertet sie
  jetzt mit etwa 1.4 statt 7.4.
- **Scripts mit root-Rechten:** `update.sh`, `wiederherstellen.sh`, `sichern.sh`,
  `notfall-*.sh` und `einrichten/*` geprüft, Funde behoben.
- **Playbooks und Templates:** keine Passwörter im Repo. RCON ist nur von ai01 erreichbar
  und hat ein zufälliges Passwort.
- **Beispiel-Konfiguration:** jede Einstellung, die der Code liest, steht in
  `config/missa.env.example`, und umgekehrt.

## Behobene Lücken

Einstufung: **Kritisch**, **Hoch**, **Mittel** oder **Niedrig**, je nachdem, was ein
Angreifer erreichen konnte und wie leicht.

| Einstufung | Was war möglich | Datei | Behoben in |
|---|---|---|---|
| Kritisch | Ein Dateipfad konnte eine vollständige Adresse sein. Text in einer Mail oder Webseite konnte Missa dazu bringen, eine fremde Adresse „als Datei“ zu lesen, und dabei ging das Nextcloud-App-Passwort an diesen Host. | `tools/dateien.py` | #89 |
| Hoch | Missa startete auch mit dem Beispielschlüssel `bitte-aendern` aus der Beispiel-Konfiguration. | `core/config.py` | #89 |
| Hoch | Ein „Ja“ war nicht an die Unterhaltung gebunden und konnte mehrere offene Rückfragen auf einmal freigeben, auch aus einem anderen Chat. | `core/rechte.py`, `core/chat.py` | #91 |
| Hoch | Das Modell konnte bei Werkzeugen mit eigener Rückfrage (z. B. Einladungen an andere Personen) in einer späteren Anfrage selbst „bestätigt“ setzen, ohne dein Ja. | `core/werkzeuge.py` | #91 |
| Hoch | Inhalte aus Mails, Dateien, Notizen, Logs, Webseiten und Missa Control waren nicht als fremd gekennzeichnet. Eingeschleuster Text konnte danach schreibende Werkzeuge ohne dein Ja auslösen, z. B. Alarme abschwächen oder dauerhafte „Korrekturen“ speichern. | `core/werkzeuge.py`, `core/chat.py`, `core/prompt.py` | #91 |
| Mittel | Der Inhalt einer Anfrage wurde vor der Anmeldung gelesen, ohne Grössengrenze und ohne Sperre nach Fehlversuchen. | `core/app.py`, `core/schutz.py` | #89 |
| Mittel | `/docs`, `/redoc` und `/openapi.json` waren ohne Anmeldung erreichbar. | `core/app.py` | #89 |
| Mittel | Eine abgefangene, signierte Talk-Nachricht konnte nach einem Neustart erneut ausgeführt werden. | `channels/nextcloud_talk.py` | #89 |
| Mittel | Beim Zusammenfassen von Webseiten folgte Missa Weiterleitungen auch auf interne Adressen. | `tools/dokumente.py` | #89 |
| Mittel | Grosse Webseiten wurden ganz geladen, bevor die Grösse geprüft wurde. PDFs hatten keine Seitengrenze. | `tools/dokumente.py` | #89 |
| Mittel | Der Notfall-Stopp hielt eine schon laufende VM-Erstellung nicht an. | `tools/vms.py` | #89 |
| Mittel | Ohne `VM_VERWALTUNG_IP` wurde RCON eines neuen Minecraft-Servers für alle geöffnet. | `tools/vms.py`, `playbooks/minecraft.yml` | #93 |
| Mittel | Die Notlösung des Notfall-Scripts (ohne Python) folgte als root Verknüpfungen im Datenordner. | `scripts/notfall-stopp.sh` | #93 |
| Niedrig | `/health` zeigte ohne Anmeldung Fehlertexte von Missa Control, inklusive interner Adresse. | `core/app.py` | #89 |
| Niedrig | Sonderzeichen in Kopfzeilen eines Talk-Webhooks führten zu Fehler 500. | `channels/nextcloud_talk.py` | #89 |
| Niedrig | Werkzeug-Argumente (z. B. Passwörter in Notizen) standen ungeschwärzt im Dienst-Log. | `core/chat.py` | #89 |
| Niedrig | Ein Empfänger mit Zeilenumbruch erzeugte einen irreführend angezeigten Entwurf, der nach dem Ja still nicht gesendet wurde. IMAP-Ordnernamen mit Steuerzeichen wurden nicht abgelehnt. | `tools/mail.py`, `tools/mail_konto.py` | #89 |
| Niedrig | Prüfregeln für SSH-Angaben, Namen und Spieler liessen einen Zeilenumbruch am Ende durch. | `integrationen/ssh.py` u. a. | #89 |
| Niedrig | „Vergiss alles zu `%`“ traf jede gespeicherte Nachricht. Gelöscht wurde erst nach deinem Ja. | `tools/gedaechtnis.py` | #89 |
| Niedrig | Werte aus Proxmox-Antworten landeten ungeprüft in weiteren API-Pfaden. | `integrationen/proxmox.py` | #89 |
| Niedrig | Nach dem Wiederherstellen war die Datenbank für alle lesbar. Ein Link an der Zielstelle wurde verfolgt. | `core/sicherung.py` | #89 |
| Niedrig | Die erzeugte RCON-Passwortdatei war kurz für alle lesbar. | `tools/homelab_bauen.py` | #89 |
| Niedrig | `update.sh` prüfte nicht, ob `origin` wirklich das erwartete Repo ist, und nahm bei `--version` beliebige Tag-Namen an. | `scripts/update.sh` | #93 |

Zu jeder Korrektur gibt es einen Test, der vorher rot war.

## Schutzmassnahmen im Überblick

- **Anmeldung** vor dem Lesen jeder Anfrage. Grenze 20 MB, nach 10 Fehlversuchen in
  5 Minuten 15 Minuten Sperre. Keine öffentliche API-Beschreibung. Siehe [api.md](api.md).
- **Rechte-Stufen** `lesen`, `schreiben`, `zerstoererisch`. Zerstörerisches läuft nur nach
  deinem Ja, und das Ja gilt nur:
  - in derselben Unterhaltung,
  - in der Nachricht direkt nach der Rückfrage,
  - für genau eine Aktion.

  Siehe [rechte.md](rechte.md).
- **Fremde Inhalte** sind im Gespräch mit einer nicht fälschbaren Kennung markiert. Danach
  braucht in derselben Anfrage auch Schreibendes dein Ja. Im Hintergrund wird danach nichts
  mehr geschrieben.
- **Senden-Sperre für Mails:** Eine Mail geht nur nach deinem Ja zum genau gezeigten
  Entwurf raus. Siehe [email.md](email.md).
- **Notfall-Stopp:** Er sperrt alles Schreibende sofort, auch im Hintergrund und in einer
  laufenden VM-Erstellung. Aufheben geht nur auf ai01.
- **Netz:** Webseiten werden nur abgerufen, wenn du die Adresse selbst schickst. Es gilt
  Folgendes:
  - Weiterleitungen werden geprüft.
  - Es gibt Grössengrenzen.
  - Die TLS-Prüfung bleibt an.
- **Dateien:** Schreiben geht nur in `Missa/`, Lesen nur in der eigenen Nextcloud.
- **Angeschlossene Geräte** (nur mit `MISSA_GERAETE_AKTIV=true`, siehe
  [geraete.md](geraete.md)):
  - Jedes Gerät hat einen eigenen Token, gespeichert nur als SHA-256-Hash, Vergleich in
    konstanter Zeit; sperren wirkt sofort. Der Token gilt nur für die drei Geräte-Endpunkte,
    der API-Schlüssel dort nicht. Fehlversuche zählen wie bei der API (429).
  - Die Verbindung baut immer das Gerät auf; Missa-Core erreicht das Gerät nie von sich aus.
  - Geräte-Werkzeuge haben Mindeststufen (`rechner_befehl` & Co. immer `zerstoererisch`),
    gelten nur in den Sitzungen des Geräts, laufen im Hintergrund nie (ausser `nur_lesen`),
    und jedes Ergebnis ist fremder Inhalt. Notfall-Stopp und Aktionsprotokoll (mit
    Gerätename) gelten wie überall.
  - Was ein Werkzeug auf dem Gerät wirklich tut, entscheidet das Gerät (anderes Repo). Ein
    kompromittiertes Gerät kann falsche Ergebnisse liefern – Missa behandelt sie darum nur
    als Daten.
- **Geheimnisse:** Sie werden in Aktionsprotokoll und Dienst-Log geschwärzt, und Backups
  enthalten sie nicht. Neue Dateien des Dienstes sind nur für ihn lesbar (`UMask=0077`).
- **Dienst:** Er läuft als Benutzer `missa` mit enger systemd-Absicherung, siehe
  `deploy/missa-core.service`.
- **Prüfung bei jeder Änderung:**
  - `ruff` mit Sicherheitsregeln,
  - `shellcheck`,
  - `pip-audit`,
  - `gitleaks`,
  - über 1400 Tests.

## Was du selbst tun musst

1. **Nach dem Update auf ai01 die neue systemd-Unit übernehmen und prüfen.** Die
   zusätzlichen Absicherungen sind nicht auf ai01 getestet.

   ```bash
   sudo install -m 644 /opt/missa/app/deploy/missa-core.service /etc/systemd/system/missa-core.service
   ```

   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart missa-core
   ```

   ```bash
   sudo systemctl status missa-core
   ```

   Danach einmal eine Frage stellen, die ssh braucht (z. B. „Wie geht's rp01?“). Was zu tun
   ist, wenn etwas nicht geht, steht in [update.md](update.md).
2. **`MISSA_API_KEY` prüfen.** Mit dem Beispielwert startet Missa nicht mehr. Bei einem
   kurzen Schlüssel steht eine Warnung im Log. Neuen Schlüssel erzeugen mit
   `openssl rand -hex 32`, danach in allen Apps eintragen (missa-chat, Missa Control).
3. **Nextcloud-App-Passwort vorsorglich austauschen,** falls Missa vor diesem Update Mails,
   Webseiten oder Dateien von Fremden gelesen hat. Die kritische Lücke oben hätte es an
   einen fremden Host schicken können. Im Repo wurde kein Geheimnis gefunden.
4. **`VM_VERWALTUNG_IP` setzen,** bevor Missa einen Minecraft-Server anlegt. Sonst lehnt sie
   ab.
5. **Private vulnerability reporting einschalten:** auf GitHub unter
   *Settings → Code security → Private vulnerability reporting*, damit Meldungen nach
   [SECURITY.md](../.github/SECURITY.md) ankommen.
6. **Vorschläge von Dependabot regelmässig ansehen und mergen,** wenn die CI grün ist.
