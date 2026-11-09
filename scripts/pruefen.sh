#!/usr/bin/env bash
# Führt dieselben Prüfungen aus wie die CI: Lint, Formatierung, Tests.
set -euo pipefail

cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
pytest "$@"
