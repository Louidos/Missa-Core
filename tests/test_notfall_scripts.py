"""scripts/notfall-stopp.sh und scripts/notfall-freigabe.sh (wie auf ai01, ohne systemd)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from missa.core.config import Einstellungen
from missa.core.rechte import Aktionsprotokoll, rechte_bauen
from missa.core.werkzeuge import Stufe, Werkzeug, Werkzeugkasten

PROJEKT = Path(__file__).resolve().parents[1]
STOPP = PROJEKT / "scripts" / "notfall-stopp.sh"
FREIGABE = PROJEKT / "scripts" / "notfall-freigabe.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="braucht bash")


def starten(script: Path, daten: Path, *argumente: str, python: str = sys.executable, **env):
    umgebung = {
        **os.environ,
        "MISSA_ENV_DATEI": str(daten.parent / "missa.env"),
        "MISSA_PYTHON": python,
        "MISSA_BENUTZER": "gibt-es-nicht",
        "MISSA_FREIGABE_OHNE_ROOT": "1",
        **env,
    }
    umgebung.pop("MISSA_DATEN", None)
    return subprocess.run(
        ["bash", str(script), *argumente],
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def schreiben_geht(daten: Path) -> bool:
    rechte, _ = rechte_bauen(Einstellungen(daten=daten))
    kasten = Werkzeugkasten(
        [
            Werkzeug(
                "notieren",
                "Notiert.",
                {"type": "object", "properties": {}},
                lambda: "ok",
                stufe=Stufe.SCHREIBEN,
            )
        ],
        rechte=rechte,
    )
    return asyncio.run(kasten.ausfuehren("notieren", {})) == "ok"


@pytest.fixture
def daten(tmp_path: Path) -> Path:
    ordner = tmp_path / "daten"
    # Der Datenordner kommt wie auf ai01 aus der missa.env.
    (tmp_path / "missa.env").write_text(f'MISSA_DATEN="{ordner}"\n', encoding="utf-8")
    return ordner


def test_stopp_und_freigabe_per_script(daten: Path):
    assert schreiben_geht(daten)
    ergebnis = starten(STOPP, daten, "Test", "auf", "ai01")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "NOTFALL-STOPP ausgelöst" in ergebnis.stdout
    assert (daten / "notfall-stopp").exists()
    assert not schreiben_geht(daten)  # gilt sofort, ohne Neustart

    abgelehnt = subprocess.run(
        ["bash", str(FREIGABE)],
        input="n\n",
        env={
            **os.environ,
            "MISSA_ENV_DATEI": str(daten.parent / "missa.env"),
            "MISSA_PYTHON": sys.executable,
            "MISSA_FREIGABE_OHNE_ROOT": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert abgelehnt.returncode == 1
    assert (daten / "notfall-stopp").exists()

    freigabe = starten(FREIGABE, daten, "--ja")
    assert freigabe.returncode == 0, freigabe.stderr
    assert "aufgehoben" in freigabe.stdout
    assert schreiben_geht(daten)

    status = [a.status for a in Aktionsprotokoll(daten / "missa.db", Einstellungen().zone).liste()]
    assert "notfall_stopp" in status and "notfall_freigabe" in status


def test_scripts_gehen_auch_ohne_python(daten: Path):
    ergebnis = starten(STOPP, daten, python="/gibt/es/nicht")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert (daten / "notfall-stopp").exists()
    assert not schreiben_geht(daten)
    freigabe = starten(FREIGABE, daten, "--ja", python="/gibt/es/nicht")
    assert freigabe.returncode == 0, freigabe.stderr
    assert schreiben_geht(daten)


def test_freigabe_braucht_root(daten: Path):
    if os.geteuid() == 0:
        pytest.skip("läuft als root")
    starten(STOPP, daten)
    ergebnis = starten(FREIGABE, daten, "--ja", MISSA_FREIGABE_OHNE_ROOT="")
    assert ergebnis.returncode == 1
    assert (daten / "notfall-stopp").exists()
