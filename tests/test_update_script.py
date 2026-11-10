"""Testlauf von scripts/update.sh in einer Ersatzwelt.

Ein lokales Git-Repo spielt GitHub (Tags v0.1.0 und v0.2.0, dazu ein neuerer
Stand auf main). ``systemctl``, ``pip`` und ``curl`` sind kleine Ersatz-Scripts,
die sich merken, welche Version installiert ist und welche läuft. So lässt sich
prüfen: Update, „schon aktuell“, Probelauf, Ergänzen der Einstellungen und der
automatische Rücksprung bei Fehlern.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJEKT = Path(__file__).resolve().parents[1]
UPDATE_SH = PROJEKT / "scripts" / "update.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="braucht git und bash"
)

CHANGELOG_010 = """\
# Changelog

## Unveröffentlicht

## 0.1.0 – 2026-11-09

### Hinzugefügt

- Erste Version
"""

CHANGELOG_020 = """\
# Changelog

## Unveröffentlicht

## 0.2.0 – 2026-11-10

### Hinzugefügt

- Missa ist per Nextcloud Talk erreichbar

## 0.1.0 – 2026-11-09

### Hinzugefügt

- Erste Version
"""

CHANGELOG_MAIN = CHANGELOG_020.replace(
    "## Unveröffentlicht\n", "## Unveröffentlicht\n\n### Behoben\n\n- Noch nicht veröffentlicht\n"
)

VORLAGE_010 = """\
# Pflicht
MISSA_API_KEY=bitte-aendern
MISSA_PORT=8000
NEXTCLOUD_URL=https://cloud.example.ch
"""

VORLAGE_020 = (
    VORLAGE_010
    + """
# --- Nextcloud Talk ---
# Talk ein- oder ausschalten.
NC_TALK_ENABLED=false
# Gemeinsames Geheimnis des Bots.
NC_TALK_BOT_SECRET=
# Nur diese Accounts dürfen mit Missa sprechen.
NC_TALK_ALLOWED_USERS=
# Optional: andere Adresse für Antworten.
NC_TALK_API_URL=
NC_TALK_TIMEOUT=300
"""
)

FAKE_SYSTEMCTL = """\
#!/usr/bin/env bash
echo "systemctl $*" >>"$FAKE_STATE/aufrufe"
case "$1" in
    cat)
        [[ "$2" == "missa-core" ]] || exit 1
        printf '[Service]\\nEnvironmentFile=%s\\n' "$FAKE_ENV"
        printf 'ExecStart=%s/bin/missa-core\\n' "$FAKE_VENV"
        ;;
    restart) cp "$FAKE_STATE/installiert" "$FAKE_STATE/laeuft" ;;
    is-failed)
        v="$(cat "$FAKE_STATE/laeuft" 2>/dev/null || true)"
        [[ -n "${FAKE_ABSTURZ_VERSION:-}" && "$v" == "$FAKE_ABSTURZ_VERSION" ]] && exit 0
        exit 1
        ;;
esac
exit 0
"""

FAKE_PIP = """\
#!/usr/bin/env bash
if [[ "$1" == "show" ]]; then
    printf 'Name: missa\\nVersion: %s\\n' "$(cat "$FAKE_STATE/installiert")"
    exit 0
fi
echo "pip $*" >>"$FAKE_STATE/aufrufe"
repo="${*: -1}"
v="$(sed -n 's/^version = "\\(.*\\)"/\\1/p' "$repo/pyproject.toml")"
if [[ -n "${FAKE_PIP_FEHLER_VERSION:-}" && "$v" == "$FAKE_PIP_FEHLER_VERSION" ]]; then
    echo "ERROR: Abhängigkeit nicht gefunden" >&2
    exit 1
fi
echo "$v" >"$FAKE_STATE/installiert"
"""

FAKE_CURL = """\
#!/usr/bin/env bash
ziel=""
while [[ $# -gt 0 ]]; do
    [[ "$1" == "-o" ]] && { ziel="$2"; shift; }
    shift
done
v="$(cat "$FAKE_STATE/laeuft" 2>/dev/null || true)"
if [[ -z "$v" || "$v" == "${FAKE_HEALTH_FEHLER_VERSION:-}" ]]; then
    printf '000'
    exit 7
fi
printf '{"status":"ok","version":"%s","llama":"ok"}' "$v" >"$ziel"
printf '200'
"""


def git(ordner: Path, *argumente: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *argumente],
        cwd=ordner,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def ausfuehrbar(pfad: Path, inhalt: str) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(inhalt, encoding="utf-8")
    pfad.chmod(0o755)


def stand(ordner: Path, version: str, changelog: str, vorlage: str) -> None:
    (ordner / "pyproject.toml").write_text(
        f'[project]\nname = "missa"\nversion = "{version}"\n', encoding="utf-8"
    )
    (ordner / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (ordner / "config").mkdir(exist_ok=True)
    (ordner / "config" / "missa.env.example").write_text(vorlage, encoding="utf-8")
    (ordner / "scripts").mkdir(exist_ok=True)
    shutil.copy(UPDATE_SH, ordner / "scripts" / "update.sh")
    (ordner / ".gitignore").write_text("logs/\n", encoding="utf-8")


@dataclass
class Welt:
    app: Path
    zustand: Path
    env_datei: Path
    umgebung: dict[str, str]
    commits: dict[str, str]

    def update(self, *argumente: str, **zusatz: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.app / "scripts" / "update.sh"), *argumente],
            cwd=self.app,
            env={**self.umgebung, **zusatz},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def head(self) -> str:
        return git(self.app, "rev-parse", "HEAD")

    def laeuft(self) -> str:
        return (self.zustand / "laeuft").read_text().strip()

    def aufrufe(self) -> str:
        datei = self.zustand / "aufrufe"
        return datei.read_text() if datei.exists() else ""


@pytest.fixture
def welt(tmp_path: Path) -> Welt:
    quelle = tmp_path / "quelle"
    quelle.mkdir()
    git(quelle, "init", "-q", "-b", "main")
    commits = {}
    for version, changelog, vorlage, tag in [
        ("0.1.0", CHANGELOG_010, VORLAGE_010, "v0.1.0"),
        ("0.2.0", CHANGELOG_020, VORLAGE_020, "v0.2.0"),
        ("0.2.0", CHANGELOG_MAIN, VORLAGE_020, None),
    ]:
        stand(quelle, version, changelog, vorlage)
        git(quelle, "add", "-A")
        git(quelle, "commit", "-q", "-m", f"Stand {version}")
        commits[tag or "main"] = git(quelle, "rev-parse", "HEAD")
        if tag:
            git(quelle, "tag", "-a", tag, "-m", version)
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(quelle), str(origin))

    app = tmp_path / "app"
    git(tmp_path, "clone", "-q", str(origin), str(app))
    git(app, "checkout", "-q", "v0.1.0")

    zustand = tmp_path / "zustand"
    zustand.mkdir()
    (zustand / "installiert").write_text("0.1.0\n")
    (zustand / "laeuft").write_text("0.1.0\n")

    venv = tmp_path / "venv"
    ausfuehrbar(venv / "bin" / "pip", FAKE_PIP)
    ausfuehrbar(venv / "bin" / "missa-core", "#!/bin/sh\n")
    werkzeuge = tmp_path / "werkzeuge"
    ausfuehrbar(werkzeuge / "systemctl", FAKE_SYSTEMCTL)
    ausfuehrbar(werkzeuge / "curl", FAKE_CURL)

    env_datei = tmp_path / "missa.env"
    env_datei.write_text(
        textwrap.dedent(
            """\
            MISSA_API_KEY=mein-echter-schluessel
            MISSA_PORT=8000
            NEXTCLOUD_URL=https://cloud.louis.example
            """
        ),
        encoding="utf-8",
    )

    umgebung = {
        "PATH": f"{werkzeuge}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_STATE": str(zustand),
        "FAKE_ENV": str(env_datei),
        "FAKE_VENV": str(venv),
        "MISSA_UPDATE_OHNE_ROOT": "1",
        "MISSA_HEALTH_WARTEZEIT": "4",
        "MISSA_HEALTH_WARTEZEIT_RUECKSPRUNG": "4",
        "NO_COLOR": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return Welt(app, zustand, env_datei, umgebung, commits)


# ---------------------------------------------------------------------- Erfolg


def test_update_auf_neuesten_release(welt: Welt):
    ergebnis = welt.update()
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert welt.head() == welt.commits["v0.2.0"]
    assert welt.laeuft() == "0.2.0"
    ausgabe = ergebnis.stdout
    assert "Neu in v0.2.0 (seit v0.1.0)" in ausgabe
    assert "Missa ist per Nextcloud Talk erreichbar" in ausgabe
    assert "Erste Version" not in ausgabe
    assert "Noch nicht veröffentlicht" not in ausgabe
    assert "Missa-Core läuft jetzt mit v0.2.0" in ausgabe
    assert "systemctl restart missa-core" in welt.aufrufe()
    assert (welt.app / "logs" / "update.log").read_text().count("Update erfolgreich") == 1


def test_neue_einstellungen_werden_ergaenzt_ohne_bestehende_zu_aendern(welt: Welt):
    vorher = welt.env_datei.read_text()
    ergebnis = welt.update()
    assert ergebnis.returncode == 0, ergebnis.stderr
    nachher = welt.env_datei.read_text()
    assert nachher.startswith(vorher)  # nur angehängt, nichts verändert
    neu = nachher[len(vorher) :]
    assert "# --- neu in v0.2.0" in neu
    assert "# Gemeinsames Geheimnis des Bots.\n# neu in v0.2.0\nNC_TALK_BOT_SECRET=\n" in neu
    assert "NC_TALK_ENABLED=false" in neu
    for schon_da in ("MISSA_API_KEY", "MISSA_PORT", "NEXTCLOUD_URL"):
        assert f"{schon_da}=" not in neu
    # Liste am Ende: was noch auszufüllen ist
    pflicht, optional = ergebnis.stdout.split("Bitte noch ausfüllen")[1].split("Optional (darf")
    assert "NC_TALK_BOT_SECRET" in pflicht and "NC_TALK_ALLOWED_USERS" in pflicht
    assert "NC_TALK_API_URL" not in pflicht
    assert "NC_TALK_API_URL" in optional
    assert "NC_TALK_ENABLED" not in pflicht + optional  # hat einen Wert (false)
    # Sicherungskopie der alten Datei
    assert list(welt.env_datei.parent.glob("missa.env.vor-update-*"))


def test_auskommentierte_einstellung_wird_nicht_wieder_ergaenzt(welt: Welt):
    welt.env_datei.write_text(welt.env_datei.read_text() + "#NC_TALK_TIMEOUT=600\n")
    welt.update()
    assert welt.env_datei.read_text().count("NC_TALK_TIMEOUT") == 1


def test_schon_aktuell_ohne_neustart(welt: Welt):
    assert welt.update().returncode == 0
    neustarts = welt.aufrufe().count("restart")
    ergebnis = welt.update()
    assert ergebnis.returncode == 0
    assert "schon aktuell" in ergebnis.stdout
    assert welt.aufrufe().count("restart") == neustarts


def test_main_und_bestimmte_version(welt: Welt):
    ergebnis = welt.update("--main")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert welt.head() == welt.commits["main"]
    assert "Noch nicht veröffentlicht" in ergebnis.stdout

    ergebnis = welt.update("--version", "0.1.0")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert welt.head() == welt.commits["v0.1.0"]
    assert welt.laeuft() == "0.1.0"
    assert "nicht neuer" in ergebnis.stdout


def test_unbekannte_version(welt: Welt):
    ergebnis = welt.update("--version", "v9.9.9")
    assert ergebnis.returncode == 1
    assert "gibt es nicht" in ergebnis.stderr
    assert welt.head() == welt.commits["v0.1.0"]


# ---------------------------------------------------------------------- Probelauf


def test_dry_run_aendert_nichts(welt: Welt):
    env_vorher = welt.env_datei.read_text()
    ergebnis = welt.update("--dry-run")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "Probelauf" in ergebnis.stdout
    assert "Missa ist per Nextcloud Talk erreichbar" in ergebnis.stdout
    assert "NC_TALK_BOT_SECRET" in ergebnis.stdout  # würde ergänzt
    assert welt.head() == welt.commits["v0.1.0"]
    assert welt.env_datei.read_text() == env_vorher
    assert "pip" not in welt.aufrufe()
    assert "restart" not in welt.aufrufe()
    assert not (welt.app / "logs").exists()
    assert not list(welt.env_datei.parent.glob("missa.env.vor-update-*"))


def test_hilfe(welt: Welt):
    ergebnis = welt.update("--help")
    assert ergebnis.returncode == 0
    assert "--dry-run" in ergebnis.stdout and "--main" in ergebnis.stdout


# ---------------------------------------------------------------------- Vorabprüfungen


def test_lokale_aenderungen_brechen_ab(welt: Welt):
    (welt.app / "CHANGELOG.md").write_text("von Hand geändert\n")
    ergebnis = welt.update()
    assert ergebnis.returncode == 1
    assert "lokale Änderungen" in ergebnis.stderr
    assert "CHANGELOG.md" in ergebnis.stderr
    assert "pip" not in welt.aufrufe()


def test_github_nicht_erreichbar(welt: Welt):
    git(welt.app, "remote", "set-url", "origin", str(welt.app.parent / "gibtsnicht.git"))
    ergebnis = welt.update()
    assert ergebnis.returncode == 1
    assert "nicht erreichbar" in ergebnis.stderr


def test_ohne_root_nur_probelauf(welt: Welt):
    umgebung = {k: v for k, v in welt.umgebung.items() if k != "MISSA_UPDATE_OHNE_ROOT"}
    if os.geteuid() == 0:
        pytest.skip("läuft als root")
    ergebnis = subprocess.run(
        ["bash", str(welt.app / "scripts" / "update.sh")],
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ergebnis.returncode == 1
    assert "sudo" in ergebnis.stderr


# ---------------------------------------------------------------------- Rücksprung


@pytest.mark.parametrize(
    ("fehler", "meldung"),
    [
        ({"FAKE_PIP_FEHLER_VERSION": "0.2.0"}, "Installation der Abhängigkeiten fehlgeschlagen"),
        (
            {"FAKE_HEALTH_FEHLER_VERSION": "0.2.0"},
            "Gesundheitsprüfung nach dem Update fehlgeschlagen",
        ),
        ({"FAKE_ABSTURZ_VERSION": "0.2.0"}, "abgestürzt"),
    ],
)
def test_bei_fehler_laeuft_wieder_die_alte_version(welt: Welt, fehler, meldung):
    ergebnis = welt.update(**fehler)
    assert ergebnis.returncode == 1
    assert meldung in ergebnis.stderr
    assert "Die alte Version v0.1.0 läuft wieder" in ergebnis.stdout
    assert welt.head() == welt.commits["v0.1.0"]
    assert (welt.zustand / "installiert").read_text().strip() == "0.1.0"
    assert welt.laeuft() == "0.1.0"
    assert "fehlgeschlagen" in (welt.app / "logs" / "update.log").read_text()


def test_ruecksprung_auf_branch(welt: Welt):
    git(welt.app, "checkout", "-q", "-B", "main", "v0.1.0")
    ergebnis = welt.update(FAKE_PIP_FEHLER_VERSION="0.2.0")
    assert ergebnis.returncode == 1
    assert git(welt.app, "symbolic-ref", "--short", "HEAD") == "main"
    assert welt.head() == welt.commits["v0.1.0"]


# ---------------------------------------------------------------------- Ersteinrichtung


def test_ersteinrichtung_nach_checkout_von_hand(welt: Welt):
    """Louis holt das Script einmal von Hand (git checkout v0.2.0); installiert ist noch 0.1.0."""
    git(welt.app, "checkout", "-q", "v0.2.0")
    ergebnis = welt.update()
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert "noch nicht installiert" in ergebnis.stdout
    assert "Neu in v0.2.0 (seit v0.1.0)" in ergebnis.stdout
    assert welt.laeuft() == "0.2.0"
    assert "NC_TALK_BOT_SECRET" in welt.env_datei.read_text()
    # Danach ist alles aktuell.
    assert "schon aktuell" in welt.update().stdout


def test_ersteinrichtung_ruecksprung_auf_installierte_version(welt: Welt):
    git(welt.app, "checkout", "-q", "v0.2.0")
    ergebnis = welt.update(FAKE_PIP_FEHLER_VERSION="0.2.0")
    assert ergebnis.returncode == 1
    assert welt.head() == welt.commits["v0.1.0"]
    assert welt.laeuft() == "0.1.0"


def test_erzwingen_installiert_neu_auch_wenn_aktuell(welt: Welt):
    welt.update()
    neustarts = welt.aufrufe().count("restart")
    ergebnis = welt.update("--erzwingen")
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "wegen --erzwingen" in ergebnis.stdout
    assert welt.aufrufe().count("restart") == neustarts + 1


def test_ruecksprung_probelauf_mit_testschalter(welt: Welt):
    """So probt Louis den Rücksprung auf ai01: --erzwingen + MISSA_UPDATE_TEST_FEHLER=1."""
    welt.update()
    ergebnis = welt.update("--erzwingen", MISSA_UPDATE_TEST_FEHLER="1")
    assert ergebnis.returncode == 1
    assert "Testmodus" in ergebnis.stderr
    assert "Die alte Version v0.2.0 läuft wieder" in ergebnis.stdout
    assert welt.head() == welt.commits["v0.2.0"]
    assert welt.laeuft() == "0.2.0"


def test_ersteinrichtung_ohne_tag_der_alten_version(welt: Welt):
    """Fehler auf ai01: v0.1.0 gab es nicht als Tag – trotzdem darf es nicht „aktuell“ heißen."""
    git(welt.app, "tag", "-d", "v0.1.0")
    git(welt.app, "push", "-q", "origin", ":refs/tags/v0.1.0")
    git(welt.app, "checkout", "-q", "v0.2.0")
    ergebnis = welt.update()
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert "schon aktuell" not in ergebnis.stdout
    assert "noch nicht installiert" in ergebnis.stdout
    assert welt.laeuft() == "0.2.0"
    assert "NC_TALK_BOT_SECRET" in welt.env_datei.read_text()
    assert "schon aktuell" in welt.update().stdout
