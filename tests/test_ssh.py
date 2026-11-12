"""SSH-Anbindung, missa-befehl auf dem Host und der Inhalt der sudoers-Datei."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from missa.integrationen.ssh import SshFehler, SshVerbindung, hosts_lesen
from tests.ssh_fake import FakeHosts

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "einrichten" / "missa-benutzer.sh"
SCHLUESSEL = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBmissatestschluessel missa@ai01"


# ----------------------------------------------------------------------------- Missa-Seite


def test_hosts_lesen():
    hosts = hosts_lesen("WB01=10.0.20.10, int01=10.0.30.20:2222;rp01")
    assert hosts["wb01"].adresse == "10.0.20.10" and hosts["wb01"].port == 22
    assert hosts["int01"].port == 2222
    assert hosts["rp01"].adresse == "rp01"
    for falsch in ("wb01=10.0.0.1|rm", "wb01=a:b", "w!b=1.2.3.4", "x=$(id)", "x=a b:c"):
        with pytest.raises(ValueError):
            hosts_lesen(falsch)


def test_kommando_ohne_shell_mit_strenger_pruefung():
    ssh = SshVerbindung(
        hosts_lesen("wb01=10.0.20.10"),
        schluessel=Path("/etc/missa/ssh/id_ed25519"),
        known_hosts=Path("/etc/missa/ssh/known_hosts"),
    )
    kommando = ssh.kommando(ssh.host("wb01"), "docker-logs", ("nextcloud", "50"))
    assert kommando[0] == "ssh"
    assert {"BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"} <= set(kommando)
    assert "UserKnownHostsFile=/etc/missa/ssh/known_hosts" in kommando
    assert kommando[-5:] == ["missa@10.0.20.10", "--", "docker-logs", "nextcloud", "50"]


@pytest.mark.parametrize("angabe", ["x; rm -rf /", "a b", "$(id)", "`id`", "a|b", "'x'", ""])
def test_unsichere_angaben_werden_abgelehnt(angabe):
    fake = FakeHosts()
    with pytest.raises(SshFehler, match="Unzulässige Angabe"):
        asyncio.run(fake.verbindung().befehl("wb01", "docker-logs", angabe, "10"))
    assert fake.aufrufe == []


def test_nur_erlaubte_befehle_und_hosts():
    fake = FakeHosts()
    ssh = fake.verbindung()
    for befehl in ("bash", "docker-exec", "apt-get", "rm"):
        with pytest.raises(SshFehler, match="nicht erlaubt"):
            asyncio.run(ssh.befehl("wb01", befehl))
    with pytest.raises(SshFehler, match="kenne ich nicht"):
        asyncio.run(ssh.befehl("pve-id01", "pruefen"))
    assert fake.aufrufe == []


def test_fehler_werden_verstaendlich():
    fake = FakeHosts()
    fake.offline.add("wb01")
    fake.antworten[("int01", "docker-ps")] = (64, "", "missa-befehl: Befehl nicht erlaubt")
    fake.antworten[("rp01", "pruefen")] = TimeoutError()
    ssh = fake.verbindung()
    with pytest.raises(SshFehler, match="Keine SSH-Verbindung zu wb01"):
        asyncio.run(ssh.befehl("wb01", "pruefen"))
    with pytest.raises(SshFehler, match="docker-ps auf int01 fehlgeschlagen"):
        asyncio.run(ssh.befehl("int01", "docker-ps"))
    with pytest.raises(SshFehler, match="rp01 antwortet nicht"):
        asyncio.run(ssh.befehl("rp01", "pruefen"))


# ----------------------------------------------------------------------------- Host-Seite


@pytest.fixture
def fakes(tmp_path: Path) -> Path:
    """Ordner mit gefälschten Programmen, die ihre Aufrufe protokollieren (nie die echten!)."""
    ordner = tmp_path / "bin"
    ordner.mkdir()
    for name in (
        "docker", "sudo", "journalctl", "tail", "apt-get", "apt", "shutdown", "caddy",
        "systemctl",
    ):  # fmt: skip
        programm = ordner / name
        programm.write_text(
            f'#!/bin/bash\nprintf "%s\\n" "{name} $*" >> "{tmp_path}/aufrufe.log"\n',
            encoding="utf-8",
        )
        programm.chmod(0o755)
    return ordner


def erzeugen(
    fakes: Path,
    *argumente: str,
    wrapper: Path | None = None,
    update: Path | None = None,
    **extra: str,
) -> str:
    umgebung = {**os.environ, "PATH": f"{fakes}:{os.environ['PATH']}", **extra}
    if wrapper is not None:
        umgebung["MISSA_DOCKER_DATEI"] = str(wrapper)
    if update is not None:
        umgebung["MISSA_UPDATE_DATEI"] = str(update)
    ergebnis = subprocess.run(
        ["bash", str(SCRIPT), *argumente], capture_output=True, text=True, env=umgebung, check=True
    )
    return ergebnis.stdout


@pytest.fixture
def wrapper(tmp_path: Path, fakes: Path) -> Path:
    """Das erzeugte missa-docker (ruft das gefälschte docker mit absolutem Pfad auf)."""
    datei = tmp_path / "missa-docker"
    datei.write_text(erzeugen(fakes, "--nur-docker"), encoding="utf-8")
    datei.chmod(0o755)
    return datei


@pytest.fixture
def update(tmp_path: Path, fakes: Path) -> Path:
    """Das erzeugte missa-update (ruft gefälschtes apt-get und shutdown mit absolutem Pfad auf)."""
    datei = tmp_path / "missa-update"
    datei.write_text(erzeugen(fakes, "--nur-update"), encoding="utf-8")
    datei.chmod(0o755)
    return datei


@pytest.fixture
def befehl(tmp_path: Path, fakes: Path, wrapper: Path, update: Path) -> Path:
    datei = tmp_path / "missa-befehl"
    datei.write_text(
        erzeugen(
            fakes,
            "--nextcloud-log",
            "/var/log/nextcloud/nextcloud.log",
            "--nur-befehl",
            wrapper=wrapper,
            update=update,
        ),
        encoding="utf-8",
    )
    datei.chmod(0o755)
    return datei


def protokoll_lesen(fakes: Path) -> list[str]:
    protokoll = fakes.parent / "aufrufe.log"
    aufrufe = protokoll.read_text(encoding="utf-8").splitlines() if protokoll.exists() else []
    protokoll.unlink(missing_ok=True)
    return aufrufe


def ausfuehren(befehl: Path, fakes: Path, ssh_befehl: str) -> tuple[int, list[str], str]:
    protokoll_lesen(fakes)
    umgebung = {
        "SSH_ORIGINAL_COMMAND": ssh_befehl,
        "MISSA_TEST_PATH": f"{fakes}:/usr/bin:/bin",
        "PATH": "/usr/bin:/bin",
    }
    ergebnis = subprocess.run(
        ["bash", str(befehl)], capture_output=True, text=True, env=umgebung, check=False
    )
    return ergebnis.returncode, protokoll_lesen(fakes), ergebnis.stderr


@pytest.mark.parametrize(
    ("ssh_befehl", "erwartet"),
    [
        ("docker-ps", "sudo -n {w} ps"),
        ("docker-stats", "sudo -n {w} stats"),
        ("docker-logs nextcloud 50", "sudo -n {w} logs nextcloud 50"),
        ("docker-restart nextcloud", "sudo -n {w} restart nextcloud"),
        (
            "journal unit=caddy.service prio=3 seit=@1700000000 zeilen=100",
            "journalctl --no-pager --quiet -o short-iso -n 500 -u caddy.service -p 3 "
            "--since @1700000000 -n 100",
        ),
        ("journal unit=*nextcloud* zeilen=20", "journalctl --no-pager --quiet -o short-iso "
         "-n 500 -u *nextcloud* -n 20"),
        (
            "ssh-log seit=@1700000000 zeilen=5000",
            "journalctl --no-pager --quiet -o short-iso -n 500 -t sshd -t sshd-session "
            "--since @1700000000 -n 5000",
        ),
        ("nextcloud-log 200", "tail -n 200 -- /var/log/nextcloud/nextcloud.log"),
        ("apt-update", "sudo -n {u} update"),
        ("apt-upgrade", "sudo -n {u} upgrade"),
        ("neustart", "sudo -n {u} reboot"),
        ("apt-liste", "apt list --upgradable"),
    ],
)  # fmt: skip
def test_missa_befehl_fuehrt_erlaubtes_aus(befehl, fakes, wrapper, update, ssh_befehl, erwartet):
    code, aufrufe, fehler = ausfuehren(befehl, fakes, ssh_befehl)
    assert code == 0, fehler
    assert aufrufe == [erwartet.format(w=wrapper, u=update)]


def test_neustart_noetig_liest_nur(befehl, fakes):
    code, aufrufe, _ = ausfuehren(befehl, fakes, "neustart-noetig")
    assert code == 0 and aufrufe == []


@pytest.mark.parametrize(
    "ssh_befehl",
    [
        "",
        "bash",
        "sh -c id",
        "docker-exec nextcloud sh",
        "docker run alpine",
        "docker-logs x;id 5",
        "docker-logs nextcloud 99999",
        "docker-logs nextcloud",
        "docker-restart a b",
        "docker-restart ../x",
        "docker-ps --all",
        "journal --vacuum-time=1s",
        "journal vacuum=1s",
        "journal seit=yesterday",
        "journal unit=x;id",
        "journal prio=9",
        "ssh-log unit=sshd",
        "nextcloud-log 10 /etc/shadow",
        "apt-update --allow-insecure-repositories",
        "apt-upgrade -y",
        "apt-get install foo",
        "apt-install foo",
        "apt-liste --all",
        "neustart now",
        "reboot",
        "shutdown -h now",
    ],
)
def test_missa_befehl_lehnt_alles_andere_ab(befehl, fakes, ssh_befehl):
    code, aufrufe, fehler = ausfuehren(befehl, fakes, ssh_befehl)
    assert code != 0
    assert aufrufe == []
    assert "missa-befehl:" in fehler


# ----------------------------------------------------------------------------- sudo


def sudoers_regeln(inhalt: str) -> list[str]:
    """Erlaubte Befehle aus dem sudoers-Text (Cmnd_Alias aufgelöst)."""
    text = inhalt.replace("\\\n", " ")
    aliase: dict[str, list[str]] = {}
    erlaubt: list[str] = []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith(("#", "Defaults")):
            continue
        if zeile.startswith("Cmnd_Alias"):
            name, _, befehle = zeile.removeprefix("Cmnd_Alias").partition("=")
            aliase[name.strip()] = [b.strip() for b in befehle.split(",")]
            continue
        benutzer, _, rest = zeile.partition(" ")
        assert benutzer == "missa", zeile
        assert "(root) NOPASSWD:" in rest, zeile
        for eintrag in rest.split("NOPASSWD:", 1)[1].split(","):
            eintrag = eintrag.strip()
            erlaubt += aliase.get(eintrag, [eintrag])
    return erlaubt


def wrapper_ausfuehren(wrapper: Path, fakes: Path, *argumente: str) -> tuple[int, list[str]]:
    protokoll_lesen(fakes)
    ergebnis = subprocess.run(
        ["bash", str(wrapper), *argumente], capture_output=True, text=True, check=False
    )
    return ergebnis.returncode, protokoll_lesen(fakes)


def test_sudoers_erlaubt_nur_missa_docker_und_missa_update(fakes):
    """sudo darf nur die zwei Programme – ohne Platzhalter, gültig für sudo und sudo-rs."""
    inhalt = erzeugen(fakes, "--nur-sudoers")
    assert sudoers_regeln(inhalt) == [
        "/usr/local/lib/missa/missa-docker",
        "/usr/local/lib/missa/missa-update",
    ]
    regeln = [z for z in inhalt.splitlines() if z and not z.startswith("#")]
    assert regeln == [
        "missa ALL=(root) NOPASSWD: /usr/local/lib/missa/missa-docker, "
        "/usr/local/lib/missa/missa-update"
    ]
    assert "*" not in "".join(regeln) and "ALL\n" not in inhalt and "NOPASSWD: ALL" not in inhalt


@pytest.mark.parametrize(
    ("argumente", "erwartet"),
    [
        (["ps"], "docker ps --all --no-trunc --format {{json .}}"),
        (["stats"], "docker stats --no-stream --no-trunc --format {{json .}}"),
        (["logs", "nextcloud", "50"], "docker logs --tail 50 --timestamps nextcloud"),
        (["logs", "nextcloud", "0050"], "docker logs --tail 50 --timestamps nextcloud"),
        (["restart", "nextcloud"], "docker restart nextcloud"),
    ],
)
def test_missa_docker_kann_nur_ps_stats_logs_restart(wrapper, fakes, argumente, erwartet):
    code, aufrufe = wrapper_ausfuehren(wrapper, fakes, *argumente)
    assert code == 0
    assert aufrufe == [erwartet]


@pytest.mark.parametrize(
    "argumente",
    [
        [],
        ["exec", "-it", "nextcloud", "sh"],
        ["run", "--rm", "-v", "/:/host", "alpine"],
        ["rm", "-f", "nextcloud"],
        ["cp", "nextcloud:/etc/shadow", "/tmp"],
        ["ps", "-a"],
        ["ps", "--format", "{{.Names}}"],
        ["stats", "nextcloud"],
        ["logs", "nextcloud"],
        ["logs", "x;id", "5"],
        ["logs", "--follow", "5"],
        ["logs", "nextcloud", "99999"],
        ["logs", "nextcloud", "5", "--details"],
        ["restart"],
        ["restart", "a", "b"],
        ["restart", "-t0"],
        ["restart", "../x"],
        ["--host", "tcp://evil", "ps"],
        ["system", "prune", "-af"],
    ],
)
def test_missa_docker_lehnt_alles_andere_ab(wrapper, fakes, argumente):
    code, aufrufe = wrapper_ausfuehren(wrapper, fakes, *argumente)
    assert code == 64
    assert aufrufe == []


def test_sudoers_ohne_docker_ohne_rechte(tmp_path):
    """Ohne Docker auf dem Host bekommt missa gar keine sudo-Rechte."""
    ohne_docker = tmp_path / "ohne-docker"
    ohne_docker.mkdir()
    for name in ("cat", "sed", "head"):
        (ohne_docker / name).symlink_to(shutil.which(name))
    ergebnis = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--nur-sudoers"],
        capture_output=True,
        text=True,
        env={"PATH": str(ohne_docker)},
        check=True,
    )
    assert sudoers_regeln(ergebnis.stdout) == []
    assert "NOPASSWD" not in ergebnis.stdout


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo nicht installiert")
def test_sudoers_ist_gueltig(fakes, tmp_path):
    datei = tmp_path / "missa.sudoers"
    datei.write_text(erzeugen(fakes, "--nur-sudoers"), encoding="utf-8")
    ergebnis = subprocess.run(
        ["visudo", "-cf", str(datei)], capture_output=True, text=True, check=False
    )
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr


# ----------------------------------------------------------------------------- Einrichten


def test_dry_run_zeigt_schritte(fakes):
    ausgabe = erzeugen(fakes, "--dry-run", "--schluessel", SCHLUESSEL)
    assert "würde ausführen: useradd --system" in ausgabe or "gibt es schon" in ausgabe
    assert "würde ausführen: usermod --append --groups systemd-journal missa" in ausgabe or (
        "systemd-journal fehlt" in ausgabe
    )
    assert "würde schreiben: /usr/local/lib/missa/missa-befehl (0755, root:root)" in ausgabe
    assert "authorized_keys (0600" in ausgabe
    assert "würde schreiben: /etc/sudoers.d/missa (0440, root:root)" in ausgabe


def test_ohne_gueltigen_schluessel_abbruch(fakes):
    umgebung = {**os.environ, "PATH": f"{fakes}:{os.environ['PATH']}"}
    ergebnis = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--schluessel", "kein schluessel"],
        capture_output=True,
        text=True,
        env=umgebung,
        check=False,
    )
    assert ergebnis.returncode == 1 and "öffentlicher SSH-Schlüssel" in ergebnis.stderr


def test_proxmox_host_nur_mit_ausdruecklichem_ja(fakes, tmp_path):
    pve = tmp_path / "pve"
    pve.mkdir()
    umgebung = {**os.environ, "PATH": f"{fakes}:{os.environ['PATH']}", "PVE_ORDNER": str(pve)}
    nein = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--schluessel", SCHLUESSEL],
        capture_output=True,
        text=True,
        env=umgebung,
        check=False,
    )
    assert nein.returncode == 1 and "Proxmox-Host" in nein.stderr
    ja = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--proxmox-ja", "--schluessel", SCHLUESSEL],
        capture_output=True,
        text=True,
        env=umgebung,
        check=False,
    )
    assert ja.returncode == 0, ja.stderr


def test_authorized_keys_bindet_an_missa_befehl():
    inhalt = SCRIPT.read_text(encoding="utf-8")
    assert 'restrict,command=\\"$BEFEHL_DATEI\\" $SCHLUESSEL' in inhalt


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck nicht installiert")
def test_erzeugte_programme_bestehen_shellcheck(befehl, wrapper, update):
    ergebnis = subprocess.run(
        ["shellcheck", "-s", "bash", str(befehl), str(wrapper), str(update)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ergebnis.returncode == 0, ergebnis.stdout


@pytest.mark.parametrize(
    ("argumente", "erwartet"),
    [
        (["update"], "apt-get update"),
        (
            ["upgrade"],
            "apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
            "upgrade",
        ),
        (["reboot"], "shutdown -r +1 Neustart durch Missa (nach Louis' Ja)"),
    ],
)
def test_missa_update_kann_nur_update_upgrade_reboot(update, fakes, argumente, erwartet):
    code, aufrufe = wrapper_ausfuehren(update, fakes, *argumente)
    assert code == 0
    assert aufrufe == [erwartet]


@pytest.mark.parametrize(
    "argumente",
    [
        [],
        ["install", "nmap"],
        ["remove", "openssh-server"],
        ["dist-upgrade"],
        ["full-upgrade"],
        ["upgrade", "-y"],
        ["update", "--allow-insecure-repositories"],
        ["reboot", "now"],
        ["poweroff"],
        ["-o", "APT::Update::Pre-Invoke::=/bin/sh", "update"],
    ],
)
def test_missa_update_lehnt_alles_andere_ab(update, fakes, argumente):
    code, aufrufe = wrapper_ausfuehren(update, fakes, *argumente)
    assert code == 64
    assert aufrufe == []


# ----------------------------------------------------------------------------- Caddy (rp01)


@pytest.fixture
def caddy_ordner(tmp_path: Path) -> Path:
    ordner = tmp_path / "caddy-missa"
    ordner.mkdir()
    return ordner


@pytest.fixture
def caddy_wrapper(tmp_path: Path, fakes: Path) -> Path:
    """Das erzeugte missa-caddy (ruft gefälschtes caddy und systemctl mit absolutem Pfad auf)."""
    datei = tmp_path / "missa-caddy"
    datei.write_text(
        erzeugen(fakes, "--nur-caddy", MISSA_CADDYFILE="/etc/caddy/Caddyfile"), encoding="utf-8"
    )
    datei.chmod(0o755)
    return datei


@pytest.fixture
def caddy_befehl(tmp_path: Path, fakes: Path, caddy_wrapper: Path, caddy_ordner: Path) -> Path:
    datei = tmp_path / "missa-befehl-caddy"
    datei.write_text(
        erzeugen(
            fakes,
            "--caddy",
            "--nur-befehl",
            MISSA_CADDY_DATEI=str(caddy_wrapper),
            MISSA_CADDY_ORDNER=str(caddy_ordner),
        ),
        encoding="utf-8",
    )
    datei.chmod(0o755)
    return datei


def ausfuehren_mit(
    befehl: Path, fakes: Path, ssh_befehl: str, eingabe: str = ""
) -> tuple[int, list[str], str, str]:
    protokoll_lesen(fakes)
    umgebung = {
        "SSH_ORIGINAL_COMMAND": ssh_befehl,
        "MISSA_TEST_PATH": f"{fakes}:/usr/bin:/bin",
        "PATH": "/usr/bin:/bin",
    }
    ergebnis = subprocess.run(
        ["bash", str(befehl)],
        capture_output=True,
        text=True,
        env=umgebung,
        input=eingabe,
        check=False,
    )
    return ergebnis.returncode, protokoll_lesen(fakes), ergebnis.stdout, ergebnis.stderr


EINTRAG = "spiel.louis-degiorgi.ch {\n\treverse_proxy 10.0.70.120:8080\n}"


def test_caddy_schreiben_liste_loeschen(caddy_befehl, fakes, caddy_ordner):
    code, aufrufe, aus, fehler = ausfuehren_mit(
        caddy_befehl, fakes, "caddy-schreiben missa-spiel.caddy", EINTRAG
    )
    assert code == 0, fehler
    assert aus.strip() == "geschrieben: missa-spiel.caddy" and aufrufe == []
    datei = caddy_ordner / "missa-spiel.caddy"
    assert datei.read_text(encoding="utf-8") == EINTRAG + "\n"
    assert datei.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in caddy_ordner.iterdir()] == ["missa-spiel.caddy"]

    code, _, aus, _ = ausfuehren_mit(caddy_befehl, fakes, "caddy-liste")
    assert code == 0 and aus == f"### missa-spiel.caddy\n{EINTRAG}\n"

    code, _, aus, _ = ausfuehren_mit(caddy_befehl, fakes, "caddy-loeschen missa-spiel.caddy")
    assert code == 0 and aus.strip() == "entfernt: missa-spiel.caddy"
    assert list(caddy_ordner.iterdir()) == []


def test_caddy_pruefen_und_neu_laden_nur_ueber_missa_caddy(caddy_befehl, fakes, caddy_wrapper):
    assert ausfuehren_mit(caddy_befehl, fakes, "caddy-pruefen")[:2] == (
        0,
        [f"sudo -n {caddy_wrapper} validate"],
    )
    assert ausfuehren_mit(caddy_befehl, fakes, "caddy-neu-laden")[:2] == (
        0,
        [f"sudo -n {caddy_wrapper} reload"],
    )


@pytest.mark.parametrize(
    ("ssh_befehl", "eingabe"),
    [
        ("caddy-schreiben ../Caddyfile", EINTRAG),
        ("caddy-schreiben Caddyfile", EINTRAG),
        ("caddy-schreiben missa-spiel.conf", EINTRAG),
        ("caddy-schreiben missa-Spiel.caddy", EINTRAG),
        ("caddy-schreiben missa-a.caddy missa-b.caddy", EINTRAG),
        ("caddy-schreiben", EINTRAG),
        ("caddy-loeschen ../../etc/passwd", ""),
        ("caddy-loeschen missa-*.caddy", ""),
        ("caddy-schreiben missa-spiel.caddy", "import /etc/caddy/Caddyfile"),
        ("caddy-schreiben missa-spiel.caddy", "{\n\tadmin off\n}"),
        ("caddy-schreiben missa-spiel.caddy", "x" * 5000),
        ("caddy-liste missa-spiel.caddy", ""),
        ("caddy-pruefen --config /tmp/boese", ""),
        ("caddy-neu-laden nginx", ""),
    ],
)
def test_caddy_befehle_lehnen_alles_andere_ab(
    caddy_befehl, fakes, caddy_ordner, ssh_befehl, eingabe
):
    code, aufrufe, _, _ = ausfuehren_mit(caddy_befehl, fakes, ssh_befehl, eingabe)
    assert code != 0
    assert aufrufe == []
    assert list(caddy_ordner.iterdir()) == []


def test_ohne_caddy_option_keine_caddy_befehle(befehl, fakes):
    code, aufrufe, _, fehler = ausfuehren_mit(befehl, fakes, "caddy-liste")
    assert code != 0 and "nicht eingerichtet" in fehler and aufrufe == []


@pytest.mark.parametrize(
    ("argumente", "erwartet"),
    [
        (["validate"], "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"),
        (["reload"], "systemctl reload caddy"),
    ],
)
def test_missa_caddy_kann_nur_validate_und_reload(caddy_wrapper, fakes, argumente, erwartet):
    code, aufrufe = wrapper_ausfuehren(caddy_wrapper, fakes, *argumente)
    assert code == 0
    assert aufrufe == [erwartet]


@pytest.mark.parametrize(
    "argumente",
    [
        [],
        ["restart"],
        ["stop"],
        ["validate", "--config", "/tmp/boese"],
        ["reload", "nginx"],
        ["adapt"],
        ["run"],
    ],
)
def test_missa_caddy_lehnt_alles_andere_ab(caddy_wrapper, fakes, argumente):
    code, aufrufe = wrapper_ausfuehren(caddy_wrapper, fakes, *argumente)
    assert code != 0
    assert aufrufe == []


def test_sudoers_mit_caddy_erlaubt_genau_drei_programme(fakes, tmp_path):
    inhalt = erzeugen(fakes, "--caddy", "--nur-sudoers")
    assert sudoers_regeln(inhalt) == [
        "/usr/local/lib/missa/missa-docker",
        "/usr/local/lib/missa/missa-update",
        "/usr/local/lib/missa/missa-caddy",
    ]
    assert "*" not in "".join(z for z in inhalt.splitlines() if not z.startswith("#"))
    if shutil.which("visudo") is not None:
        datei = tmp_path / "missa-caddy.sudoers"
        datei.write_text(inhalt, encoding="utf-8")
        ergebnis = subprocess.run(
            ["visudo", "-cf", str(datei)], capture_output=True, text=True, check=False
        )
        assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr


def test_caddy_einrichten_aendert_caddyfile_nicht(fakes, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("example.ch {\n\trespond ok\n}\n", encoding="utf-8")
    vorher = caddyfile.read_bytes()
    ausgabe = erzeugen(
        fakes,
        "--dry-run",
        "--caddy",
        "--schluessel",
        SCHLUESSEL,
        MISSA_CADDYFILE=str(caddyfile),
        MISSA_CADDY_ORDNER=str(tmp_path / "missa"),
    )
    assert "würde schreiben: /usr/local/lib/missa/missa-caddy (0755, root:root)" in ausgabe
    assert f"import {tmp_path / 'missa'}/missa-*.caddy" in ausgabe
    assert caddyfile.read_bytes() == vorher
    assert not (tmp_path / "missa").exists()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck nicht installiert")
def test_caddy_programme_bestehen_shellcheck(caddy_befehl, caddy_wrapper):
    ergebnis = subprocess.run(
        ["shellcheck", "-s", "bash", str(caddy_befehl), str(caddy_wrapper)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ergebnis.returncode == 0, ergebnis.stdout
