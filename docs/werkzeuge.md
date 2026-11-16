# Werkzeuge

Alle Werkzeuge, die Missa aufrufen kann, mit ihrer Stufe. Missa bekommt nur die Werkzeuge,
deren Dienst eingerichtet ist; welche gerade aktiv sind, zeigt `GET /health` unter
`werkzeuge` (siehe [api.md](api.md)). Beispiel-Fragen zu jedem Bereich stehen in
[api.md](api.md#was-missa-kann-werkzeuge).

| Stufe | Bedeutung |
|-------|-----------|
| lesen | schaut nur nach, läuft sofort – auch bei Notfall-Stopp |
| schreiben | legt an oder ändert; läuft sofort, nach fremden Daten (Mail, Datei, Log …) erst nach Ja |
| **zerstörerisch** | löscht, sendet, startet neu – nur nach Louis' ausdrücklichem Ja zu genau dieser Aktion, nie im Hintergrund |

Einzelheiten zu Rückfrage, Aktionsprotokoll und Notfall-Stopp: [rechte.md](rechte.md).
Wie ein neues Werkzeug dazukommt: [entwicklung.md](entwicklung.md#ein-neues-werkzeug).

Stand: Version 0.10.0 – 122 fest eingebaute Werkzeuge (56 lesen, 45
schreiben, 21 zerstörerisch). Die Beschreibungen sind der erste Satz dessen, was das Modell
zu jedem Werkzeug liest (aus dem Code, `src/missa/`).

## Fest eingebaute Werkzeuge

### Rechte und Aktionsprotokoll

Voraussetzung: immer · Anleitung: [rechte.md](rechte.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `aktionen_anzeigen` | lesen | Zeigt Missas eigenes Aktionsprotokoll: was sie wann getan hat (Werkzeuge, Bestätigungen, Sperren). |

### Gedächtnis

Voraussetzung: immer · Anleitung: [gedaechtnis.md](gedaechtnis.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `merken` | schreiben | Merkt sich einen wichtigen Fakt über Louis dauerhaft mit Datum (Vorlieben, Entscheidungen, Projekte – über Gespräche und Neustarts hinweg). |
| `erinnern` | lesen | Sucht im Gedächtnis nach gespeicherten Fakten über Louis („Was weißt du über …?“). |
| `korrigieren` | schreiben | Lernt aus einer Korrektur („das war falsch, …“): speichert, was richtig ist, und ersetzt den falschen alten Eintrag, statt ihn doppelt zu führen. |
| `frueher_gesagt` | lesen | Durchsucht frühere Gespräche nach dem, was Louis gesagt hat (mit Datum) – z. B. „Was hab ich dir letzte Woche über den Server erzählt?“. |
| `vergessen` | **zerstörerisch** | Löscht endgültig: einen Fakt (fakt_id) oder alles zu einem Thema (thema, auch aus früheren Gesprächen) – „Vergiss das mit dem Kaffee“. |

### Erinnerungen

Voraussetzung: immer · Anleitung: [erinnerungen.md](erinnerungen.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `erinnerung_anlegen` | schreiben | Erinnert Louis zu einem Zeitpunkt per Nachricht (auch wiederkehrend). |
| `erinnerungen_anzeigen` | lesen | Listet alle geplanten Erinnerungen mit ID, Zeitpunkt und Wiederholung. |
| `erinnerung_aendern` | schreiben | Ändert Text oder Zeitpunkt einer Erinnerung oder beendet ihre Wiederholung. |
| `erinnerung_loeschen` | **zerstörerisch** | Löscht eine geplante Erinnerung. |
| `erinnerung_schlummern` | schreiben | Erinnert nochmal nach einigen Minuten („in 10 Minuten nochmal“). |

### Routinen und Nachtschicht

Voraussetzung: immer · Anleitung: [routinen-nachtschicht.md](routinen-nachtschicht.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `routine_anlegen` | schreiben | Legt eine Routine an: eine Anweisung an dich selbst, die du regelmäßig ausführst (z. B. „jeden Freitag um 17 Uhr einen Wochenrückblick schreiben“). |
| `routinen_anzeigen` | lesen | Listet alle Routinen mit ID, nächstem Lauf, Wiederholung und ob sie pausiert sind. |
| `routine_pausieren` | schreiben | Pausiert eine Routine (läuft nicht mehr). |
| `routine_fortsetzen` | schreiben | Setzt eine pausierte Routine fort (ab dem nächsten regulären Zeitpunkt). |
| `routine_loeschen` | **zerstörerisch** | Löscht eine Routine. |
| `routine_jetzt_ausfuehren` | schreiben | Führt eine Routine sofort einmal im Hintergrund aus (z. B. zum Ausprobieren). |
| `nachtaufgabe_anlegen` | schreiben | Merkt eine lange Aufgabe für die Nachtschicht vor („mach das heute Nacht“, „erledige das nachts“). |
| `nachtaufgaben_anzeigen` | lesen | Zeigt das Nachtfenster, vorgemerkte Nachtaufgaben und Ergebnisse der letzten Tage. |
| `nachtaufgabe_ergebnis` | lesen | Liefert das vollständige Ergebnis einer Nachtaufgabe. |
| `nachtaufgabe_loeschen` | **zerstörerisch** | Löscht eine Nachtaufgabe. |
| `nachtschicht_einstellen` | schreiben | Ändert das Zeitfenster der Nachtschicht (Standard 01:00–06:00) dauerhaft, z. B. „Nachtschicht von 2 bis 5 Uhr“. |

### Sicherung

Voraussetzung: immer · Anleitung: [sicherung.md](sicherung.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `backup_erstellen` | schreiben | Legt sofort ein Backup von Missa an (Gedächtnis, Charakter, Regeln, Routinen, Konfiguration ohne Geheimnisse). |
| `backups_anzeigen` | lesen | Listet die vorhandenen Backups von Missa. |

### Eigene Doku

Voraussetzung: Nextcloud mit eingetragenen Doku-Ordnern · Anleitung: [doku-wissen.md](doku-wissen.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `doku_fragen` | lesen | Sucht in Louis' eigener Projektdoku und seinen Notizen (lokaler Index der Nextcloud-Ordner) und liefert passende Stellen mit Dateinamen – z. B. „Welche IP hat rp01?“. |
| `doku_index_aktualisieren` | schreiben | Liest neue und geänderte Doku-Dateien sofort ein (sonst passiert das täglich). |

### Zusammenfassungen

Voraussetzung: immer (Dateien aus Nextcloud und Mails nur, wenn eingerichtet) · Anleitung: [doku-wissen.md](doku-wissen.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `zusammenfassen` | schreiben | Fasst ein PDF, eine Text-Datei aus Nextcloud (pfad), eine Mail (mail_id, z. B. INBOX:42) oder eine Webseite zusammen. |
| `dokument_fragen` | lesen | Beantwortet Rückfragen zu einem geladenen Dokument mit den passenden Stellen. |
| `dokumente_anzeigen` | lesen | Listet die zuletzt geladenen Dokumente mit Nummer. |

### Kalender

Voraussetzung: Nextcloud · Anleitung: [kalender.md](kalender.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `termine_anzeigen` | lesen | Zeigt Louis' Termine aus dem Nextcloud-Kalender in einem Zeitraum (wiederkehrende Termine einzeln, mit Farbe, Kategorien, Erinnerungen und ID). |
| `termin_eintragen` | schreiben | Trägt einen Termin ein. |
| `termin_aendern` | schreiben | Ändert Titel, Ort, Notiz, Farbe, Kategorien, Erinnerungen, Wiederholung oder Teilnehmer eines Termins: nur die Angaben mitschicken, die sich ändern. |
| `termin_verschieben` | **zerstörerisch** | Verschiebt einen bestehenden Termin auf eine andere Zeit (neuer start ohne ende behält die Dauer; mit nur_am nur ein Vorkommen einer Serie). |
| `termin_loeschen` | **zerstörerisch** | Löscht einen Termin (oder mit nur_am nur ein Vorkommen einer Serie). |
| `kalender_anzeigen` | lesen | Listet alle Kalender mit Farbe und dein Farbsystem (Kategorie = Farbe). |
| `kalender_anlegen` | schreiben | Legt einen neuen Kalender an, optional mit Farbe (z. B. für Arbeit, Lehre, Pausen).selbststaendig |
| `kalender_aendern` | schreiben | Benennt einen Kalender um oder ändert seine Farbe.selbststaendig |
| `farbsystem_festlegen` | schreiben | Legt fest, welche Farbe Termine einer Kategorie bekommen (z. B. Lehre = grün); ohne farbe wird die Kategorie entfernt. |
| `kalender_rueckgaengig` | schreiben | Macht eine Kalenderänderung rückgängig („mach das rückgängig“). |
| `kalender_protokoll` | lesen | Zeigt deine letzten Kalenderänderungen mit Nummer (für Rückgängig). |

### Pausen und Puffer

Voraussetzung: Nextcloud-Kalender · Anleitung: [pausen.md](pausen.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `tag_pruefen` | schreiben | Prüft einen Tag auf fehlende Puffer, Pausen (nach 3 Std. am Stück), Mittagspause, Ortswechsel und zu volle Tage und schlägt Puffer-/Pausenblöcke vor. |
| `pausen_regeln_anzeigen` | lesen | Zeigt die Regeln für Puffer, Pausen und Ortswechsel. |
| `pausen_regeln_einstellen` | schreiben | Ändert Regeln für Puffer und Pausen dauerhaft, z. B. „mach 20 Minuten Puffer“, „Pause schon nach 2 Stunden“, „bei Ortswechsel 45 Minuten Abstand“. |

### Aufgaben

Voraussetzung: Nextcloud mit Aufgabenliste · Anleitung: [nextcloud-dateien.md](nextcloud-dateien.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `aufgaben_anzeigen` | lesen | Zeigt Louis' Aufgabenliste aus Nextcloud (standardmäßig nur offene Aufgaben). |
| `aufgabe_erstellen` | schreiben | Legt eine neue Aufgabe in Louis' Nextcloud-Aufgabenliste an – auch von dir aus, wenn Louis nebenbei sagt, dass er etwas noch tun muss („muss noch Rail-Kit bestellen“). |
| `aufgabe_erledigt` | schreiben | Hakt eine Aufgabe in Nextcloud als erledigt ab. |
| `aufgabe_loeschen` | **zerstörerisch** | Löscht eine Aufgabe. |

### Notizen

Voraussetzung: Nextcloud Notes · Anleitung: [nextcloud-dateien.md](nextcloud-dateien.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `notiz_anlegen` | schreiben | Schreibt eine Notiz in Nextcloud Notes („Schreib dir auf, dass …“, „Notier …“). |
| `notiz_ergaenzen` | schreiben | Hängt Text mit Datum an eine bestehende Notiz an. |
| `notizen_suchen` | lesen | Durchsucht Louis' Notizen in Nextcloud Notes nach Wörtern (Titel und Inhalt). |
| `notiz_lesen` | lesen | Zeigt den ganzen Inhalt einer Notiz. |

### Dateien

Voraussetzung: Nextcloud (WebDAV) · Anleitung: [nextcloud-dateien.md](nextcloud-dateien.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `dateien_suchen` | lesen | Sucht in Louis' Nextcloud-Dateien nach Datei- und Ordnernamen (z. B. „Wo ist die Doku zu rp01?“ → name=rp01). |
| `ordner_anzeigen` | lesen | Listet den Inhalt eines Nextcloud-Ordners (leer = oberste Ebene). |
| `datei_lesen` | lesen | Liest eine Text- oder Markdown-Datei aus Nextcloud (Pfad aus dateien_suchen). |
| `datei_schreiben` | schreiben | Legt eine Textdatei in deinem eigenen Ordner „Missa/“ (Standard) an oder hängt Text an (anhaengen=true). |

### Kontakte

Voraussetzung: Nextcloud (CardDAV) · Anleitung: [kontakte.md](kontakte.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `kontakte_suchen` | lesen | Sucht Kontakte in Nextcloud (Name, Spitzname, E-Mail) mit Adresse und Geburtstag. |
| `geburtstage_anzeigen` | lesen | Zeigt, wer in den nächsten Tagen Geburtstag hat. |
| `kontakt_wichtig_markieren` | schreiben | Markiert eine Person als wichtig: Missa erinnert an ihren Geburtstag (am Vortag und am Morgen) und daran, wenn Louis sich länger als den Abstand nicht gemeldet hat („Oma mindestens alle 3 Wochen“; ohne Angabe 30 Tage). |
| `kontakt_nicht_mehr_wichtig` | schreiben | Nimmt eine Person aus der Kontaktpflege (keine Erinnerungen mehr). |
| `kontakt_gemeldet` | schreiben | Vermerkt, dass Louis Kontakt mit einer Person hatte („hab gerade mit Oma telefoniert“, „hab heute Oma angerufen“ → art=Anruf). |
| `letzter_kontakt` | lesen | Sagt, wann Louis zuletzt Kontakt mit einer Person hatte („Wann hab ich Oma zuletzt angerufen?“) – mit Datum und Art (Anruf, Mail, Treffen). |
| `wichtige_kontakte_anzeigen` | lesen | Zeigt die wichtigen Kontakte mit gewünschtem Abstand und letztem Kontakt. |

### E-Mail

Voraussetzung: IMAP/SMTP eingerichtet · Anleitung: [email.md](email.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `emails_anzeigen` | lesen | Listet die neuesten Mails eines Ordners (Standard: Posteingang) mit ID. |
| `email_lesen` | lesen | Liest eine Mail (ID aus emails_anzeigen). |
| `email_entwurf_erstellen` | schreiben | Schreibt einen E-Mail-Entwurf im Stil von Louis (siehe Schreibstil) und legt ihn im Entwürfe-Ordner ab. |
| `email_entwurf_aendern` | schreiben | Ändert einen offenen Entwurf. |
| `email_entwuerfe_anzeigen` | lesen | Zeigt alle offenen E-Mail-Entwürfe (Louis sieht sie unter deiner Antwort). |
| `email_entwurf_verwerfen` | **zerstörerisch** | Verwirft einen Entwurf (wenn Louis ihn nicht mehr will). |
| `email_adresse_suchen` | lesen | Sucht die E-Mail-Adresse einer Person (Gedächtnis, Kontakte, frühere Mails). |
| `schreibstil_anzeigen` | lesen | Zeigt Louis' Schreibstil-Profil für E-Mails. |
| `schreibstil_lernen` | schreiben | Leitet Louis' Schreibstil neu aus seinen gesendeten Mails ab (eigene Regeln bleiben). |
| `schreibstil_anpassen` | schreiben | Speichert eine Stil-Regel („an Lehrer förmlicher, Sie-Form“) oder entfernt sie. |

### Nextcloud Talk

Voraussetzung: Talk eingerichtet · Anleitung: [nextcloud-talk.md](nextcloud-talk.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `talk_nachricht_senden` | schreiben | Schreibt Louis von dir aus eine Nachricht in seine Talk-Unterhaltung mit dir (z. B. eine Erinnerung, einen Hinweis oder eine Zusammenfassung). |

### Alarme

Voraussetzung: mindestens ein meldender Dienst (Monitoring, Erreichbarkeit, SSH …) · Anleitung: [server-alarme.md](server-alarme.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `alarme_anzeigen` | lesen | Zeigt die Alarme (Server, Erreichbarkeit …), die du zuletzt gemeldet hast, dazu Ruhezeit und Ruhefenster. |
| `alarm_einstellungen` | schreiben | Ändert die Ruhezeit für Alarme (info und wichtig warten bis zum Ende, Notfälle kommen immer) oder wie lange derselbe Alarm nicht wiederholt wird. |

### Server-Alarme

Voraussetzung: Monitoring-Quelle eingerichtet · Anleitung: [server-alarme.md](server-alarme.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `server_werte_anzeigen` | lesen | Zeigt die aktuellen Messwerte der Server (CPU, RAM, Disk, läuft …) aus Monitoring, Homepage und Proxmox, dazu aktive Alarme. |
| `alarmregel_anlegen` | schreiben | Speichert eine Alarm-Regel: Wenn ein Messwert eines Servers länger als dauer_minuten über (oder unter) der Grenze liegt, meldest du dich mit der Stufe. |
| `alarmregeln_anzeigen` | lesen | Listet alle Alarm-Regeln für die Server und welche gerade ausgelöst sind. |
| `alarmregel_aendern` | schreiben | Ändert eine Alarm-Regel (Grenze, Richtung, Dauer, Stufe, Ziel, Messwert oder wie lange derselbe Alarm nicht wiederholt wird). |
| `alarmregel_loeschen` | **zerstörerisch** | Löscht eine Alarm-Regel (nur nach Louis' ausdrücklichem Ja). |

### Erreichbarkeit

Voraussetzung: eigene Domains eingetragen · Anleitung: [server-alarme.md](server-alarme.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `erreichbarkeit_pruefen` | lesen | Prüft sofort, ob die eigenen Domains (z. B. cloud., git., ki.) von außen erreichbar sind: DNS, HTTPS-Antwort und gültiges Zertifikat. |

### Container und Fehlersuche

Voraussetzung: SSH-Hosts eingetragen · Anleitung: [container-logs-logins.md](container-logs-logins.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `container_anzeigen` | lesen | Zeigt die Docker-Container eines Hosts (oder aller Hosts) mit Status, CPU und RAM und markiert hängende oder abgestürzte Container. |
| `container_logs` | lesen | Zeigt die letzten Log-Zeilen eines Containers, auf Wunsch nur die auffälligen. |
| `container_neustarten` | **zerstörerisch** | Startet einen Docker-Container neu (docker restart) – nur nach Louis' ausdrücklichem Ja. |
| `fehlersuche` | lesen | Sucht die Ursache, wenn ein Dienst nicht geht („Warum geht Nextcloud nicht?“): liest Container- und System-Logs auf den Hosts, filtert die wichtigen Zeilen und erkennt bekannte Ursachen. |
| `journal_lesen` | lesen | Liest das System-Log (journalctl) eines Hosts, optional nur eine Unit oder ab einer Priorität. |

### Anmeldungen

Voraussetzung: SSH-Hosts eingetragen · Anleitung: [container-logs-logins.md](container-logs-logins.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `anmeldungen_anzeigen` | lesen | Zeigt die SSH- und Nextcloud-Anmeldungen (erfolgreich und fehlgeschlagen) der letzten Tage, optional für einen Host. |
| `ip_als_bekannt_markieren` | schreiben | Markiert eine IP als bekannt (z. B. „10.0.30.5 ist mein Laptop“) – Anmeldungen von dort lösen keine Hinweise mehr aus. |
| `ip_nicht_mehr_bekannt` | schreiben | Nimmt die Markierung „bekannt“ von einer IP wieder weg. |
| `bekannte_ips_anzeigen` | lesen | Listet alle als bekannt markierten IPs. |
| `login_einstellungen` | schreiben | Ändert, ab wie vielen fehlgeschlagenen Anmeldungen in wie vielen Minuten du dich meldest. |

### Updates

Voraussetzung: SSH-Hosts eingetragen · Anleitung: [updates-zertifikate.md](updates-zertifikate.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `updates_anzeigen` | lesen | Zeigt die anstehenden Updates pro VM, Sicherheitsupdates markiert, und ob ein Neustart nötig ist. |
| `wartungsfenster_vorschlagen` | lesen | Sucht einen freien Termin im Kalender für ein Wartungsfenster (trägt nichts ein). |
| `wartungsfenster_eintragen` | **zerstörerisch** | Trägt das vorgeschlagene Wartungsfenster in den Kalender ein – nur nach Louis' Ja. |
| `updates_plan` | lesen | Zeigt die Reihenfolge der VMs für die Updates (die eigene VM zuletzt), was schon eingespielt ist und das Wartungsfenster. |
| `updates_einspielen` | **zerstörerisch** | Spielt die Updates auf genau einer VM ein (apt-get -y upgrade) – nur nach Louis' Ja, VM für VM, die eigene VM zuletzt. |
| `host_neustarten` | **zerstörerisch** | Startet eine VM neu (z. B. nach Updates) – nur nach Louis' eigenem Ja. |

### Zertifikate, Domains, DNS

Voraussetzung: Domains eingetragen · Anleitung: [updates-zertifikate.md](updates-zertifikate.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `zertifikate_pruefen` | lesen | Zeigt, wann die HTTPS-Zertifikate und Domains ablaufen (RDAP) und ob die DNS-Einträge den erwarteten Werten entsprechen. |

### Homelab

Voraussetzung: Proxmox-Token (nur lesen) · Anleitung: [homelab-firewall.md](homelab-firewall.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `homelab_anzeigen` | lesen | Zeigt die VMs und Container aus Proxmox mit IP, VLAN, Status und Ressourcen – für Fragen wie „Welche IP hat rp01?“. |

### Firewall

Voraussetzung: Firewall-Logdatei eingetragen · Anleitung: [homelab-firewall.md](homelab-firewall.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `firewall_zusammenfassung` | lesen | Fasst die Firewall-Logs (ZyWALL) eines Tages zusammen: geblockte Verbindungen, Port-Scans, ungewöhnliche Ziele. |

### Eigene VMs

Voraussetzung: VM-Token, Host, Template und Netze · Anleitung: [proxmox-vms.md](proxmox-vms.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `vm_planen` | lesen | Plant eine neue VM in Proxmox für einen Zweck (z. B. „Richte einen Minecraft-Server ein“): Template, Ressourcen, VLAN, IP, Name und Playbooks – mit Prüfung der Grenzen. |
| `vm_erstellen` | **zerstörerisch** | Legt die geplante VM an und richtet sie ein – nur nach Louis' Ja. |
| `missa_vms_anzeigen` | lesen | Zeigt die VMs im Pool missa und den Stand der letzten Erstellungs-Aufträge. |
| `playbooks_anzeigen` | lesen | Zeigt, was Missa auf neuen VMs einrichten kann, und die festen Grenzen. |
| `vm_starten` | **zerstörerisch** | Startet eine VM aus dem Pool missa – nur nach Louis' Ja. |
| `vm_stoppen` | **zerstörerisch** | Fährt eine VM aus dem Pool missa herunter – nur nach Louis' Ja. |
| `vm_loeschen` | **zerstörerisch** | Löscht eine VM aus dem Pool missa endgültig – nur nach Louis' Ja. |

### Caddy

Voraussetzung: Caddy-Host und Domain · Anleitung: [caddy-minecraft.md](caddy-minecraft.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `subdomain_vorschlagen` | lesen | Schlägt eine neue Subdomain in Caddy vor und zeigt den fertigen Eintrag – noch nicht aktiv. |
| `subdomain_aktivieren` | **zerstörerisch** | Aktiviert die vorgeschlagene Subdomain (Datei schreiben, caddy validate, neu laden) – nur nach Louis' Ja. |
| `subdomain_entfernen` | **zerstörerisch** | Entfernt eine von Missa angelegte Subdomain wieder – nur nach Louis' Ja. |
| `subdomains_anzeigen` | lesen | Zeigt die Subdomains, die Missa in Caddy angelegt hat. |

### Minecraft

Voraussetzung: RCON eingetragen · Anleitung: [caddy-minecraft.md](caddy-minecraft.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `minecraft_spieler` | lesen | Zeigt, wer gerade auf dem Minecraft-Server online ist („Wer ist online?“). |
| `minecraft_whitelist` | lesen | Zeigt die Whitelist des Minecraft-Servers. |
| `minecraft_whitelist_hinzufuegen` | schreiben | Setzt einen Spieler auf die Whitelist des Minecraft-Servers. |
| `minecraft_whitelist_entfernen` | schreiben | Nimmt einen Spieler von der Whitelist des Minecraft-Servers. |
| `minecraft_neustarten` | **zerstörerisch** | Startet den Minecraft-Server neu – nur nach Louis' Ja; warnt, wenn Spieler online sind. |

### Git-Server und GitHub

Voraussetzung: Git-Server-Token bzw. GitHub-Token (nur lesen) · Anleitung: [git-github-prompts.md](git-github-prompts.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `git_issue_anlegen` | schreiben | Legt auf dem eigenen Git-Server ein Issue an („Leg im Repo X ein Issue an: …“). |
| `git_ueberblick` | lesen | Zeigt den eigenen Git-Server (Gitea/Forgejo), die Repos und fasst offene Pull Requests zusammen. |
| `git_alte_branches` | lesen | Zeigt Branches auf dem Git-Server, die seit Wochen keinen Commit mehr hatten. |
| `github_ueberblick` | lesen | Zeigt die eigenen GitHub-Repos: fehlgeschlagene Builds, neueste Releases, offene Issues (nur lesend). |

### Claude-Code-Prompts

Voraussetzung: Prompt-Vorlage (Issue nur mit Git-Server) · Anleitung: [git-github-prompts.md](git-github-prompts.md)

| Werkzeug | Stufe | Was es tut |
|----------|-------|------------|
| `claude_prompt_erstellen` | schreiben | Macht aus einer groben Idee einen Claude-Code-Prompt nach Louis' Vorlage (Repo-Aufbau und Arbeitsweise kommen aus der Vorlage). |
| `claude_prompt_als_issue` | **zerstörerisch** | Legt einen gezeigten Prompt-Entwurf als Issue im Repo auf dem Git-Server an – nur nach Louis' Ja. |

## Funktionen aus Missa Control

Ist Missa Control angebunden ([missa-control.md](missa-control.md)), kommen alle Funktionen dazu,
die im Panel eingerichtet sind – mit dem Namen aus dem Panel (Vorsilbe der Integration, z. B.
`kalender_termine_anzeigen`). Sie sind `lesen` oder `schreiben`, nie zerstörerisch: Ob eine
Funktion erst nachfragen muss, entscheidet das Panel mit seinen Freigaben. Die Liste wird alle
`MISSA_CONTROL_AKTUALISIEREN` Sekunden (Standard 60) neu geholt, ohne Neustart.
