#!/usr/bin/env bash
# Spielt ein Backup von Missa ein – z. B. auf einer frischen Installation.
#
#   sudo /opt/missa/app/scripts/wiederherstellen.sh                 zeigt die Backups
#   sudo /opt/missa/app/scripts/wiederherstellen.sh <datei>          fragt vorher nach
#   sudo /opt/missa/app/scripts/wiederherstellen.sh <datei> --ja     ohne Rückfrage
#
# Ablauf: Dienst stoppen, Datenbank, Charakter und Schreibstil zurückspielen (bisherige
# Dateien werden aufgehoben), Rechte für den Dienst-Benutzer setzen, Dienst starten.
# Die echte /etc/missa/missa.env wird nie überschrieben – die Konfiguration aus dem Backup
# (ohne Geheimnisse) landet daneben als missa.env.aus-backup.
#
# Beschreibung: docs/sicherung.md

set -Eeuo pipefail

ENV_DATEI="${MISSA_ENV_DATEI:-/etc/missa/missa.env}"
PYTHON="${MISSA_PYTHON:-/opt/missa/venv/bin/python}"
BENUTZER="${MISSA_BENUTZER:-missa}"
DIENST="${MISSA_DIENST:-missa-core}"
# Nur für die automatischen Tests: erlaubt den Aufruf ohne root und ohne systemd.
OHNE_ROOT="${MISSA_WIEDERHERSTELLEN_OHNE_ROOT:-}"

einstellung() {
    local name="$1" standard="$2" wert=""
    wert="${!name:-}"
    if [[ -z "$wert" && -r "$ENV_DATEI" ]]; then
        wert="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?${name}=//p" "$ENV_DATEI" |
            tail -n 1 | tr -d "\"'" | xargs)"
    fi
    printf '%s\n' "${wert:-$standard}"
}

dienst() {
    if [[ -n "$OHNE_ROOT" ]] || ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    systemctl "$1" "$DIENST" || true
}

main() {
    if [[ "$EUID" -ne 0 && -z "$OHNE_ROOT" ]]; then
        echo "Bitte mit sudo auf ai01 ausführen." >&2
        exit 1
    fi
    export MISSA_ENV_DATEI="$ENV_DATEI"
    if [[ $# -eq 0 ]]; then
        "$PYTHON" -m missa.core.sicherung liste
        echo
        echo "Einspielen: sudo $0 <datei>"
        return 0
    fi
    local datei="$1"
    local ordner daten
    daten="$(einstellung MISSA_DATEN /opt/missa/data)"
    ordner="$(einstellung MISSA_BACKUP_ORDNER "$daten/backup")"
    if [[ ! -f "$datei" && -f "$ordner/$datei" ]]; then
        datei="$ordner/$datei"
    fi
    if [[ ! -f "$datei" ]]; then
        echo "Backup $datei nicht gefunden." >&2
        exit 1
    fi
    echo "Backup: $datei"
    if [[ "${2:-}" != "--ja" ]]; then
        local antwort=""
        read -r -p "Missa wird gestoppt und der Stand aus dem Backup eingespielt. Weiter? [j/N] " \
            antwort || true
        if [[ ! "$antwort" =~ ^[jJyY] ]]; then
            echo "Abgebrochen – nichts verändert."
            exit 1
        fi
    fi
    dienst stop
    if ! "$PYTHON" -m missa.core.sicherung wiederherstellen "$datei"; then
        echo "Wiederherstellen fehlgeschlagen – Missa wird mit dem bisherigen Stand gestartet." >&2
        dienst start
        exit 1
    fi
    if [[ "$EUID" -eq 0 ]] && id "$BENUTZER" >/dev/null 2>&1; then
        chown -R "$BENUTZER:$BENUTZER" "$daten"
    fi
    dienst start
    echo "Fertig. Missa läuft wieder mit dem Stand aus dem Backup."
}

main "$@"
