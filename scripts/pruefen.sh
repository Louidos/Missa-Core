#!/usr/bin/env bash
# Führt dieselben Prüfungen aus wie die CI: Lint, Formatierung, Shell-Scripts, Tests.
set -euo pipefail

cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck scripts/*.sh scripts/einrichten/*.sh
else
    echo "Hinweis: shellcheck ist nicht installiert – Shell-Scripts werden nur in der CI geprüft." >&2
fi
pytest "$@"
