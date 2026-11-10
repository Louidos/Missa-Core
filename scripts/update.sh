#!/usr/bin/env bash
# Missa-Core auf ai01 aktualisieren – ein Befehl, mit automatischem Rücksprung.
#
#   sudo ./scripts/update.sh               neuester Release-Tag (Standard)
#   sudo ./scripts/update.sh --main        neuester Stand von main
#   sudo ./scripts/update.sh --version v0.2.0
#   ./scripts/update.sh --dry-run          nur anzeigen, nichts ändern
#
# Ausführliche Beschreibung: docs/update.md
#
# Alles steckt in Funktionen; ausgeführt wird erst ganz unten mit `main "$@"; exit`.
# So liest Bash das ganze Script, bevor `git checkout` die Datei austauscht.

set -Eeuo pipefail

# ----------------------------------------------------------------------------- Einstellungen
# Für Sonderfälle und Tests per Umgebungsvariable änderbar.
DIENST="${MISSA_DIENST:-missa-core}"
HEALTH_WARTEZEIT="${MISSA_HEALTH_WARTEZEIT:-60}"
RUECKSPRUNG_WARTEZEIT="${MISSA_HEALTH_WARTEZEIT_RUECKSPRUNG:-60}"
# Nur zum Ausprobieren: lässt die Prüfung nach dem Update absichtlich scheitern.
TEST_FEHLER="${MISSA_UPDATE_TEST_FEHLER:-}"
STANDARD_VENV="/opt/missa/venv"
STANDARD_ENV_DATEI="/etc/missa/missa.env"

REPO=""
LOG=""
ART=""            # systemd oder docker
VENV=""
ENV_DATEI=""
VORLAGE=""
COMPOSE_DATEI=""
DRY_RUN=0
ERZWINGEN=0
ZIEL_ART="release"  # release, main oder version
ZIEL_ANGABE=""
KOPF_COMMIT=""    # was gerade ausgecheckt ist
ALT_COMMIT=""     # was installiert ist und läuft (Ziel eines Rücksprungs)
ALT_BRANCH=""
ALT_NAME=""
ALT_VERSION=""
ZIEL_COMMIT=""
ZIEL_NAME=""
ZIEL_VERSION=""
IN_ARBEIT=0       # 1, sobald etwas verändert wurde (dann ist ein Rücksprung möglich)
NICHT_INSTALLIERT=0  # 1, wenn der ausgecheckte Code nachweislich noch nicht installiert ist
NEUE_SCHLUESSEL=()
LEERE_SCHLUESSEL=()
OPTIONALE_SCHLUESSEL=()

# ----------------------------------------------------------------------------- Ausgabe
farben_setzen() {
    if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
        ROT=$'\e[31m' GRUEN=$'\e[32m' GELB=$'\e[33m' BLAU=$'\e[34m' FETT=$'\e[1m' NORMAL=$'\e[0m'
    else
        ROT="" GRUEN="" GELB="" BLAU="" FETT="" NORMAL=""
    fi
}

ins_log() {
    if [[ -n "$LOG" && "$DRY_RUN" -eq 0 ]]; then
        printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"
    fi
}

info() { printf '%s\n' "${BLAU}•${NORMAL} $*"; ins_log "INFO  $*"; }
ok() { printf '%s\n' "${GRUEN}✔${NORMAL} $*"; ins_log "OK    $*"; }
warnung() { printf '%s\n' "${GELB}!${NORMAL} $*"; ins_log "WARN  $*"; }
fehler() { printf '%s\n' "${ROT}✘ $*${NORMAL}" >&2; ins_log "FEHLER $*"; }
titel() { printf '\n%s\n' "${FETT}$*${NORMAL}"; ins_log "----- $*"; }
abbruch() {
    fehler "$*"
    exit 1
}

# Führt einen Befehl aus; Ausgabe nur ins Log (bei Fehler die letzten Zeilen zeigen).
leise() {
    local ausgabe status=0
    ausgabe="$("$@" 2>&1)" || status=$?
    if [[ -n "$LOG" && "$DRY_RUN" -eq 0 ]]; then
        printf '%s\n' "$ausgabe" >>"$LOG"
    fi
    if [[ $status -ne 0 ]]; then
        printf '%s\n' "$ausgabe" | tail -n 15 >&2
    fi
    return "$status"
}

hilfe() {
    cat <<'EOF'
Missa-Core aktualisieren

Aufruf:
  sudo ./scripts/update.sh [Optionen]

Optionen:
  (ohne)              auf den neuesten Release-Tag wechseln (empfohlen)
  --main              auf den neuesten Stand von main wechseln
  --version vX.Y.Z    auf eine bestimmte Version wechseln (auch zurück)
  --dry-run           nur anzeigen, was passieren würde – nichts wird geändert
  --erzwingen         neu installieren und neu starten, auch wenn alles aktuell ist
  -h, --help          diese Hilfe

Ablauf:
  1. Prüfen: Repo-Ordner, lokale Änderungen, GitHub erreichbar
  2. Neuerungen aus dem CHANGELOG zeigen
  3. Code wechseln, Abhängigkeiten installieren
  4. Neue Einstellungen aus der Vorlage in der Env-Datei ergänzen (nie überschreiben)
  5. Dienst neu starten, /health bis zu 60 Sekunden prüfen
  Geht etwas schief, wird automatisch auf die vorherige Version zurückgewechselt.

Log: logs/update.log im Repo-Ordner. Mehr dazu: docs/update.md
EOF
}

# ----------------------------------------------------------------------------- Hilfen
g() { git -c safe.directory="$REPO" -C "$REPO" "$@"; }

version_aus_commit() {
    g show "$1:pyproject.toml" 2>/dev/null | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -n 1
}

# Liest einen Wert aus einer Env-Datei, ohne sie auszuführen.
env_wert() {
    local datei="$1" name="$2" zeile
    [[ -r "$datei" ]] || return 0
    zeile="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$datei" | tail -n 1 || true)"
    zeile="${zeile#*=}"
    zeile="${zeile%\"}"
    zeile="${zeile#\"}"
    zeile="${zeile%\'}"
    zeile="${zeile#\'}"
    printf '%s' "$zeile"
}

argumente_lesen() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --main) ZIEL_ART="main" ;;
            --version)
                [[ $# -ge 2 ]] || abbruch "--version braucht eine Versionsnummer, z. B. --version v0.2.0"
                ZIEL_ART="version"
                ZIEL_ANGABE="$2"
                shift
                ;;
            --version=*)
                ZIEL_ART="version"
                ZIEL_ANGABE="${1#*=}"
                ;;
            --dry-run) DRY_RUN=1 ;;
            --erzwingen) ERZWINGEN=1 ;;
            -h | --help)
                hilfe
                exit 0
                ;;
            *) abbruch "Unbekannte Option: $1 (Hilfe: --help)" ;;
        esac
        shift
    done
    if [[ "$ZIEL_ART" == "version" && "$ZIEL_ANGABE" != v* ]]; then
        ZIEL_ANGABE="v$ZIEL_ANGABE"
    fi
}

# ----------------------------------------------------------------------------- Vorabprüfungen
repo_pruefen() {
    local skriptordner oberste
    skriptordner="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO="$(cd "$skriptordner/.." && pwd)"
    oberste="$(g rev-parse --show-toplevel 2>/dev/null || true)"
    if [[ "$oberste" != "$REPO" || ! -f "$REPO/pyproject.toml" ]]; then
        abbruch "Das Script liegt nicht im missa-core-Repo (erwartet: $REPO). Bitte aus dem geklonten Repo starten."
    fi
}

log_oeffnen() {
    if [[ "$DRY_RUN" -eq 0 ]]; then
        mkdir -p "$REPO/logs" || abbruch "Kann $REPO/logs nicht anlegen."
        LOG="$REPO/logs/update.log"
        ins_log "===== Update gestartet: $0 $*"
    fi
}

root_pruefen() {
    if [[ "$DRY_RUN" -eq 0 && "${EUID:-$(id -u)}" -ne 0 && -z "${MISSA_UPDATE_OHNE_ROOT:-}" ]]; then
        abbruch "Bitte mit sudo starten: sudo ./scripts/update.sh (oder --dry-run zum Anschauen)"
    fi
}

sperre_holen() {
    [[ "$DRY_RUN" -eq 0 ]] || return 0
    command -v flock >/dev/null 2>&1 || return 0
    exec 9>"$REPO/logs/.update.lock"
    flock -n 9 || abbruch "Es läuft schon ein Update."
}

aenderungen_pruefen() {
    local geaendert
    geaendert="$(g status --porcelain --untracked-files=no)"
    if [[ -n "$geaendert" ]]; then
        fehler "Es gibt lokale Änderungen an versionierten Dateien – Update abgebrochen:"
        printf '%s\n' "$geaendert" | sed 's/^/    /' >&2
        fehler "Bitte sichern oder verwerfen (z. B. git -C $REPO stash), dann erneut starten."
        exit 1
    fi
}

github_pruefen() {
    if ! timeout 30 git -c safe.directory="$REPO" -C "$REPO" ls-remote --exit-code origin HEAD >/dev/null 2>&1; then
        abbruch "GitHub ($(g remote get-url origin 2>/dev/null || echo origin)) ist nicht erreichbar. Netzwerk prüfen und erneut versuchen."
    fi
}

art_erkennen() {
    local datei unit exe
    for datei in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
        if [[ -f "$REPO/$datei" ]]; then
            COMPOSE_DATEI="$REPO/$datei"
            break
        fi
    done

    if systemctl cat "$DIENST" >/dev/null 2>&1; then
        ART="systemd"
        unit="$(systemctl cat "$DIENST" 2>/dev/null)"
        exe="$(printf '%s\n' "$unit" | sed -n 's/^ExecStart=[-@+!:]*//p' | head -n 1 | awk '{print $1}')"
        VENV="${MISSA_VENV:-}"
        if [[ -z "$VENV" && "$exe" == */bin/* ]]; then
            VENV="${exe%/bin/*}"
        fi
        VENV="${VENV:-$STANDARD_VENV}"
        ENV_DATEI="${MISSA_ENV_DATEI:-$(printf '%s\n' "$unit" | sed -n 's/^EnvironmentFile=-\{0,1\}//p' | head -n 1)}"
        ENV_DATEI="${ENV_DATEI:-$STANDARD_ENV_DATEI}"
        VORLAGE="$REPO/config/missa.env.example"
        [[ -x "$VENV/bin/pip" ]] || abbruch "Python-Umgebung $VENV nicht gefunden (pip fehlt). Siehe docs/installation.md."
    elif [[ -n "$COMPOSE_DATEI" ]] && command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ART="docker"
        ENV_DATEI="${MISSA_ENV_DATEI:-$REPO/.env}"
        VORLAGE="$REPO/.env.example"
    else
        abbruch "Weder der systemd-Dienst $DIENST noch eine Docker-Compose-Datei gefunden. Siehe docs/installation.md."
    fi
}

# ----------------------------------------------------------------------------- Versionen
marke_datei() { printf '%s' "$REPO/logs/.installiert"; }

# Version des installierten Pakets (nur bei systemd/venv bekannt).
installierte_version() {
    [[ "$ART" == "systemd" ]] || return 0
    "$VENV/bin/pip" show missa 2>/dev/null | sed -n 's/^Version: *//p' | head -n 1 || true
}

# Ermittelt, welcher Stand wirklich installiert ist. Normalerweise ist das der
# ausgecheckte Stand. Nach einem Checkout von Hand (Ersteinrichtung) kann der
# Code aber neuer sein als das, was läuft – dann gilt die installierte Version.
alt_merken() {
    local marke="" installiert kopf_version tag_commit
    KOPF_COMMIT="$(g rev-parse HEAD)"
    ALT_BRANCH="$(g symbolic-ref -q --short HEAD || true)"
    kopf_version="$(version_aus_commit "$KOPF_COMMIT")"
    installiert="$(installierte_version)"

    [[ -r "$(marke_datei)" ]] && marke="$(head -n 1 "$(marke_datei)")"
    if [[ -n "$marke" ]] && g cat-file -e "$marke^{commit}" 2>/dev/null &&
        { [[ -z "$installiert" ]] || [[ "$(version_aus_commit "$marke")" == "$installiert" ]]; }; then
        ALT_COMMIT="$marke"
    elif [[ -z "$installiert" || "$installiert" == "$kopf_version" ]]; then
        ALT_COMMIT="$KOPF_COMMIT"
    else
        tag_commit="$(g rev-parse -q --verify "refs/tags/v$installiert^{commit}" 2>/dev/null || true)"
        if [[ -n "$tag_commit" ]]; then
            ALT_COMMIT="$tag_commit"
            info "Der ausgecheckte Code ($kopf_version) ist noch nicht installiert; installiert ist $installiert."
        else
            ALT_COMMIT="$KOPF_COMMIT"
            NICHT_INSTALLIERT=1
            warnung "Installiert ist $installiert, dazu gibt es aber keinen Tag v$installiert – ein Rücksprung ginge nur auf den ausgecheckten Stand."
        fi
    fi
    [[ "$ALT_COMMIT" == "$KOPF_COMMIT" ]] || ALT_BRANCH=""

    ALT_NAME="$(g describe --tags --exact-match "$ALT_COMMIT" 2>/dev/null || true)"
    ALT_NAME="${ALT_NAME:-${ALT_BRANCH:-Commit} $(g rev-parse --short "$ALT_COMMIT")}"
    ALT_VERSION="$(version_aus_commit "$ALT_COMMIT")"
    ins_log "Installiert: $ALT_NAME ($ALT_COMMIT), ausgecheckt: $KOPF_COMMIT"
}

installiert_merken() {
    printf '%s\n' "$1" >"$(marke_datei)" || true
}

ziel_bestimmen() {
    local tag
    case "$ZIEL_ART" in
        release)
            tag="$(g tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)"
            [[ -n "$tag" ]] || abbruch "Auf GitHub gibt es noch keinen Release-Tag (vX.Y.Z). Neuesten Stand mit --main holen."
            ZIEL_COMMIT="$(g rev-parse "refs/tags/$tag^{commit}")"
            ZIEL_NAME="$tag"
            ;;
        version)
            ZIEL_COMMIT="$(g rev-parse -q --verify "refs/tags/$ZIEL_ANGABE^{commit}" 2>/dev/null || true)"
            [[ -n "$ZIEL_COMMIT" ]] || abbruch "Version $ZIEL_ANGABE gibt es nicht. Vorhanden: $(g tag -l 'v*' --sort=-v:refname | head -n 5 | tr '\n' ' ')"
            ZIEL_NAME="$ZIEL_ANGABE"
            ;;
        main)
            ZIEL_COMMIT="$(g rev-parse -q --verify "refs/remotes/origin/main^{commit}" 2>/dev/null || true)"
            [[ -n "$ZIEL_COMMIT" ]] || abbruch "origin/main nicht gefunden."
            ZIEL_NAME="main@$(g rev-parse --short "$ZIEL_COMMIT")"
            ;;
    esac
    ZIEL_VERSION="$(version_aus_commit "$ZIEL_COMMIT")"
}

# Zeigt die CHANGELOG-Abschnitte, die neuer sind als die alte Version.
neuerungen_zeigen() {
    local text
    text="$(g show "$ZIEL_COMMIT:CHANGELOG.md" 2>/dev/null | awk -v alt="$ALT_VERSION" '
        /^## / {
            kopf = $0
            sub(/^## /, "", kopf)
            split(kopf, teile, " ")
            if (alt != "" && teile[1] == alt) { exit }
            if (offen && !inhalt) { puffer = "" }
            printf "%s", puffer
            puffer = $0 "\n"; offen = 1; inhalt = 0
            next
        }
        offen {
            puffer = puffer $0 "\n"
            if ($0 ~ /[^[:space:]]/) { inhalt = 1 }
        }
        END { if (offen && inhalt) { printf "%s", puffer } }
    ' | head -n 80 || true)"
    titel "Neu in $ZIEL_NAME (seit $ALT_NAME):"
    if [[ -n "${text//[[:space:]]/}" ]]; then
        printf '%s\n' "$text" | sed 's/^/    /'
    else
        info "Im CHANGELOG steht dazu nichts."
    fi
}

# ----------------------------------------------------------------------------- Einstellungen ergänzen
# Schlüssel der Vorlage, die in der Env-Datei fehlen (auch auskommentierte zählen als vorhanden).
fehlende_schluessel() {
    local schluessel
    [[ -r "$VORLAGE" ]] || return 0
    while IFS= read -r schluessel; do
        if ! grep -Eq "^[[:space:]]*#?[[:space:]]*(export[[:space:]]+)?${schluessel}=" "$ENV_DATEI" 2>/dev/null; then
            printf '%s\n' "$schluessel"
        fi
    done < <(sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/\1/p' "$VORLAGE")
}

# Kommentarzeilen direkt über einem Schlüssel in der Vorlage.
vorlage_kommentar() {
    awk -v k="$1" '
        /^#/ { block = block $0 "\n"; next }
        index($0, k "=") == 1 { printf "%s", block; exit }
        { block = "" }
    ' "$VORLAGE"
}

env_ergaenzen() {
    local schluessel fehlend kommentar zeile wert
    fehlend="$(fehlende_schluessel)"
    [[ -n "$fehlend" ]] || return 0

    if [[ "$DRY_RUN" -eq 1 ]]; then
        while IFS= read -r schluessel; do
            NEUE_SCHLUESSEL+=("$schluessel")
        done <<<"$fehlend"
        return 0
    fi

    cp -p "$ENV_DATEI" "$ENV_DATEI.vor-update-$(date +%Y%m%d-%H%M%S)" || return 1
    {
        printf '\n# --- neu in %s (ergänzt von scripts/update.sh am %s) ---\n' "$ZIEL_NAME" "$(date +%Y-%m-%d)"
        while IFS= read -r schluessel; do
            # Kommentarzeilen direkt über dem Schlüssel aus der Vorlage mitnehmen.
            kommentar="$(vorlage_kommentar "$schluessel")"
            zeile="$(grep -E "^${schluessel}=" "$VORLAGE" | head -n 1)"
            # $(…) entfernt den letzten Zeilenumbruch – darum hier wieder einfügen.
            [[ -n "$kommentar" ]] && kommentar="$kommentar"$'\n'
            printf '\n%s# neu in %s\n%s\n' "$kommentar" "$ZIEL_NAME" "$zeile"
        done <<<"$fehlend"
    } >>"$ENV_DATEI" || return 1

    while IFS= read -r schluessel; do
        NEUE_SCHLUESSEL+=("$schluessel")
        wert="$(env_wert "$ENV_DATEI" "$schluessel")"
        [[ -z "$wert" ]] || continue
        # Steht in der Vorlage „# Optional: …“, darf der Wert leer bleiben.
        if vorlage_kommentar "$schluessel" | grep -Eqi '^#[[:space:]]*optional'; then
            OPTIONALE_SCHLUESSEL+=("$schluessel")
        else
            LEERE_SCHLUESSEL+=("$schluessel")
        fi
    done <<<"$fehlend"
    ok "Neue Einstellungen in $ENV_DATEI ergänzt: ${NEUE_SCHLUESSEL[*]}"
}

# ----------------------------------------------------------------------------- Installieren und Starten
code_wechseln() {
    leise g checkout -q --detach "$1" || return 1
}

installieren() {
    if [[ "$ART" == "systemd" ]]; then
        leise "$VENV/bin/pip" install --quiet --upgrade "$REPO" || return 1
    else
        leise docker compose -f "$COMPOSE_DATEI" build --pull || return 1
    fi
    if [[ -x "$REPO/scripts/migrationen.sh" ]]; then
        leise "$REPO/scripts/migrationen.sh" "$VENV" || return 1
    fi
}

neu_starten() {
    if [[ "$ART" == "systemd" ]]; then
        leise systemctl restart "$DIENST" || return 1
    else
        leise docker compose -f "$COMPOSE_DATEI" up -d --remove-orphans || return 1
    fi
}

health_url() {
    local host port
    host="$(env_wert "$ENV_DATEI" MISSA_HOST)"
    port="$(env_wert "$ENV_DATEI" MISSA_PORT)"
    case "$host" in "" | 0.0.0.0 | "::") host="127.0.0.1" ;; esac
    printf 'http://%s:%s/health' "$host" "${port:-8000}"
}

# Wartet, bis /health die erwartete Version meldet. llama-server darf dabei fehlen.
gesund_pruefen() {
    local erwartet="$1" wartezeit="${2:-$HEALTH_WARTEZEIT}" url antwort code version ende
    url="$(health_url)"
    antwort="$(mktemp)"
    ende=$((SECONDS + wartezeit))
    while ((SECONDS < ende)); do
        if [[ "$ART" == "systemd" ]] && systemctl is-failed --quiet "$DIENST" 2>/dev/null; then
            fehler "Der Dienst $DIENST ist abgestürzt (journalctl -u $DIENST -n 50)."
            rm -f "$antwort"
            return 1
        fi
        code="$(curl -sS -m 5 -o "$antwort" -w '%{http_code}' "$url" 2>/dev/null || true)"
        if [[ "$code" == "200" || "$code" == "503" ]]; then
            version="$(sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p' "$antwort")"
            if [[ -z "$erwartet" || "$version" == "$erwartet" ]]; then
                if [[ "$code" == "503" ]]; then
                    warnung "Missa-Core läuft, aber llama-server antwortet gerade nicht (siehe /health)."
                fi
                rm -f "$antwort"
                return 0
            fi
        fi
        sleep 2
    done
    rm -f "$antwort"
    fehler "Keine gesunde Antwort von $url innerhalb von ${wartezeit} s."
    return 1
}

# ----------------------------------------------------------------------------- Rücksprung
zurueck() {
    local grund="$1"
    trap - INT TERM
    fehler "$grund"
    titel "Rücksprung auf $ALT_NAME …"
    if [[ -n "$ALT_BRANCH" ]]; then
        leise g checkout -q "$ALT_BRANCH" || true
    else
        leise g checkout -q --detach "$ALT_COMMIT" || true
    fi
    if installieren && neu_starten && gesund_pruefen "$ALT_VERSION" "$RUECKSPRUNG_WARTEZEIT"; then
        installiert_merken "$ALT_COMMIT"
        ok "Die alte Version $ALT_NAME läuft wieder."
    else
        fehler "ACHTUNG: Auch die alte Version $ALT_NAME startet nicht. Bitte prüfen: journalctl -u $DIENST -n 50"
    fi
    if [[ ${#NEUE_SCHLUESSEL[@]} -gt 0 ]]; then
        info "Die neu ergänzten Einstellungen bleiben in $ENV_DATEI stehen; die alte Version ignoriert sie."
    fi
    fehler "Update auf $ZIEL_NAME fehlgeschlagen. Details: ${LOG:-logs/update.log}"
    exit 1
}

# shellcheck disable=SC2329 # wird über "trap abgebrochen INT TERM" aufgerufen
abgebrochen() {
    if [[ "$IN_ARBEIT" -eq 1 ]]; then
        zurueck "Update wurde abgebrochen."
    fi
    exit 130
}

# ----------------------------------------------------------------------------- Ablauf
zusammenfassung() {
    local schluessel
    if [[ ${#NEUE_SCHLUESSEL[@]} -eq 0 ]]; then
        return 0
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
        titel "Diese Einstellungen würden in $ENV_DATEI ergänzt:"
    else
        titel "Neu ergänzte Einstellungen:"
    fi
    for schluessel in "${NEUE_SCHLUESSEL[@]}"; do
        printf '    %s\n' "$schluessel"
    done
    if [[ ${#LEERE_SCHLUESSEL[@]} -gt 0 ]]; then
        printf '\n%s\n' "${GELB}${FETT}Bitte noch ausfüllen in $ENV_DATEI:${NORMAL}"
        for schluessel in "${LEERE_SCHLUESSEL[@]}"; do
            printf '    %s\n' "${GELB}$schluessel${NORMAL}"
        done
        ins_log "Noch auszufüllen: ${LEERE_SCHLUESSEL[*]}"
    fi
    if [[ ${#OPTIONALE_SCHLUESSEL[@]} -gt 0 ]]; then
        printf '\n%s\n' "Optional (darf leer bleiben):"
        for schluessel in "${OPTIONALE_SCHLUESSEL[@]}"; do
            printf '    %s\n' "$schluessel"
        done
    fi
    if [[ ${#LEERE_SCHLUESSEL[@]} -gt 0 || ${#OPTIONALE_SCHLUESSEL[@]} -gt 0 ]]; then
        printf '\n%s\n' "Nach dem Ausfüllen: sudo systemctl restart $DIENST"
    fi
}

main() {
    farben_setzen
    argumente_lesen "$@"
    repo_pruefen
    root_pruefen
    log_oeffnen "$@"
    sperre_holen

    if [[ "$DRY_RUN" -eq 1 ]]; then
        titel "Missa-Core-Update (Probelauf – es wird nichts geändert)"
    else
        titel "Missa-Core-Update"
    fi
    aenderungen_pruefen
    art_erkennen
    info "Repo: $REPO · Startart: $ART${VENV:+ · venv: $VENV} · Einstellungen: $ENV_DATEI"
    [[ -f "$ENV_DATEI" ]] || abbruch "Einstellungsdatei $ENV_DATEI fehlt. Siehe docs/installation.md."

    github_pruefen
    leise g fetch --tags --prune origin || abbruch "git fetch fehlgeschlagen."
    alt_merken
    info "Aktuell: $ALT_NAME (Version ${ALT_VERSION:-?})"
    ziel_bestimmen
    info "Ziel:    $ZIEL_NAME (Version ${ZIEL_VERSION:-?})"

    if [[ "$ZIEL_COMMIT" == "$ALT_COMMIT" && "$KOPF_COMMIT" == "$ALT_COMMIT" && "$NICHT_INSTALLIERT" -eq 1 ]]; then
        info "Der Code ist schon auf $ZIEL_NAME, aber noch nicht installiert – installiere jetzt."
    elif [[ "$ZIEL_COMMIT" == "$ALT_COMMIT" && "$KOPF_COMMIT" == "$ALT_COMMIT" ]]; then
        if [[ "$ERZWINGEN" -eq 0 ]]; then
            [[ "$DRY_RUN" -eq 1 ]] || installiert_merken "$ALT_COMMIT"
            ok "Missa-Core ist schon aktuell ($ALT_NAME). Kein Neustart nötig."
            exit 0
        fi
        info "Schon aktuell – wegen --erzwingen wird trotzdem neu installiert und neu gestartet."
    elif g merge-base --is-ancestor "$ALT_COMMIT" "$ZIEL_COMMIT"; then
        neuerungen_zeigen
    else
        warnung "$ZIEL_NAME ist nicht neuer als $ALT_NAME – es wird zurück bzw. auf einen anderen Stand gewechselt."
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        local vorlage_ziel
        vorlage_ziel="$(mktemp)"
        g show "$ZIEL_COMMIT:${VORLAGE#"$REPO"/}" >"$vorlage_ziel" 2>/dev/null || true
        VORLAGE="$vorlage_ziel" env_ergaenzen
        rm -f "$vorlage_ziel"
        zusammenfassung
        titel "Probelauf beendet – es wurde nichts geändert."
        info "Würde: Code auf $ZIEL_NAME wechseln, Abhängigkeiten installieren, $DIENST neu starten, /health prüfen."
        exit 0
    fi

    trap abgebrochen INT TERM
    IN_ARBEIT=1
    titel "Update auf $ZIEL_NAME"
    code_wechseln "$ZIEL_COMMIT" || zurueck "Code konnte nicht auf $ZIEL_NAME gewechselt werden."
    ok "Code auf $ZIEL_NAME gewechselt."
    installieren || zurueck "Installation der Abhängigkeiten fehlgeschlagen."
    ok "Abhängigkeiten installiert."
    env_ergaenzen || zurueck "Einstellungen konnten nicht ergänzt werden."
    if [[ "$ART" == "systemd" && -f /etc/systemd/system/$DIENST.service ]] &&
        ! cmp -s "$REPO/deploy/$DIENST.service" "/etc/systemd/system/$DIENST.service"; then
        warnung "deploy/$DIENST.service weicht von der installierten Unit ab – bei Bedarf übernehmen (docs/update.md)."
    fi
    neu_starten || zurueck "Neustart von $DIENST fehlgeschlagen."
    info "Dienst neu gestartet, warte auf /health (bis ${HEALTH_WARTEZEIT} s) …"
    if [[ -n "$TEST_FEHLER" ]]; then
        zurueck "Testmodus (MISSA_UPDATE_TEST_FEHLER): Gesundheitsprüfung absichtlich fehlgeschlagen."
    fi
    gesund_pruefen "$ZIEL_VERSION" || zurueck "Gesundheitsprüfung nach dem Update fehlgeschlagen."
    trap - INT TERM

    installiert_merken "$ZIEL_COMMIT"
    ok "${FETT}Missa-Core läuft jetzt mit $ZIEL_NAME (Version ${ZIEL_VERSION:-?}).${NORMAL}"
    zusammenfassung
    ins_log "===== Update erfolgreich: $ALT_NAME → $ZIEL_NAME"
}

main "$@"
exit $?
