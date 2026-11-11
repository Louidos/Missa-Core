#!/usr/bin/env bash
# Notfall-Stopp auf ai01: sperrt sofort alle schreibenden und zerstörerischen Werkzeuge
# von Missa – auch über einen Neustart hinweg. Lesen und Reden geht weiter.
#
#   sudo /opt/missa/app/scripts/notfall-stopp.sh ["Grund"]
#
# Aufheben geht nur hier auf ai01: sudo /opt/missa/app/scripts/notfall-freigabe.sh
# Beschreibung: docs/rechte.md

set -Eeuo pipefail

ENV_DATEI="${MISSA_ENV_DATEI:-/etc/missa/missa.env}"
PYTHON="${MISSA_PYTHON:-/opt/missa/venv/bin/python}"
BENUTZER="${MISSA_BENUTZER:-missa}"
GRUND="${*:-per Script auf ai01}"

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
    local daten
    daten="$(daten_ordner)"
    export MISSA_ENV_DATEI="$ENV_DATEI" MISSA_DATEN="$daten"
    if [[ -x "$PYTHON" ]] && als_dienst "$PYTHON" -m missa.core.rechte stopp --grund "$GRUND"; then
        return 0
    fi
    # Notlösung ohne Python: nur die Sperrdatei anlegen (ohne Eintrag im Protokoll).
    echo "Hinweis: Python von Missa nicht nutzbar – lege nur die Sperrdatei an." >&2
    mkdir -p "$daten"
    if [[ ! -e "$daten/notfall-stopp" ]]; then
        printf '{"seit": "%s", "grund": "%s", "quelle": "ai01/script"}\n' \
            "$(date -Iseconds)" "${GRUND//\"/\'}" >"$daten/notfall-stopp"
    fi
    chmod 644 "$daten/notfall-stopp"
    echo "NOTFALL-STOPP ausgelöst: Schreiben und Zerstörerisches sind gesperrt."
}

main "$@"
