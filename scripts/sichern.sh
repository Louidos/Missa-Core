#!/usr/bin/env bash
# Legt sofort ein Backup von Missa an (täglich passiert das ohnehin automatisch).
#
#   sudo /opt/missa/app/scripts/sichern.sh
#
# Beschreibung: docs/sicherung.md

set -Eeuo pipefail

ENV_DATEI="${MISSA_ENV_DATEI:-/etc/missa/missa.env}"
PYTHON="${MISSA_PYTHON:-/opt/missa/venv/bin/python}"
BENUTZER="${MISSA_BENUTZER:-missa}"

export MISSA_ENV_DATEI="$ENV_DATEI"
if [[ "$EUID" -eq 0 ]] && id "$BENUTZER" >/dev/null 2>&1; then
    exec runuser -u "$BENUTZER" -- "$PYTHON" -m missa.core.sicherung erstellen
fi
exec "$PYTHON" -m missa.core.sicherung erstellen
