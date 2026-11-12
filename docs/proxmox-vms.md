# VMs in Proxmox erstellen und einrichten

Auf Zuruf („Richte einen Minecraft-Server ein“) legt Missa eine VM aus einem
Cloud-Init-Template an und richtet den Dienst per Ansible-Playbook ein. Vorher zeigt sie den
Plan, sie legt erst nach deinem Ja an, und es gelten harte Grenzen. Alles passiert nur in
einem eigenen Pool `missa`: Andere VMs kann Missa weder starten, stoppen noch löschen.

## Was du sagen kannst

| Du sagst | Missa macht | Werkzeug (Stufe) |
|----------|-------------|------------------|
| „Richte einen Minecraft-Server ein“ (auch „… mit Version 1.21.4“, „… mit 6 GB RAM“) | zeigt den Plan, legt noch nichts an | `vm_planen` (lesen) |
| „Ja, mach“ | legt an und richtet ein (im Hintergrund, einige Minuten) | `vm_erstellen` (zerstoererisch) |
| „Was kannst du einrichten?“ | Katalog und Grenzen | `playbooks_anzeigen` (lesen) |
| „Welche VMs hast du angelegt?“ | VMs im Pool und Stand der Aufträge | `missa_vms_anzeigen` (lesen) |
| „Stopp mc01“ / „Starte mc01“ / „Lösch mc01“ | nur Pool-VMs, nur nach Ja | `vm_stoppen`, `vm_starten`, `vm_loeschen` (zerstoererisch) |

## Ablauf

1. **Plan:** Template (`VM_TEMPLATE_ID`), Ressourcen (Standard aus
   [playbooks/katalog.yml](../playbooks/katalog.yml), änderbar), VLAN und freie IP aus
   `VM_NETZE`, Name (z. B. `mc01`) und Playbooks. Die Grenzen werden sofort geprüft:
   - pro VM höchstens `VM_MAX_CPU` (4), `VM_MAX_RAM_GB` (8), `VM_MAX_DISK_GB` (64),
   - der ganze Pool (bestehende VMs plus die neue) höchstens `VM_POOL_MAX_CPU` (8),
     `VM_POOL_MAX_RAM_GB` (16), `VM_POOL_MAX_DISK_GB` (200),
   - nur VLANs aus `VM_NETZE`, VM-IDs nur aus `VM_ID_BEREICH` (200–249).

   Über den Grenzen lehnt Missa ab und sagt, warum.
2. **Nach deinem Ja**, im Hintergrund, jeder Schritt mit Eintrag im Aktionsprotokoll
   (`vm_erstellen/…`):
   1. nächste freie VM-ID aus dem Bereich
   2. Vollklon aus dem Template in den Pool `missa`
   3. CPU, RAM, `net0` mit VLAN-Tag, Cloud-Init `ipconfig0` (feste IP), Benutzer
      `VM_BENUTZER` und Missas öffentlicher SSH-Schlüssel (`SSH_SCHLUESSEL.pub`)
   4. Disk auf die geplante Größe
   5. starten und warten, bis SSH antwortet
   6. Playbooks der Reihe nach, z. B. `basis.yml` → `minecraft.yml`
   7. fertig melden (`info`) und ins Homelab-Wissen aufnehmen

   Geht ein Schritt schief, kommt ein `wichtig` mit dem Schritt und dem Grund. Die VM bleibt
   zum Nachsehen stehen.

## Playbooks

| Datei | Was es tut |
|-------|-----------|
| `basis.yml` | Updates, Gast-Agent (damit Proxmox die IP sieht), Zeitzone, automatische Sicherheitsupdates, Firewall auf der VM (ufw: eingehend zu, SSH nur von ai01) |
| `minecraft.yml` | Java 21, Benutzer `minecraft`, Server-Jar der gewünschten Version (von Mojang, mit SHA1-Prüfung), Datapacks-Ordner, Whitelist an, **RCON an** (Port 25575, nur von ai01), systemd-Dienst, der nach `stop` wieder startet. „Vanilla+“: Datapacks in `world/datapacks`, ein eigenes Server-Jar (z. B. Fabric) mit `minecraft_server_url` |

Ein neuer Dienst ist ein neues Playbook in `playbooks/` und ein Eintrag in
`playbooks/katalog.yml` mit Beschreibung, Playbooks, Standard-Ressourcen und Stichwörtern.

Das RCON-Passwort kommt aus `MINECRAFT_RCON_PASSWORT`. Ist es leer, erzeugt Missa eins und
legt es in `MISSA_DATEN/minecraft-rcon.txt` ab, nur für `missa` lesbar. Mit dem Einrichten
wird die [Minecraft-EULA](https://aka.ms/MinecraftEULA) akzeptiert.

## Einrichten

1. **Cloud-Init-Template** auf pve-id01, z. B. aus dem Debian-13-Cloud-Image, mit
   `qemu-guest-agent` und Disk `scsi0` (`VM_DISK`), als Template markiert.
2. **Zugang** auf pve-id01 als root, von Hand:

   ```bash
   bash proxmox-missa.sh --template 9000 --storage local-lvm --vlans 70 --dry-run
   ```

   ```bash
   bash proxmox-missa.sh --template 9000 --storage local-lvm --vlans 70
   ```

   [scripts/einrichten/proxmox-missa.sh](../scripts/einrichten/proxmox-missa.sh) legt den
   Pool `missa`, die Rolle `MissaVMs` und das Token `missa@pve!vms` (privsep) an. Rechte gibt
   es nur auf `/pool/missa`, auf das eine Template (`PVETemplateUser`), auf das eine Storage
   (`PVEDatastoreUser`) und auf die erlaubten VLANs (`PVESDNUser` auf
   `/sdn/zones/localnetwork/<bridge>/<vlan>`). Auf andere VMs gibt es keine Rechte.
3. **Ansible auf ai01:**

   ```bash
   sudo -u missa /opt/missa/venv/bin/pip install "ansible-core>=2.16"
   ```

4. **Einstellungen** in `/etc/missa/missa.env`: `PROXMOX_VM_TOKEN_ID`,
   `PROXMOX_VM_TOKEN_SECRET`, `VM_NODE`, `VM_TEMPLATE_ID`, `VM_NETZE` (z. B.
   `70:10.0.70.100-10.0.70.149/24:10.0.70.1`) und `VM_VERWALTUNG_IP` (IP von ai01, nur sie darf
   per SSH und RCON auf die neuen VMs). Danach `sudo systemctl restart missa-core`.

Netzfreigaben: [netzfreigaben.md](netzfreigaben.md).

**Hinweis:** Für den ersten Ansible-Lauf ist die Host-Schlüssel-Prüfung aus, denn die VM ist
gerade frisch aus dem eigenen Template entstanden und ihr Schlüssel kann noch nirgends
bekannt sein. Die IP liegt im eigenen VLAN.
