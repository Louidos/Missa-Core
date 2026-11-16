# Changelog

Alle nennenswerten Änderungen an diesem Projekt stehen in dieser Datei.

Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionsnummern folgen [Semantic Versioning](https://semver.org/lang/de/).

## Unveröffentlicht

### Geändert

- Die automatische Prüfung sucht jetzt zusätzlich nach bekannten Lücken in Abhängigkeiten
  und nach versehentlich eingecheckten Passwörtern (#84)
- Der Code wurde vollständig auf Sicherheitslücken geprüft; strengere Prüfregeln (ruff `S`)
  sind jetzt dauerhaft aktiv (#86)

### Behoben

- Missa ruft Dateien nur noch aus deiner eigenen Nextcloud ab; Pfade, die woandershin zeigen,
  werden abgelehnt (#86)
- Missa startet nicht mehr mit dem Beispielschlüssel `bitte-aendern` aus der
  Beispiel-Konfiguration; ein sehr kurzer Schlüssel erzeugt eine Warnung im Log (#86)
- Die Anmeldung an der API wird geprüft, bevor eine Anfrage gelesen wird; sehr grosse
  Anfragen (über 20 MB) werden abgelehnt, wiederholte falsche Schlüssel sperren die Adresse
  für 15 Minuten, und die öffentliche API-Beschreibung (`/docs`) ist abgeschaltet (#86)
- `/health` zeigt ohne Anmeldung keine Fehlertexte von Missa Control mehr (#86)
- Eine Talk-Nachricht wird nur einmal ausgeführt, auch wenn sie nach einem Neustart noch
  einmal zugestellt wird; ungewöhnliche Zeichen in den Kopfzeilen eines Webhooks führen nicht
  mehr zu einem Fehler 500 (#86)
- Beim Zusammenfassen von Webseiten folgt Missa keiner Weiterleitung mehr auf interne
  Adressen in deinem Netz; zu grosse Seiten werden nicht mehr ganz geladen, und PDFs mit
  extrem vielen Seiten werden abgelehnt statt den Dienst zu blockieren (#86)
- Passwörter und Schlüssel in Werkzeug-Aufrufen landen nicht mehr ungeschwärzt im
  Dienst-Log (journalctl), sondern geschwärzt wie im Aktionsprotokoll (#86)
- Empfänger mit Zeilenumbrüchen oder anderen Steuerzeichen werden abgelehnt, statt einen
  irreführend angezeigten Entwurf anzulegen, der danach still nicht gesendet wird (#86)
- Angaben für Befehle auf anderen Hosts (SSH) werden strenger geprüft; ein angehängter
  Zeilenumbruch wird jetzt ebenfalls abgelehnt (#86)
- „Vergiss alles zu …“ mit Zeichen wie `%` oder `_` löscht nicht mehr versehentlich den
  ganzen Gesprächsverlauf, sondern nur Nachrichten, die den Text wirklich enthalten (#86)
- Das Homelab-Inventar prüft die Antworten von Proxmox, bevor es damit weitere Abfragen
  baut; ein kaputter Eintrag bricht nicht mehr das ganze Inventar ab (#86)
- Der Notfall-Stopp hält jetzt auch eine VM-Erstellung an, die schon läuft – vor dem
  nächsten Schritt statt erst am Ende (#86)
- Beim Wiederherstellen eines Backups bekommt die Datenbank enge Dateirechte (nur der
  Dienst darf sie lesen), und es wird nie durch eine Verknüpfung an einen anderen Ort
  geschrieben (#86)
- Die automatisch erzeugte RCON-Passwortdatei für Minecraft ist von Anfang an nur für den
  Dienst lesbar (#86)
- Missa kann eine Rückfrage (z. B. eine Einladung an andere Personen) nicht mehr selbst
  bestätigen; es zählt nur dein Ja in deiner eigenen Nachricht (#90)
- Ein „Ja“ gilt nur noch in derselben Unterhaltung, direkt nach der Rückfrage und für genau
  eine Aktion – ein „Ok“ in einem anderen Chat oder Stunden später gibt nichts mehr frei (#90)

## 0.10.0 – 2026-11-16

### Hinzugefügt

- Anbindung an Missa Control: Mit zwei Zeilen in der Konfiguration (`MISSA_CONTROL_URL`,
  `MISSA_CONTROL_TOKEN`) nutzt Missa alle Funktionen, die im Panel eingerichtet sind – neue
  erscheinen ohne Neustart. Freigaben entscheidest du im Panel, im Hintergrund darf Missa dort
  nur lesen, fällt das Panel aus, läuft Missa normal weiter; `/health` zeigt den Stand (#78)
- Missa meldet Ereignisse aus Missa Control von sich aus über Nextcloud Talk: wartende
  Freigaben (sofort, mit Hinweis zum Erlauben im Panel), ausgefallene Integrationen,
  ausgelöste Regeln – wörtlich, mit Ruhezeit und Ruhefenster der Alarme, ohne Doppelte auch
  nach einem Neustart; abschaltbar mit `MISSA_CONTROL_EREIGNISSE=aus` (#79)
- Charaktertest gegen das echte Modell: `scripts/charaktertest` stellt Missa die Fragen aus
  `tests/charakter/fragen.yaml` und prüft, ob sie duzt, keine japanischen Wörter benutzt, in
  Krisen ruhig bleibt und nichts Zerstörerisches ohne dein Ja anstösst (Werkzeuge werden dabei
  nie ausgeführt). Der Bericht landet als Markdown in `logs/charaktertest/`. Mit
  `--modell a --modell b` vergleicht er zwei Modelle nebeneinander: Trefferquote, Zeit bis zum
  ersten Token, Tokens pro Sekunde und Gesamtzeit (#75)
- `sudo ./scripts/update.sh --zurueck` wechselt mit einem Befehl auf den letzten Release-Tag
  vor dem installierten Stand zurück (#75)

### Geändert

- Das Update prüft nach dem Neustart zusätzlich, ob Anmeldung, Datenbank und Zeitplaner
  antworten; sonst springt es automatisch auf die vorige Version zurück (#75)
- Jedes Werkzeug hat jetzt mindestens einen Test, und die CI achtet darauf, dass das so
  bleibt (#75)

### Behoben

- Anfragen mit mehreren Aufgaben in einem Satz („Verschiebe das Home Office, trag morgen
  Check-in ein und erinnere mich daran“) enden nicht mehr mit einem unverständlichen Fehler:
  Lehnt das Sprachmodell die Anfrage als zu gross ab, versucht Missa es einmal mit gekürztem
  Verlauf und gekürzten Werkzeug-Ergebnissen. Fehlerantworten sagen jetzt auf Deutsch, was los
  ist und was hilft, und haben einen festen `error.code` (`zeitlimit`, `anfrage_zu_gross`,
  `modell_nicht_erreichbar`, `unerwartete_antwort`, …) und eine `anfrage_id`, unter der im Log
  der Originaltext des llama-servers steht (#77)

## 0.9.0 – 2026-11-12

### Hinzugefügt

- Fragen zur eigenen Doku mit Quellenangabe und Zusammenfassungen von PDFs, Mails und
  Webseiten: Missa indexiert deine Projektdokus und Notizen aus `NEXTCLOUD_DOKU_ORDNER` lokal
  (täglich aktualisiert) und beantwortet „Welche IP hat rp01?“ mit Antwort und Dateiname –
  steht es nicht drin, sagt sie „weiß ich nicht“ statt zu raten. „Fass mir das zusammen:
  <Link>“, ein PDF aus Nextcloud oder eine lange Mail fasst sie kurz zusammen und beantwortet
  Rückfragen dazu; lange Texte verarbeitet sie in Stücken im Hintergrund, sehr lange in der
  Nachtschicht. Webseiten ruft sie nur auf, wenn du ihr den Link selbst schickst (#71)

- Langzeitgedächtnis, Lernen aus Korrekturen und tägliche Sicherung mit Wiederherstellung:
  Missa findet auch frühere Gespräche wieder („was hab ich dir vorgestern über den Server
  erzählt?“), merkt sich Vorlieben, Entscheidungen und Projekte mit Datum und ersetzt ähnliche
  alte Einträge, statt sie doppelt zu führen. Auf „Das war falsch, …“ speichert sie die
  Korrektur und antwortet beim nächsten Mal richtig. „Vergiss das mit dem Kaffee“ löscht nach
  deinem Ja alle Fakten und Gesprächsnachrichten zum Thema endgültig. Jede Nacht um 03:30
  entsteht ein Backup (Gedächtnis, Charakter, Regeln, Routinen, Konfiguration ohne
  Geheimnisse) in `MISSA_DATEN/backup`, die neuesten 14 bleiben; zurückspielen mit
  `sudo scripts/wiederherstellen.sh` (#69)

- Erinnerungen an Geburtstage und lange nicht angerufene Kontakte, E-Mail-Entwürfe im eigenen
  Stil: „Wann hab ich Oma zuletzt angerufen?“ nennt Datum und Art des letzten Kontakts,
  „hab heute Oma angerufen“ vermerkt Missa von selbst, und „Oma ist mir wichtig“ reicht – dann
  erinnert sie nach 30 Tagen ohne Kontakt (`KONTAKTE_ABSTAND_TAGE`). Für die spätere Telefonie
  gibt es `POST /api/kontakte/kontakt` (findet die Person über die Telefonnummer).
  Mail-Entwürfe bleiben Entwürfe, bis du Ja sagst (#67)
- To-dos aus Gesprächen, Notizen und Dateisuche in Nextcloud: Sagst du nebenbei „muss noch
  Rail-Kit bestellen“, legt Missa die Aufgabe in Nextcloud Tasks an und sagt es kurz (doppelte
  legt sie nicht an). „Schreib dir auf, dass …“ ergibt eine Notiz in Nextcloud Notes; Notizen
  lassen sich ergänzen und durchsuchen. „Wo ist die Doku zu rp01?“ findet Dateien und Ordner
  nach Namen, Text- und Markdown-Dateien liest sie vor. Schreiben darf Missa nur in ihrem
  eigenen Ordner `Missa/` (bestehende Dateien ergänzt sie nur), gelöscht wird nie etwas.
  Neu: `NEXTCLOUD_NOTIZEN_KATEGORIE`, `NEXTCLOUD_DATEIEN_ORDNER` (#65)
- Kalender: volle Bearbeitung inkl. Farben, Hinweise zu Pausen, Puffern und Doppelbuchungen:
  Überschneidet sich ein neuer Termin mit einem anderen (in irgendeinem Kalender) oder
  bleiben bei verschiedenen Orten weniger als 30 Minuten, sagt Missa das **bevor** sie
  einträgt und schlägt eine freie Zeit vor. Nach 3 Stunden Terminen am Stück weist sie auf
  eine fehlende Pause hin – beim Eintragen, in der Tagesübersicht („Was habe ich morgen?“)
  und am Vorabend. Puffer, Pausenregel und Abstand bei Ortswechsel lassen sich im Gespräch
  ändern („Mach 20 Minuten Puffer“) und bleiben gespeichert; neu dazu `PAUSEN_NACH_STUNDEN`
  und `PAUSEN_PAUSE_MINUTEN` (#63)

### Geändert

- Geburtstags-Erinnerungen gibt es jetzt nur für wichtige Kontakte, am Vortag und am Morgen
  des Tages (vorher: alle Kontakte, 3 Tage vorher). Wie früher: `KONTAKTE_GEBURTSTAGE=alle`
  und `KONTAKTE_GEBURTSTAG_VORLAUF=3` (#67)
- Bestehende Termine löscht oder verschiebt Missa nur noch nach deinem Ja (neues Werkzeug
  `termin_verschieben`; Farbe, Titel & Co. ändert sie weiter sofort) (#63)
- Puffer- und Pausenblöcke trägt Missa nicht mehr ungefragt ein: Am Abend schlägt sie sie
  vor, und erst auf „Ja, trag ein“ landen sie im Kalender „Pausen“. Der Standard-Puffer ist
  jetzt 15 statt 10 Minuten (#63)

## 0.8.0 – 2026-11-12

### Hinzugefügt

- Überblick über eigene Repos, Issues anlegen und Claude-Code-Prompts aus groben Ideen:
  Missa erkennt den eigenen Git-Server (Gitea oder Forgejo), legt auf Zuruf Issues an
  („Leg im Repo Missa-Core ein Issue an: …“), fasst offene Pull Requests zusammen und
  meldet einmal pro Woche Branches ohne Commit seit 14 Tagen. Auf GitHub (Konto
  Louidos, Token nur lesend) meldet sie fehlgeschlagene Actions-Builds als `wichtig`
  sowie neue Releases und Issues als `info`. Aus einer groben Idee macht sie einen
  Claude-Code-Prompt nach der Vorlage `config/prompts/claude-code.md` (immer mit
  „Repo-Aufbau und Arbeitsweise“ und pro Gruppe Ziel, Aufgaben, Fertig-wenn,
  CHANGELOG-Zeile und Commit), zeigt ihn und legt ihn erst nach deinem Ja als Issue
  an; siehe docs/git-github-prompts.md (#60)
- Subdomains in Caddy vorbereiten mit Freigabe und Minecraft-Verwaltung per RCON:
  „Mach spiel.louis-degiorgi.ch auf 10.0.70.120:8080“ zeigt den fertigen Caddy-Eintrag
  und sagt, ob noch ein DNS-Eintrag fehlt. Erst nach deinem Ja schreibt Missa die Datei
  in ihren eigenen Ordner auf rp01, prüft mit `caddy validate` und lädt Caddy neu. Ist
  der Eintrag ungültig, nimmt sie ihn sofort zurück und erklärt den Fehler; Caddy läuft
  unverändert weiter. Das bestehende Caddyfile bleibt unangetastet (eine `import`-Zeile
  ergänzt du selbst), Einrichtung mit `missa-benutzer.sh --caddy`. Minecraft per RCON:
  „Wer ist online?“, Whitelist anzeigen, Spieler hinzufügen und entfernen, Neustart nur
  nach Ja und mit Warnung, wenn gerade Spieler online sind. Ohne `CADDY_HOST` bzw.
  `MINECRAFT_RCON_HOST` ist beides aus; siehe docs/caddy-minecraft.md (#58)
- Missa erstellt und richtet VMs auf Zuruf ein, mit Plan, Bestätigung und festen Grenzen:
  „Richte einen Minecraft-Server ein“ zeigt einen Plan mit Template, CPU, RAM, Disk, VLAN,
  IP, Name und Playbooks. Erst nach deinem Ja klont Missa die VM aus dem
  Cloud-Init-Template in den Pool `missa`, setzt Netz, IP und SSH-Schlüssel, startet sie,
  richtet sie per Ansible ein (`basis.yml`, `minecraft.yml`: Version wählbar, Whitelist,
  RCON nur von ai01) und meldet dir das Ergebnis. Aufträge über den Grenzen (pro VM und für
  den ganzen Pool, erlaubte VLANs, ID-Bereich) lehnt sie mit Begründung ab. Starten,
  Stoppen und Löschen gehen nur für VMs im eigenen Pool und nur nach Ja, und jeder Schritt
  steht im Aktionsprotokoll. Einrichtung mit `scripts/einrichten/proxmox-missa.sh`; neue
  Playbooks kommen als Datei dazu. Neu: `GET /api/vms`; siehe docs/proxmox-vms.md (#56)
- Missa hält ihr Wissen über VMs und IPs selbst aktuell und fasst die Firewall-Logs zusammen:
  Stündlich liest sie aus Proxmox (nur lesend) alle VMs und Container mit Status,
  Ressourcen, VLAN und IP. „Welche IP hat rp01?“ beantwortet sie mit dem aktuellen Stand,
  und neue VMs, verschwundene VMs und geänderte IPs meldet sie als `info`. Die Logs der
  ZyWALL (per Syslog an ai01) wertet sie aus und schickt jeden Morgen eine Zusammenfassung
  des Vortags mit gehäuften Blocks, Port-Scans und ungewöhnlichen Zielen. Auf Nachfrage
  geht das auch für jeden anderen Tag („Was war gestern an der Firewall los?“). An der
  Firewall selbst kann Missa nichts ändern. Neu: `GET /api/homelab`; Einrichtung siehe
  docs/homelab-firewall.md (#54)

## 0.7.0 – 2026-11-11

### Hinzugefügt

- Update-Verwaltung mit Wartungsfenster und Warnungen vor ablaufenden Zertifikaten und
  Domains: „Welche Updates stehen an?“ zeigt pro VM, was ansteht, Sicherheitsupdates mit 🔒
  und ob ein Neustart nötig ist. Missa prüft das täglich selbst und meldet neue
  Sicherheitsupdates einmal. „Plan ein Wartungsfenster“ schlägt einen freien Termin im
  Kalender vor, eingetragen wird er erst nach deinem Ja, und zu Beginn erinnert Missa dich
  mit dem Plan. Eingespielt wird VM für VM, jede erst nach deinem Ja. ai01 kommt zuletzt,
  mit Warnung, weil Missa dabei kurz weg ist. Ein Neustart braucht ein eigenes Ja, und
  danach meldet sich Missa mit „Ich bin wieder da“. Täglich prüft sie außerdem die
  HTTPS-Zertifikate, das Ablaufdatum der Domains (RDAP) und die erwarteten DNS-Einträge
  und warnt 21 und 7 Tage vor einem Ablauf. Auf den Hosts darf `missa` per sudo dafür nur
  `apt-get update`, `apt-get -y upgrade` und `reboot` (Einrichtungs-Script noch einmal
  ausführen). Neu: `GET /api/updates`; siehe docs/updates-zertifikate.md (#51)

## 0.6.0 – 2026-11-11

### Hinzugefügt

- Container-Übersicht, verständliche Fehlersuche in Logs und Warnungen bei ungewöhnlichen
  Anmeldungen: „Welche Container laufen auf wb01?“ zeigt Status, CPU und RAM. Hängende oder
  abgestürzte Container meldet Missa von selbst (`wichtig`), neu startet sie einen erst
  nach deinem Ja, und beides steht im Aktionsprotokoll. „Warum geht Nextcloud nicht?“
  liest Container-, System- und Nextcloud-Logs und erklärt die wahrscheinliche Ursache
  verständlich (z. B. „Die Datenbank ist nicht erreichbar“, „Die Festplatte ist voll“).
  Lange Logs kommen in Teilen, sehr lange Analysen bietet Missa für die Nachtschicht an.
  SSH- und Nextcloud-Anmeldungen werden überwacht: 10 Fehlversuche in 15 Minuten,
  Anmeldungen von neuen IPs und zu ungewohnten Zeiten meldet Missa. „10.0.30.5 ist mein
  Laptop“ markiert eine IP als bekannt. Zugang über den Benutzer `missa`, den
  `scripts/einrichten/missa-benutzer.sh` auf jedem Host einrichtet: keine Shell, per sudo
  nur `docker ps`/`stats`/`logs`/`restart`. Neu: `GET /api/container` und
  `GET /api/anmeldungen`; Einrichtung siehe docs/container-logs-logins.md (#48)

## 0.5.0 – 2026-11-11

### Hinzugefügt

- Server-Alarme mit Regeln per Sprache, Hinweise auf auffällige Werte und Prüfung der
  Erreichbarkeit: „Ruf mich an, wenn ai01 über 90 % ist“ speichert eine Regel (Ziel,
  Messwert, Grenze, Dauer, Stufe), die du im Chat auflisten, ändern und löschen kannst.
  Missa liest dafür jede Minute dein Monitoring-Tool, das Homepage-Dashboard und Proxmox
  (sobald eingerichtet, nur lesend) und meldet sich mit der passenden Stufe: `info`, `wichtig`
  oder `notfall`. Notfälle kommen sofort, auch nachts (bis es ein Telefon gibt, deutlich
  markiert per Talk); `info` und `wichtig` warten bis nach der Ruhezeit
  (Standard 22–7 Uhr, im Chat änderbar). Derselbe Alarm kommt nicht im Minutentakt
  (Ruhefenster, Standard 60 Minuten). Steigt ein Wert ungewöhnlich stark über das, was um
  diese Tageszeit üblich ist, gibt Missa schon unter der Grenze einen Hinweis. Außerdem prüft
  sie alle 5 Minuten, ob cloud., git. und ki.louis-degiorgi.ch von außen erreichbar sind (DNS,
  HTTPS, gültiges Zertifikat), und meldet Ausfall und Rückkehr. Übersicht unter
  `GET /api/alarme` und `GET /api/server`; Einrichtung siehe docs/server-alarme.md (#45)

### Behoben

- Die Versionsnummer im Code passt wieder zu den Releases: Nach dem Update auf 0.4.0 zeigten
  `/health` und das Update-Script noch „0.3.0“ an; ab dem nächsten Release stimmt es wieder (#43)

## 0.4.0 – 2026-11-11

### Hinzugefügt

- Zeitgenaue Erinnerungen, Routinen per Sprache und Nachtschicht für lange Aufgaben: „Jeden
  Freitag um 17 Uhr Wochenrückblick“ legt eine Routine an, die Missa zur Zeit selbst mit
  ihren Werkzeugen erledigt und dir das Ergebnis schickt – pausieren, fortsetzen und löschen
  per Gespräch. „Mach das heute Nacht“ merkt lange Aufgaben für die Nachtschicht vor
  (Standard 01:00–06:00, per Gespräch änderbar, z. B. „Nachtschicht von 2 bis 5“); das
  Ergebnis kommt am Morgen als Nachricht und Missa erwähnt es im nächsten Gespräch. Alles
  läuft im Hintergrund, der Chat bleibt frei, und verpasste Läufe werden nach einem Neustart
  nachgeholt; Übersicht unter `GET /api/routinen` und `GET /api/nachtschicht` (#37)
- Bestätigung vor zerstörerischen Aktionen, Aktionsprotokoll und Notfall-Stopp: Jedes Werkzeug
  hat eine Stufe (lesen, schreiben, zerstörerisch). Löschen und Verwerfen macht Missa nur nach
  deinem ausdrücklichen Ja zu genau dieser Aktion – sie sagt vorher, was passiert, und das Ja
  gilt 10 Minuten. Jede Aktion steht mit Zeit, Kanal, Parametern (ohne Passwörter) und Ergebnis
  im Protokoll: Frag „Was hast du heute gemacht?“ oder schau unter `GET /api/aktionen`.
  „Notfall-Stopp“ im Chat oder `sudo scripts/notfall-stopp.sh` sperrt sofort alles Schreibende,
  auch nach einem Neustart; Lesen und Reden geht weiter. Aufheben geht nur auf ai01 mit
  `sudo scripts/notfall-freigabe.sh` (#39)

### Geändert

- Erinnerungen, Routinen und Nachtaufgaben löscht Missa erst nach deinem ausdrücklichen Ja.
  Was sie im Hintergrund (Routine, Nachtschicht) tut, kann nie eine Rückfrage bestätigen
  oder eine E-Mail senden (#37)
- Einen E-Mail-Entwurf verwirft Missa jetzt erst nach deinem ausdrücklichen Ja (#39)
- Auf Missas Rückfrage musst du nicht extra „Ja“ sagen: Ein klarer Auftrag wie „Lösch es“,
  „Entferne den Termin“ oder „Schick diese Mail“ zählt genauso als Zustimmung. Fragen,
  „lieber nicht“ oder „Vergiss es“ zählen weiterhin nicht (#41)

## 0.3.0 – 2026-11-10

### Hinzugefügt

- Missa erinnert dich auf die Minute genau, auch wiederkehrend und nach einem Neustart:
  „Erinnere mich um 6:30 Uhr an Training“, „in 20 Minuten“, „am Freitag um 14 Uhr“ oder
  „jeden Montag um 7“ – sie bestätigt Datum und Uhrzeit, fragt bei mehrdeutigen Angaben
  nach, holt verpasste Erinnerungen nach einem Ausfall mit Hinweis nach, und du kannst
  Erinnerungen im Chat auflisten, ändern, löschen und schlummern („in 10 Minuten nochmal“);
  Zustellung über Nextcloud Talk, Übersicht unter `GET /api/erinnerungen` (#26)
- Missa verwaltet deinen Kalender komplett selbst, inklusive Farben, eigener Kalender und
  Rückgängig-Funktion: Termine ändern und verschieben („Verschieb das Meeting morgen auf
  15 Uhr und mach es rot“), Kategorien mit eigenem Farbsystem, Erinnerungen, Serien samt
  einzelner Ausnahmen, ganztägige Termine, Kalender anlegen, umbenennen und einfärben; jede
  Änderung wird mit vorher/nachher protokolliert, lässt sich mit „mach das rückgängig“
  zurücknehmen und steht unter `GET /api/kalender/protokoll`; Einladungen an andere
  Personen verschickt sie nur nach deinem Ja (#27)

- Missa schreibt E-Mails in deinem Stil vor und schickt sie nur ab, wenn du zustimmst:
  „Schreib dem Chef, dass ich morgen später komme“ ergibt einen Entwurf im Entwürfe-Ordner,
  den Missa dir mit Empfänger, Betreff und Text zeigt und mit „Soll ich das abschicken?“
  abschliesst; erst dein „Ja, abschicken“ verschickt ihn, jede Änderung braucht ein neues Ja.
  Dein Schreibstil wird aus gesendeten Mails gelernt, liegt als bearbeitbare Datei vor und
  lässt sich im Chat anpassen („schreib förmlicher an Lehrer“). Einrichtung über die neuen
  `MAIL_*`-Einstellungen (#28)

- Missa erinnert an Geburtstage und daran, dich bei wichtigen Menschen zu melden:
  Geburtstage aus Nextcloud-Kontakte bekommen eine Erinnerung einige Tage vorher und am Tag
  selbst (29. Februar in Nicht-Schaltjahren am 28.); „Oma mindestens alle 3 Wochen“ markiert
  wichtige Kontakte, der letzte Kontakt kommt aus deinen Mails oder aus dem Chat („hab gerade
  mit Oma telefoniert“), und ist der Abstand überschritten, erinnert Missa freundlich und
  schlägt einen freien Zeitpunkt im Kalender vor (#29)

- Missa achtet von sich aus auf Pausen und Puffer in deinem Kalender: Jeden Abend prüft sie
  den nächsten Tag auf Termine direkt hintereinander, fehlende Mittagspause, zu volle Tage
  und fehlende Wegzeit, trägt Puffer- und Pausenblöcke im Kalender „Pausen“ selbst ein
  (rückgängig machbar) und sagt dir danach Bescheid; zerstört ein neuer Termin einen Puffer,
  weist sie sofort darauf hin und schlägt eine bessere Uhrzeit vor. Alle Grenzwerte sind
  über `PAUSEN_*` einstellbar (#30)

### Geändert

- Termine löscht Missa jetzt ohne Rückfrage – dafür lässt sich jede Löschung rückgängig
  machen. Nur wenn andere Personen eine Absage-Mail bekämen, fragt sie weiterhin vorher (#27)

## 0.2.1 – 2026-11-10

### Behoben

- Update-Script: Wurde der neue Code von Hand ausgecheckt und gab es zur installierten
  Version keinen Tag, meldete das Script fälschlich „schon aktuell“ und installierte
  nichts. Jetzt erkennt es die Ersteinrichtung auch in diesem Fall (#16)

## 0.2.0 – 2026-11-10

### Hinzugefügt

- Update auf ai01 mit einem Befehl: `sudo ./scripts/update.sh` holt die neueste Version,
  zeigt, was neu ist, ergänzt neue Einstellungen (ohne bestehende zu ändern), listet auf,
  was du noch ausfüllen musst und was optional ist, und springt bei Fehlern automatisch auf
  die vorherige Version zurück; `--dry-run` zeigt nur an (#16)
- Missa ist per Nextcloud Talk erreichbar, nur für deinen Account und den Admin-Account;
  alle anderen werden still ignoriert. Missa zeigt mit 👀, dass sie arbeitet, antwortet
  als Zitat-Antwort mit ihrem gewohnten Charakter, Gedächtnis und Werkzeugen und teilt
  sehr lange Antworten auf (Einrichtung siehe docs/nextcloud-talk.md) (#17)
- Missa kann dir von sich aus in Nextcloud Talk schreiben, z. B. bei Erinnerungen –
  nur in die eingestellte Unterhaltung und höchstens 10 Nachrichten pro Minute (#18)

## 0.1.0 – 2026-11-09

Erste Version von Missa-Core.

### Hinzugefügt

- Missa antwortet über eine eigene API mit ihrem Charakter: OpenAI-kompatible
  Endpunkte `/v1/chat/completions` (mit und ohne Streaming) und `/v1/models`,
  geschützt per Bearer-Token; der Charakter wird bei jeder Anfrage frisch aus
  der Charakterdatei gelesen, Gespräche mit Sitzungs-ID werden gemerkt, und
  `/health` zeigt, ob llama-server erreichbar ist (#2)
- Missa liest und schreibt Termine im Nextcloud-Kalender: Termine eines Tages
  oder Zeitraums anzeigen (auch wiederkehrende), neue Termine eintragen und
  Termine löschen – gelöscht wird erst, nachdem Louis ausdrücklich Ja gesagt hat (#3)
- Missa verwaltet deine Aufgaben in Nextcloud: offene (oder alle) Aufgaben
  anzeigen, neue Aufgaben mit Fälligkeit, Priorität und Notiz anlegen, Aufgaben
  abhaken und – nach deinem Ja – löschen; alles erscheint direkt in der
  Nextcloud-App „Aufgaben“ (#4)
- Missa merkt sich Wichtiges über dich: Auf „Merk dir …“ speichert sie den Fakt
  dauerhaft, findet passende Fakten bei jeder Frage von selbst wieder (auch in
  neuen Gesprächen), vergisst etwas erst nach deinem Ja, und unter
  `GET /api/gedaechtnis` kannst du alles Gespeicherte prüfen (#5)
- Installationsanleitung, API-Doku und systemd-Dienst: Missa-Core läuft auf ai01
  als Dienst unter dem Benutzer `missa`, startet nach einem Neustart von selbst,
  und `docs/api.md` erklärt, wie sich eine App verbindet (#6)
