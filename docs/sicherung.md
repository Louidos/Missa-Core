# Missa sichern und wiederherstellen

## Was gesichert wird

Ein Backup ist **eine** Datei `missa-backup-JJJJMMTT-HHMMSS.tar.gz` (nur für den Dienst-Benutzer
lesbar) mit:

| Datei | Inhalt |
|-------|--------|
| `missa.db` | Gedächtnis und Korrekturen, Gesprächsverlauf, Regeln und im Gespräch geänderte Einstellungen (Pausen, Nachtschicht …), Erinnerungen, Routinen, Nachtaufgaben, Kontaktpflege, Protokolle |
| `charakter.md` | Missas Charakterprofil (`MISSA_CHARAKTER_DATEI`) |
| `schreibstil.md` | dein Schreibstil-Profil, falls vorhanden |
| `missa.env.ohne-geheimnisse` | die Konfiguration – **alle Passwörter, Schlüssel und Tokens geleert** |
| `manifest.json` | Version und Zeitpunkt |

Die Datenbank wird über die SQLite-Backup-Funktion kopiert – auch während Missa läuft, ist die
Kopie in sich stimmig.

## Täglich automatisch

Jede Nacht um `MISSA_BACKUP_ZEIT` (Standard 03:30) legt der Zeitplaner ein Backup in
`MISSA_BACKUP_ORDNER` an (Standard `MISSA_DATEN/backup`, also `/opt/missa/data/backup`) und
löscht ältere, sodass die neuesten `MISSA_BACKUP_BEHALTEN` (Standard 14) bleiben. Andere
Dateien im Ordner fasst sie nicht an. War ai01 um 03:30 aus, wird das Backup nach dem Start
nachgeholt.

- Sofort ein Backup: im Chat „Mach ein Backup“ oder auf ai01 `sudo /opt/missa/app/scripts/sichern.sh`.
- Liste: „Welche Backups gibt es?“ oder `sudo /opt/missa/app/scripts/wiederherstellen.sh`.
- **Eigener Ordner** (z. B. eine zweite Platte): `MISSA_BACKUP_ORDNER=/opt/missa/backup` setzen
  und den Ordner für `missa` anlegen (`sudo install -d -o missa -g missa -m 750 /opt/missa/backup`).
  Die systemd-Unit erlaubt diesen Pfad schon (`ReadWritePaths=-/opt/missa/backup`); übernimm
  dafür einmal die neue Unit aus `deploy/missa-core.service` (siehe [update.md](update.md)).
- Alles bleibt lokal. Willst du die Backups zusätzlich woanders haben, kopiere den Ordner selbst
  (z. B. per `rsync` auf int01).

## Wiederherstellen (z. B. auf frischer Installation)

1. Missa-Core installieren wie in [installation.md](installation.md) (Code, venv,
   `/etc/missa/missa.env` mit den Zugangsdaten, Dienst).
2. Backup-Datei auf ai01 kopieren, z. B. nach `/opt/missa/data/backup/`.
3. Einspielen:

```bash
sudo /opt/missa/app/scripts/wiederherstellen.sh missa-backup-20261112-033000.tar.gz
```

Das Script fragt nach, stoppt `missa-core`, prüft das Backup (Manifest, Datenbank-Integrität),
spielt Datenbank, Charakter und Schreibstil zurück, setzt die Rechte für `missa` und startet den
Dienst wieder. Bestehende Dateien werden **nicht weggeworfen**, sondern als
`….vor-wiederherstellung-<Zeit>` daneben aufgehoben.

4. Die Konfiguration aus dem Backup liegt danach als `/etc/missa/missa.env.aus-backup` neben
   deiner `missa.env` – **deine `missa.env` wird nie überschrieben**. Vergleiche beide und trag
   fehlende Werte nach; Passwörter und Schlüssel sind im Backup absichtlich leer.

Danach sind Gedächtnis, Regeln und Routinen wieder da. Ohne Rückfrage: `--ja` anhängen. Von Hand
(ohne Script): `python -m missa.core.sicherung erstellen | liste | wiederherstellen <datei>`.
