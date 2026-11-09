"""Startet Missa-Core: ``missa-core`` oder ``python -m missa.core``."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from missa.core.app import create_app
from missa.core.config import (
    STANDARD_ENV_DATEI,
    Einstellungen,
    KonfigurationsFehler,
    env_datei_laden,
)


def main() -> None:
    env_datei_laden(Path(os.environ.get("MISSA_ENV_DATEI", str(STANDARD_ENV_DATEI))))
    stufe = os.environ.get("MISSA_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=stufe, format="%(levelname)s %(name)s: %(message)s")
    # Jede einzelne Anfrage an llama-server muss nicht im Journal stehen.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        einstellungen = Einstellungen.aus_umgebung()
        einstellungen.pruefen()
    except KonfigurationsFehler as fehler:
        print(f"Konfigurationsfehler: {fehler}", file=sys.stderr)
        sys.exit(2)

    uvicorn.run(
        create_app(einstellungen),
        host=einstellungen.host,
        port=einstellungen.port,
        log_level=stufe.lower(),
    )


if __name__ == "__main__":
    main()
