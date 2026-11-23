#!/usr/bin/env bash
# Legt auf ai01 ein Gerät für den Geräte-Anschluss an (z. B. den Laptop mit hypr-shell).
# Louis führt das Script von Hand aus – Missa selbst nie.
#
#   sudo /opt/missa/app/scripts/einrichten/geraet-anlegen.sh laptop        # neues Gerät
#   sudo /opt/missa/app/scripts/einrichten/geraet-anlegen.sh --sperren laptop
#   sudo /opt/missa/app/scripts/einrichten/geraet-anlegen.sh --liste
#
# Der Token wird genau einmal angezeigt; gespeichert ist nur sein Hash (SHA-256) in
# MISSA_DATEN/missa.db. Gesperrt gilt der Token sofort nicht mehr, auch ohne Neustart.
# Für einen neuen Token: erst sperren, dann neu anlegen. Beschreibung: docs/geraete.md

set -Eeuo pipefail

ENV_DATEI="${MISSA_ENV_DATEI:-/etc/missa/missa.env}"
PYTHON="${MISSA_PYTHON:-/opt/missa/venv/bin/python}"
BENUTZER="${MISSA_BENUTZER:-missa}"

hilfe() { sed -n '2,/^$/p' "$0"; }
abbruch() {
    echo "Fehler: $*" >&2
    exit 1
}

# Datenordner wie der Dienst: Umgebung, sonst missa.env, sonst Standard.
daten_ordner() {
    local wert="${MISSA_DATEN:-}"
    if [[ -z "$wert" && -r "$ENV_DATEI" ]]; then
        wert="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?MISSA_DATEN=//p' "$ENV_DATEI" |
            tail -n 1 | tr -d "\"'" | xargs)"
    fi
    printf '%s\n' "${wert:-/opt/missa/data}"
}

# Als Dienst-Benutzer ausführen, damit die Datenbank ihm gehört.
als_dienst() {
    if [[ "$EUID" -eq 0 ]] && id "$BENUTZER" >/dev/null 2>&1; then
        runuser -u "$BENUTZER" -- "$@"
    else
        "$@"
    fi
}

main() {
    local befehl="anlegen" name=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --sperren)
                [[ $# -ge 2 ]] || abbruch "--sperren braucht den Namen des Geräts"
                befehl="sperren"
                name="$2"
                shift
                ;;
            --liste) befehl="liste" ;;
            -h | --hilfe | --help)
                hilfe
                return 0
                ;;
            -*) abbruch "Unbekannte Option: $1 (siehe --hilfe)" ;;
            *)
                [[ -z "$name" ]] || abbruch "Nur ein Gerätename erlaubt"
                name="$1"
                ;;
        esac
        shift
    done
    if [[ "$befehl" != "liste" && -z "$name" ]]; then
        hilfe
        abbruch "Gerätename fehlt, z. B. laptop"
    fi
    [[ -x "$PYTHON" ]] || abbruch "Python von Missa nicht gefunden: $PYTHON"

    export MISSA_ENV_DATEI="$ENV_DATEI" MISSA_DATEN
    MISSA_DATEN="$(daten_ordner)"
    if [[ "$befehl" == "liste" ]]; then
        als_dienst "$PYTHON" -m missa.core.geraete liste
    else
        als_dienst "$PYTHON" -m missa.core.geraete "$befehl" "$name"
    fi
}

main "$@"
