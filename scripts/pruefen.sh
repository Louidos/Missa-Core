#!/usr/bin/env bash
# Führt dieselben Prüfungen aus wie die CI: Lint, Formatierung, Shell-Scripts,
# bekannte Lücken in Abhängigkeiten, Geheimnis-Suche und Tests.
set -euo pipefail

cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck scripts/*.sh scripts/einrichten/*.sh scripts/charaktertest
else
    echo "Hinweis: shellcheck ist nicht installiert – Shell-Scripts werden nur in der CI geprüft." >&2
fi
if command -v pip-audit >/dev/null 2>&1; then
    pip-audit --skip-editable --progress-spinner off
else
    echo "Hinweis: pip-audit ist nicht installiert – Abhängigkeiten werden nur in der CI geprüft." >&2
fi
if command -v gitleaks >/dev/null 2>&1; then
    gitleaks git --log-opts="HEAD --tags" --redact --no-banner --exit-code 1 .
else
    echo "Hinweis: gitleaks ist nicht installiert – die Geheimnis-Suche läuft nur in der CI." >&2
fi
pytest "$@"
