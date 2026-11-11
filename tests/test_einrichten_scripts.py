"""Einrichtungs-Scripts für andere Hosts (laufen nur von Hand, hier mit gefälschten Befehlen)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "einrichten"
PROXMOX = SCRIPTS / "proxmox-lesetoken.sh"


def falscher_befehl(ordner: Path, name: str, ausgaben: dict[str, str]) -> Path:
    """Legt ein Programm ``name`` an, das jeden Aufruf protokolliert und Ausgaben vorgibt."""
    protokoll = ordner / f"{name}.log"
    faelle = "\n".join(
        f"    *\"{muster}\"*) printf '%s\\n' '{ausgabe}' ;;" for muster, ausgabe in ausgaben.items()
    )
    programm = ordner / name
    programm.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{protokoll}"\n'
        'case "$*" in\n'
        f"{faelle}\n"
        "esac\n",
        encoding="utf-8",
    )
    programm.chmod(0o755)
    return protokoll


def ausfuehren(script: Path, ordner: Path, *argumente: str) -> subprocess.CompletedProcess:
    umgebung = {**os.environ, "PATH": f"{ordner}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(script), *argumente],
        capture_output=True,
        text=True,
        env=umgebung,
        check=False,
    )


def test_proxmox_dry_run_zeigt_nur_lesende_rechte(tmp_path):
    protokoll = falscher_befehl(tmp_path, "pveum", {"user list": "[]", "user token list": "[]"})
    ergebnis = ausfuehren(PROXMOX, tmp_path, "--dry-run")
    assert ergebnis.returncode == 0, ergebnis.stderr
    ausgabe = ergebnis.stdout
    assert "würde ausführen: pveum user add missa@pve" in ausgabe
    assert "acl modify / --users missa@pve --roles PVEAuditor --propagate 1" in ausgabe
    assert "user token add missa@pve monitor --privsep 1" in ausgabe
    assert "acl modify / --tokens missa@pve!monitor --roles PVEAuditor" in ausgabe
    assert "PROXMOX_TOKEN_ID=missa@pve!monitor" in ausgabe
    # Im Dry-Run wurde nur gelesen.
    aufrufe = protokoll.read_text(encoding="utf-8").splitlines()
    assert aufrufe and all(a.startswith(("user list", "user token list")) for a in aufrufe)


def test_proxmox_vorhandenes_wird_nicht_doppelt_angelegt(tmp_path):
    falscher_befehl(
        tmp_path,
        "pveum",
        {
            "user list": '[{"userid":"missa@pve"}]',
            "user token list": '[{"tokenid": "monitor"}]',
        },
    )
    ergebnis = ausfuehren(PROXMOX, tmp_path, "--dry-run")
    assert "Benutzer missa@pve gibt es schon" in ergebnis.stdout
    assert "Token missa@pve!monitor gibt es schon" in ergebnis.stdout
    assert "user add" not in ergebnis.stdout and "token add" not in ergebnis.stdout


def test_proxmox_vergibt_nur_die_rolle_pveauditor():
    inhalt = PROXMOX.read_text(encoding="utf-8")
    assert re.findall(r'^ROLLE="([^"]+)"', inhalt, re.MULTILINE) == ["PVEAuditor"]
    rollen = re.findall(r"--roles\s+(\S+)", inhalt)
    assert rollen and set(rollen) == {'"$ROLLE"'}
    for verboten in ("Administrator", "PVEVMAdmin", "PVEAdmin", "PowerMgmt", "VM.Allocate"):
        assert verboten not in inhalt


def test_proxmox_ohne_pveum_bricht_ab(tmp_path):
    leer = tmp_path / "leer"
    leer.mkdir()
    ergebnis = subprocess.run(
        ["/bin/bash", str(PROXMOX)],
        capture_output=True,
        text=True,
        env={"PATH": str(leer)},
        check=False,
    )
    assert ergebnis.returncode == 1 and "pveum fehlt" in ergebnis.stderr


@pytest.mark.parametrize("script", sorted(SCRIPTS.glob("*.sh")), ids=lambda p: p.name)
def test_einrichtungs_scripts_sind_gueltiges_bash(script):
    assert os.access(script, os.X_OK), f"{script.name} ist nicht ausführbar"
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
