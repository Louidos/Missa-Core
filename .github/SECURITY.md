# Sicherheitslücken melden

Bitte melde eine Sicherheitslücke **nicht** in einem öffentlichen Issue, Pull Request oder
Kommentar.

## So meldest du vertraulich

1. Im Repo oben auf **Security** klicken.
2. **Report a vulnerability** wählen (GitHub „Private vulnerability reporting“).
3. Beschreiben:
   - Was ist betroffen (Datei, Funktion, Version)?
   - Wie lässt es sich auslösen (Schritte oder ein kleines Beispiel)?
   - Was kann ein Angreifer damit erreichen?

Die Meldung sehen nur du und der Besitzer des Repos. Du bekommst eine Rückmeldung, sobald
sie gelesen ist. Details werden erst veröffentlicht, wenn eine korrigierte Version da ist.

## Welche Versionen Korrekturen bekommen

| Version | Korrekturen |
|---|---|
| neueste Version (aktuell 0.10.x) | ja |
| ältere Versionen | nein – bitte auf die neueste Version aktualisieren (`sudo ./scripts/update.sh`) |

Eine Korrektur erscheint als neue PATCH-Version (z. B. 0.10.1 → 0.10.2) und steht im
[CHANGELOG](../CHANGELOG.md) unter „Behoben“.

## Mehr dazu

Was bisher geprüft wurde, welche Schutzmassnahmen es gibt und was der Besitzer selbst tun
muss: [docs/sicherheit.md](../docs/sicherheit.md).
