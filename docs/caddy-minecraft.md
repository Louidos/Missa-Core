# Subdomains in Caddy und Minecraft per RCON

Missa kann neue Subdomains in Caddy auf rp01 vorbereiten. Aktiv werden sie erst nach deinem
Ja. Außerdem verwaltet sie einen Minecraft-Server per RCON: wer online ist, die Whitelist
und der Neustart. Beides ist aus, solange die Einstellungen dafür fehlen.

## Subdomains in Caddy

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Mach spiel.louis-degiorgi.ch auf 10.0.70.120:8080“ | zeigt den fertigen Eintrag und prüft das DNS, schreibt noch nichts | `subdomain_vorschlagen` (lesen) |
| „Ja“ | Datei schreiben → `caddy validate` → `systemctl reload caddy` | `subdomain_aktivieren` (zerstoererisch) |
| „Welche Subdomains hast du angelegt?“ | Liste der eigenen Einträge mit Ziel | `subdomains_anzeigen` (lesen) |
| „Nimm spiel wieder raus“ | Datei löschen, prüfen, neu laden – nur nach Ja | `subdomain_entfernen` (zerstoererisch) |

Ein Eintrag sieht so aus (eine Datei pro Subdomain, `missa-<name>.caddy`):

```caddy
# Von Missa angelegt am 12.11.2026 – missa-spiel.caddy
spiel.louis-degiorgi.ch {
	reverse_proxy 10.0.70.120:8080
}
```

**Ablauf nach dem Ja:**

1. Missa schreibt die Datei in ihren eigenen Ordner `/etc/caddy/missa` auf rp01.
2. `caddy validate` prüft die **ganze** Konfiguration (bestehendes Caddyfile plus Missas
   Dateien).
3. Ist sie gültig, lädt `systemctl reload caddy` sie neu. Laufende Verbindungen bleiben
   bestehen.
4. Ist sie ungültig, oder lässt sich Caddy nicht neu laden, entfernt Missa die Datei sofort
   wieder (ein ersetzter Eintrag bekommt seinen alten Inhalt zurück) und erklärt den Fehler.
   **Caddy läuft dann unverändert mit der alten Konfiguration weiter**, denn `validate`
   ändert am laufenden Caddy nichts.

**DNS:** Beim Vorschlag und nach dem Aktivieren prüft Missa, ob der Name schon im DNS steht
(A, AAAA oder CNAME). Fehlt er, sagt sie dir, welchen Eintrag du beim DNS-Anbieter anlegen
musst (z. B. `CNAME spiel.louis-degiorgi.ch → louis-degiorgi.ch`). Ohne DNS-Eintrag bekommt
Caddy kein Zertifikat.

**Was Missa nicht kann:** das bestehende Caddyfile ändern, die API-Key-Prüfung anfassen,
`import`-Zeilen oder globale Optionen schreiben, Dateien außerhalb von `/etc/caddy/missa`
anfassen, andere Dienste neu laden. Das erzwingt `missa-befehl` auf rp01, nicht nur der Code
auf ai01.

### Einrichtung auf rp01 (von Hand, einmal)

1. Den Benutzer `missa` auf rp01 einrichten bzw. erweitern
   ([scripts/einrichten/missa-benutzer.sh](../scripts/einrichten/missa-benutzer.sh),
   siehe [container-logs-logins.md](container-logs-logins.md)), jetzt mit `--caddy`:

   ```bash
   sudo bash missa-benutzer.sh --caddy --dry-run --schluessel "ssh-ed25519 AAAA… missa@ai01"
   ```

   ```bash
   sudo bash missa-benutzer.sh --caddy --schluessel "ssh-ed25519 AAAA… missa@ai01"
   ```

   Das legt an:
   - den Ordner `/etc/caddy/missa` (gehört `missa`, nur Dateien `missa-*.caddy`),
   - das Programm `/usr/local/lib/missa/missa-caddy`, das nur
     `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile` und
     `systemctl reload caddy` kann,
   - in `/etc/sudoers.d/missa` genau dieses eine Programm zusätzlich (geprüft mit
     `visudo -cf`).

   Andere Pfade gehen über `MISSA_CADDY_ORDNER` und `MISSA_CADDYFILE`.
2. **Eine Zeile im Caddyfile ergänzt du selbst** – das Script ändert das Caddyfile nicht.
   Ganz unten, außerhalb jedes Blocks:

   ```caddy
   import /etc/caddy/missa/missa-*.caddy
   ```

   Danach `sudo caddy validate --config /etc/caddy/Caddyfile` und
   `sudo systemctl reload caddy`. Solange der Ordner leer ist, gibt Caddy nur eine Warnung
   aus und läuft normal.
3. In `/etc/missa/missa.env` auf ai01:

   ```ini
   SSH_HOSTS=…,rp01=10.0.70.10
   CADDY_HOST=rp01
   CADDY_DOMAIN=louis-degiorgi.ch
   ```

   `CADDY_HOST` muss in `SSH_HOSTS` stehen, sonst startet Missa-Core nicht und sagt warum.

## Minecraft per RCON

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Wer ist online?“ | Liste der Spieler (z. B. „Online (2 von 20): Louis, Mia“) | `minecraft_spieler` (lesen) |
| „Wer steht auf der Whitelist?“ | die Whitelist | `minecraft_whitelist` (lesen) |
| „Setz Lea auf die Whitelist“ / „Nimm Tom runter“ | `whitelist add` / `whitelist remove` | `minecraft_whitelist_hinzufuegen`, `minecraft_whitelist_entfernen` (schreiben) |
| „Starte den Minecraft-Server neu“ | fragt nach – **mit Warnung, wenn gerade Spieler online sind** – und startet erst nach Ja neu | `minecraft_neustarten` (zerstoererisch) |

Spielernamen prüft Missa streng (3–16 Zeichen, `A–Z`, `0–9`, `_`). So kann kein anderer
RCON-Befehl über einen Namen hineinrutschen.

**Neustart:** Missa kündigt ihn im Spiel an (wenn jemand online ist) und schickt dann
`save-all` und `stop`. Den Server startet danach systemd neu (`Restart=always` im Dienst
aus [playbooks/minecraft.yml](../playbooks/minecraft.yml)). Läuft der Server bei dir ohne
diesen Dienst, startet er nach `stop` nicht von selbst.

### Einrichtung

In `/etc/missa/missa.env` auf ai01:

```ini
MINECRAFT_RCON_HOST=10.0.70.120
MINECRAFT_RCON_PORT=25575
# Leer lassen, wenn Missa die VM selbst eingerichtet hat (dann steht das Passwort in
# MISSA_DATEN/minecraft-rcon.txt).
MINECRAFT_RCON_PASSWORT=
```

Ohne `MINECRAFT_RCON_HOST` ist alles aus. Ist der Host gesetzt, aber kein Passwort da (weder
`MINECRAFT_RCON_PASSWORT` noch die Datei), bleibt es ebenfalls aus und Missa schreibt eine
Warnung ins Log.

Auf dem Server (`server.properties`): `enable-rcon=true`, `rcon.port=25575`, `rcon.password`.
Hat Missa die VM mit `minecraft.yml` eingerichtet, ist das schon erledigt, und RCON ist nur
von ai01 (`VM_VERWALTUNG_IP`) aus erreichbar. RCON ist unverschlüsselt: Den Port nie nach
außen freigeben.

## Netzfreigaben

Siehe [netzfreigaben.md](netzfreigaben.md): ai01 → rp01 TCP 22 (schon für SSH nötig) und
ai01 → Minecraft-Server TCP 25575.
