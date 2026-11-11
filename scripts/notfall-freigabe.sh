#!/usr/bin/env bash
# Hebt den Notfall-Stopp von Missa auf – geht absichtlich NUR hier auf ai01, nie per Chat.
#
#   sudo /opt/missa/app/scripts/notfall-freigabe.sh        fragt vorher nach
#   sudo /opt/missa/app/scripts/notfall-freigabe.sh --ja   ohne Rückfrage
#
# Beschreibung: docs/rechte.md

set -Eeuo pipefail

ENV_DATEI="${MISSA_ENV_DATEI:-/etc/missa/missa.env}"
PYTHON="${MISSA_PYTHON:-/opt/missa/venv/bin/python}"
BENUTZER="${MISSA_BENUTZER:-missa}"
# Nur für die automatischen Tests: erlaubt den Aufruf ohne root.
OHNE_ROOT="${MISSA_FREIGABE_OHNE_ROOT:-}"

daten_ordner() {
    local wert="${MISSA_DATEN:-}"
    if [[ -z "$wert" && -r "$ENV_DATEI" ]]; then
        wert="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?MISSA_DATEN=//p' "$ENV_DATEI" |
            tail -n 1 | tr -d "\"'" | xargs)"
    fi
    printf '%s\n' "${wert:-/opt/missa/data}"
}

als_dienst() {
    if [[ "$EUID" -eq 0 ]] && id "$BENUTZER" >/dev/null 2>&1; then
        runuser -u "$BENUTZER" -- "$@"
    else
        "$@"
    fi
}

main() {
    if [[ "$EUID" -ne 0 && -z "$OHNE_ROOT" ]]; then
        echo "Bitte mit sudo auf ai01 ausführen." >&2
        exit 1
    fi
    local daten
    daten="$(daten_ordner)"
    export MISSA_ENV_DATEI="$ENV_DATEI" MISSA_DATEN="$daten"
    if [[ ! -e "$daten/notfall-stopp" ]]; then
        echo "Es ist kein Notfall-Stopp aktiv."
        return 0
    fi
    echo "Aktiver Notfall-Stopp: $(cat "$daten/notfall-stopp")"
    if [[ "${1:-}" != "--ja" ]]; then
        local antwort=""
        read -r -p "Wirklich freigeben? Missa darf danach wieder schreiben. [j/N] " antwort || true
        if [[ ! "$antwort" =~ ^[jJyY] ]]; then
            echo "Nicht freigegeben."
            exit 1
        fi
    fi
    if [[ -x "$PYTHON" ]] && als_dienst "$PYTHON" -m missa.core.rechte freigabe; then
        return 0
    fi
    echo "Hinweis: Python von Missa nicht nutzbar – entferne nur die Sperrdatei." >&2
    rm -f "$daten/notfall-stopp"
    echo "Notfall-Stopp aufgehoben. Missa darf wieder schreiben."
}

main "$@"
