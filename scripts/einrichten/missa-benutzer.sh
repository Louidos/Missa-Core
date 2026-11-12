#!/usr/bin/env bash
# Richtet auf einem Host (z. B. wb01, int01, rp01) den Benutzer „missa“ für Missa ein.
# Louis führt das Script von Hand als root auf dem jeweiligen Host aus – Missa selbst nie.
#
#   bash missa-benutzer.sh --schluessel "ssh-ed25519 AAAA… missa@ai01"
#   bash missa-benutzer.sh --schluessel-datei missa.pub --nextcloud-log /var/log/nextcloud/nextcloud.log
#   bash missa-benutzer.sh --dry-run --schluessel "…"   # nur zeigen, was passieren würde
#   bash missa-benutzer.sh --caddy --schluessel "…"     # auf rp01: Subdomains vorbereiten
#   bash missa-benutzer.sh --entfernen                  # alles wieder entfernen
#   bash missa-benutzer.sh --nur-sudoers   # erzeugte Dateien nur anzeigen (auch --nur-befehl,
#                                          # --nur-docker, --nur-update, --nur-caddy)
#
# Was „missa“ danach darf (und nicht mehr):
#   - per SSH nur mit Missas Schlüssel und NUR das Programm /usr/local/lib/missa/missa-befehl
#     (keine Shell, kein Terminal, keine Weiterleitungen: authorized_keys mit restrict,command=)
#   - per sudo NUR das Programm /usr/local/lib/missa/missa-docker, und das kennt nur:
#     docker ps, docker stats --no-stream (nur lesen), docker logs, docker restart
#   - per sudo NUR das Programm /usr/local/lib/missa/missa-update, und das kennt nur:
#     apt-get update, apt-get -y upgrade und reboot (Neustart nach 1 Minute)
#   - die Liste anstehender Updates lesen (apt list --upgradable, ohne sudo)
#   - Logs lesen (journalctl) über die Gruppe systemd-journal – ohne sudo, ohne Pager
#   - optional die eine Nextcloud-Logdatei lesen (--nextcloud-log, per ACL nur diese Datei)
#   - optional auf rp01 (--caddy): nur Dateien missa-*.caddy im eigenen Ordner
#     /etc/caddy/missa schreiben und löschen, per sudo nur missa-caddy (caddy validate,
#     systemctl reload caddy). Das bestehende Caddyfile bleibt unangetastet.
# Auf dem Proxmox-Host nur mit --proxmox-ja (siehe docs/container-logs-logins.md).

set -Eeuo pipefail

BENUTZER="missa"
HEIMAT="/var/lib/missa"
BEFEHL_DATEI="/usr/local/lib/missa/missa-befehl"
# Ort des sudo-Programms für Docker (änderbar nur für Tests).
DOCKER_DATEI="${MISSA_DOCKER_DATEI:-/usr/local/lib/missa/missa-docker}"
# Ort des sudo-Programms für Updates und Neustart (änderbar nur für Tests).
UPDATE_DATEI="${MISSA_UPDATE_DATEI:-/usr/local/lib/missa/missa-update}"
# Caddy auf rp01: sudo-Programm, eigener Ordner und das bestehende Caddyfile (nur lesen).
CADDY_DATEI="${MISSA_CADDY_DATEI:-/usr/local/lib/missa/missa-caddy}"
CADDY_ORDNER="${MISSA_CADDY_ORDNER:-/etc/caddy/missa}"
CADDYFILE="${MISSA_CADDYFILE:-/etc/caddy/Caddyfile}"
CADDY_JA=0
SUDOERS_DATEI="/etc/sudoers.d/missa"
# Woran ein Proxmox-Host zu erkennen ist (änderbar nur für Tests).
PVE_ORDNER="${PVE_ORDNER:-/etc/pve}"
SCHLUESSEL=""
NEXTCLOUD_LOG=""
PROXMOX_JA=0
DRY_RUN=0
AKTION="einrichten"

hilfe() { sed -n '2,/^$/p' "$0"; }
abbruch() {
    echo "Fehler: $*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --schluessel)
            [[ $# -ge 2 ]] || abbruch "--schluessel braucht den öffentlichen Schlüssel"
            SCHLUESSEL="$2"
            shift
            ;;
        --schluessel-datei)
            [[ $# -ge 2 && -r "$2" ]] || abbruch "--schluessel-datei braucht eine lesbare Datei"
            SCHLUESSEL="$(head -n 1 "$2")"
            shift
            ;;
        --nextcloud-log)
            [[ $# -ge 2 ]] || abbruch "--nextcloud-log braucht den Pfad zur Logdatei"
            NEXTCLOUD_LOG="$2"
            shift
            ;;
        --proxmox-ja) PROXMOX_JA=1 ;;
        --caddy) CADDY_JA=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --entfernen) AKTION="entfernen" ;;
        --nur-sudoers) AKTION="sudoers" ;;
        --nur-befehl) AKTION="befehl" ;;
        --nur-docker) AKTION="docker" ;;
        --nur-update) AKTION="update" ;;
        --nur-caddy) AKTION="caddy" ;;
        -h | --help)
            hilfe
            exit 0
            ;;
        *) abbruch "Unbekannte Option: $1 (siehe --help)" ;;
    esac
    shift
done

DOCKER="$(command -v docker || true)"
APT_GET="$(command -v apt-get || true)"
SHUTDOWN="$(command -v shutdown || true)"
CADDY_BIN="$(command -v caddy || true)"
SYSTEMCTL="$(command -v systemctl || true)"

# --------------------------------------------------------------------- erzeugte Dateien

sudoers_inhalt() {
    # Ohne Platzhalter und ohne Sonder-Optionen: gilt für sudo und sudo-rs gleich.
    local programme=()
    [[ -n "$DOCKER" ]] && programme+=("$DOCKER_DATEI")
    [[ -n "$APT_GET" ]] && programme+=("$UPDATE_DATEI")
    [[ "$CADDY_JA" -eq 1 ]] && programme+=("$CADDY_DATEI")
    cat <<EOF
# Verwaltet von missa-benutzer.sh – nicht von Hand ändern (Änderungen gehen verloren).
# Missa darf per sudo NUR diese Programme ausführen:
#   $DOCKER_DATEI – docker ps, docker stats --no-stream, docker logs, docker restart
#   $UPDATE_DATEI – apt-get update, apt-get -y upgrade, reboot
#   $CADDY_DATEI – caddy validate, systemctl reload caddy (nur mit --caddy)
# Logs liest missa über die Gruppe systemd-journal, dafür braucht es kein sudo.
EOF
    if ((${#programme[@]})); then
        local liste
        liste="$(printf '%s, ' "${programme[@]}")"
        echo "$BENUTZER ALL=(root) NOPASSWD: ${liste%, }"
    else
        echo "# Weder Docker noch apt-get auf diesem Host – keine sudo-Rechte."
    fi
}

caddy_inhalt() {
    sed -e "s|@CADDY_BIN@|${CADDY_BIN}|" -e "s|@SYSTEMCTL@|${SYSTEMCTL}|" \
        -e "s|@CADDYFILE@|${CADDYFILE}|" <<'CADDY'
#!/usr/bin/env bash
# missa-caddy – Caddy prüfen und neu laden, mehr nicht (per sudo als root).
# Verwaltet von missa-benutzer.sh.
set -euo pipefail
export PATH="/usr/sbin:/usr/bin:/sbin:/bin"
CADDY="@CADDY_BIN@"
SYSTEMCTL="@SYSTEMCTL@"
CADDYFILE="@CADDYFILE@"

fehler() {
    echo "missa-caddy: $*" >&2
    exit 64
}

[[ $# -eq 1 ]] || fehler "genau eine Angabe erwartet: validate oder reload"
case "$1" in
    validate)
        [[ -x "$CADDY" ]] || fehler "caddy fehlt auf diesem Host"
        exec "$CADDY" validate --config "$CADDYFILE" --adapter caddyfile
        ;;
    reload)
        [[ -x "$SYSTEMCTL" ]] || fehler "systemctl fehlt auf diesem Host"
        exec "$SYSTEMCTL" reload caddy
        ;;
    *) fehler "nicht erlaubt: $1" ;;
esac
CADDY
}

update_inhalt() {
    sed -e "s|@APT_GET@|${APT_GET}|" -e "s|@SHUTDOWN@|${SHUTDOWN}|" <<'UPDATE'
#!/usr/bin/env bash
# missa-update – Updates und Neustart, die missa per sudo (als root) auslösen darf.
# Verwaltet von missa-benutzer.sh. Kennt nur: update, upgrade, reboot – ohne weitere Angaben.
set -euo pipefail
export PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
APT_GET="@APT_GET@"
SHUTDOWN="@SHUTDOWN@"

fehler() {
    echo "missa-update: $*" >&2
    exit 64
}

[[ $# -eq 1 ]] || fehler "genau eine Angabe erwartet: update, upgrade oder reboot"
case "$1" in
    update)
        [[ -x "$APT_GET" ]] || fehler "apt-get fehlt auf diesem Host"
        exec "$APT_GET" update
        ;;
    upgrade)
        [[ -x "$APT_GET" ]] || fehler "apt-get fehlt auf diesem Host"
        # Geänderte Konfigurationsdateien bleiben, wie sie sind (keine Rückfragen).
        exec "$APT_GET" -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
        ;;
    reboot)
        # Neustart in 1 Minute, damit Missa die Antwort noch bekommt.
        [[ -x "$SHUTDOWN" ]] || fehler "shutdown fehlt auf diesem Host"
        exec "$SHUTDOWN" -r +1 "Neustart durch Missa (nach Louis' Ja)"
        ;;
    *) fehler "nicht erlaubt: $1" ;;
esac
UPDATE
}

docker_inhalt() {
    sed -e "s|@DOCKER@|${DOCKER}|" <<'DOCKER'
#!/usr/bin/env bash
# missa-docker – die einzigen Docker-Befehle, die missa per sudo (als root) ausführen darf.
# Verwaltet von missa-benutzer.sh. Jede Angabe wird geprüft, es gibt kein eval.
set -euo pipefail
export PATH="/usr/sbin:/usr/bin:/sbin:/bin"
DOCKER="@DOCKER@"

fehler() {
    echo "missa-docker: $*" >&2
    exit 64
}
name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fehler "ungültiger Name: $1"; }

[[ -n "$DOCKER" && -x "$DOCKER" ]] || fehler "Docker ist auf diesem Host nicht installiert"
case "${1:-}:$#" in
    ps:1) exec "$DOCKER" ps --all --no-trunc --format '{{json .}}' ;;
    stats:1) exec "$DOCKER" stats --no-stream --no-trunc --format '{{json .}}' ;;
    logs:3)
        name "$2"
        if [[ ! "$3" =~ ^[0-9]{1,4}$ ]] || ((10#$3 < 1 || 10#$3 > 5000)); then
            fehler "ungültige Zeilenzahl: $3"
        fi
        exec "$DOCKER" logs --tail "$((10#$3))" --timestamps "$2"
        ;;
    restart:2)
        name "$2"
        exec "$DOCKER" restart "$2"
        ;;
    *) fehler "nicht erlaubt: $*" ;;
esac
DOCKER
}

befehl_inhalt() {
    # Platzhalter werden beim Einrichten durch die Werte dieses Hosts ersetzt.
    local caddy_programm=""
    [[ "$CADDY_JA" -eq 1 ]] && caddy_programm="$CADDY_DATEI"
    sed -e "s|@DOCKER_DATEI@|${DOCKER_DATEI}|" -e "s|@UPDATE_DATEI@|${UPDATE_DATEI}|" \
        -e "s|@CADDY_DATEI@|${caddy_programm}|" -e "s|@CADDY_ORDNER@|${CADDY_ORDNER}|" \
        -e "s|@NEXTCLOUD_LOG@|${NEXTCLOUD_LOG}|" <<'BEFEHL'
#!/usr/bin/env bash
# missa-befehl – die einzigen Befehle, die Missa per SSH auf diesem Host ausführen kann.
# Verwaltet von missa-benutzer.sh. Läuft als Benutzer missa (nicht als root).
set -euo pipefail
export PATH="${MISSA_TEST_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
export SYSTEMD_PAGER="" PAGER="cat" SYSTEMD_COLORS=0 LC_ALL=C.UTF-8
DOCKER="@DOCKER_DATEI@"
UPDATE="@UPDATE_DATEI@"
CADDY="@CADDY_DATEI@"
CADDY_ORDNER="@CADDY_ORDNER@"
NEXTCLOUD_LOG="@NEXTCLOUD_LOG@"
MAX_ZEILEN=5000

fehler() {
    echo "missa-befehl: $*" >&2
    exit 64
}
zeilen() {
    if [[ ! "$1" =~ ^[0-9]{1,4}$ ]] || ((10#$1 < 1 || 10#$1 > MAX_ZEILEN)); then
        fehler "ungültige Zeilenzahl: $1"
    fi
    echo "$((10#$1))"
}
name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fehler "ungültiger Name: $1"; }
seit() { [[ "$1" =~ ^@[0-9]{1,12}$ ]] || fehler "ungültige Zeitangabe: $1"; }
unit() { [[ "$1" =~ ^[A-Za-z0-9*][A-Za-z0-9@._:*-]{0,127}$ ]] || fehler "ungültige Unit: $1"; }
prio() { [[ "$1" =~ ^[0-7]$ ]] || fehler "ungültige Priorität: $1"; }
docker_da() { [[ -x "$DOCKER" ]] || fehler "Docker ist auf diesem Host nicht installiert"; }

read -r -a teile <<<"${SSH_ORIGINAL_COMMAND:-}"
befehl="${teile[0]:-}"
argumente=("${teile[@]:1}")
anzahl="${#argumente[@]}"

case "$befehl" in
    pruefen)
        echo "missa-befehl ok $(hostname)"
        ;;
    docker-ps)
        docker_da
        ((anzahl == 0)) || fehler "docker-ps hat keine Angaben"
        exec sudo -n "$DOCKER" ps
        ;;
    docker-stats)
        docker_da
        ((anzahl == 0)) || fehler "docker-stats hat keine Angaben"
        exec sudo -n "$DOCKER" stats
        ;;
    docker-logs)
        docker_da
        ((anzahl == 2)) || fehler "docker-logs braucht Name und Zeilenzahl"
        name "${argumente[0]}"
        n="$(zeilen "${argumente[1]}")"
        exec sudo -n "$DOCKER" logs "${argumente[0]}" "$n" 2>&1
        ;;
    docker-restart)
        docker_da
        ((anzahl == 1)) || fehler "docker-restart braucht genau einen Namen"
        name "${argumente[0]}"
        exec sudo -n "$DOCKER" restart "${argumente[0]}"
        ;;
    journal | ssh-log)
        optionen=(--no-pager --quiet -o short-iso -n 500)
        [[ "$befehl" == "ssh-log" ]] && optionen+=(-t sshd -t sshd-session)
        for angabe in "${argumente[@]}"; do
            schluessel="${angabe%%=*}"
            wert="${angabe#*=}"
            [[ "$angabe" == *=* ]] || fehler "Angabe ohne =: $angabe"
            case "$befehl:$schluessel" in
                *:seit) seit "$wert" && optionen+=(--since "$wert") ;;
                *:zeilen)
                    n="$(zeilen "$wert")"
                    optionen+=(-n "$n")
                    ;;
                journal:unit) unit "$wert" && optionen+=(-u "$wert") ;;
                journal:ident) name "$wert" && optionen+=(-t "$wert") ;;
                journal:prio) prio "$wert" && optionen+=(-p "$wert") ;;
                *) fehler "Angabe nicht erlaubt: $schluessel" ;;
            esac
        done
        exec journalctl "${optionen[@]}"
        ;;
    apt-update | apt-upgrade | neustart)
        [[ -x "$UPDATE" ]] || fehler "Updates sind auf diesem Host nicht eingerichtet"
        ((anzahl == 0)) || fehler "$befehl hat keine Angaben"
        case "$befehl" in
            apt-update) exec sudo -n "$UPDATE" update 2>&1 ;;
            apt-upgrade) exec sudo -n "$UPDATE" upgrade 2>&1 ;;
            neustart) exec sudo -n "$UPDATE" reboot 2>&1 ;;
        esac
        ;;
    apt-liste)
        ((anzahl == 0)) || fehler "apt-liste hat keine Angaben"
        exec apt list --upgradable 2>/dev/null
        ;;
    neustart-noetig)
        ((anzahl == 0)) || fehler "neustart-noetig hat keine Angaben"
        if [[ -e /var/run/reboot-required ]]; then
            echo "ja"
            cat /var/run/reboot-required.pkgs 2>/dev/null || true
        else
            echo "nein"
        fi
        ;;
    caddy-liste | caddy-schreiben | caddy-loeschen | caddy-pruefen | caddy-neu-laden)
        [[ -n "$CADDY" && -d "$CADDY_ORDNER" ]] || fehler "Caddy ist auf diesem Host nicht eingerichtet (--caddy)"
        case "$befehl" in
            caddy-liste)
                ((anzahl == 0)) || fehler "caddy-liste hat keine Angaben"
                for datei in "$CADDY_ORDNER"/missa-*.caddy; do
                    [[ -e "$datei" ]] || continue
                    echo "### ${datei##*/}"
                    cat -- "$datei"
                done
                ;;
            caddy-schreiben | caddy-loeschen)
                ((anzahl == 1)) || fehler "$befehl braucht genau einen Dateinamen"
                [[ "${argumente[0]}" =~ ^missa-[a-z0-9-]{1,40}\.caddy$ ]] ||
                    fehler "nur Dateien missa-<name>.caddy sind erlaubt"
                ziel="$CADDY_ORDNER/${argumente[0]}"
                if [[ "$befehl" == "caddy-loeschen" ]]; then
                    rm -f -- "$ziel"
                    echo "entfernt: ${argumente[0]}"
                else
                    inhalt="$(head -c 4097)"
                    ((${#inhalt} <= 4096)) || fehler "Eintrag zu lang (höchstens 4096 Zeichen)"
                    if grep -Eq '^[[:space:]]*(import|\{)' <<<"$inhalt"; then
                        fehler "import und globale Optionen sind in einem Eintrag nicht erlaubt"
                    fi
                    zwischen="$(mktemp "$CADDY_ORDNER/.neu.XXXXXX")"
                    printf '%s\n' "$inhalt" >"$zwischen"
                    chmod 0644 "$zwischen"
                    mv -f -- "$zwischen" "$ziel"
                    echo "geschrieben: ${argumente[0]}"
                fi
                ;;
            caddy-pruefen)
                ((anzahl == 0)) || fehler "caddy-pruefen hat keine Angaben"
                exec sudo -n "$CADDY" validate 2>&1
                ;;
            caddy-neu-laden)
                ((anzahl == 0)) || fehler "caddy-neu-laden hat keine Angaben"
                exec sudo -n "$CADDY" reload 2>&1
                ;;
        esac
        ;;
    nextcloud-log)
        [[ -n "$NEXTCLOUD_LOG" ]] || fehler "kein Nextcloud-Log eingerichtet (--nextcloud-log)"
        ((anzahl == 1)) || fehler "nextcloud-log braucht die Zeilenzahl"
        n="$(zeilen "${argumente[0]}")"
        exec tail -n "$n" -- "$NEXTCLOUD_LOG"
        ;;
    *)
        fehler "Befehl nicht erlaubt: ${befehl:-(leer)}"
        ;;
esac
BEFEHL
}

# --------------------------------------------------------------------- Ausführen

aendern() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'würde ausführen: %s\n' "$*"
    else
        "$@"
    fi
}

datei_schreiben() {
    local ziel="$1" modus="$2" besitzer="$3" inhalt="$4" zwischen
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'würde schreiben: %s (%s, %s)\n' "$ziel" "$modus" "$besitzer"
        return
    fi
    zwischen="$(mktemp "${ziel}.XXXXXX")"
    printf '%s\n' "$inhalt" >"$zwischen"
    chmod "$modus" "$zwischen"
    chown "$besitzer" "$zwischen"
    mv -f "$zwischen" "$ziel"
}

pruefungen() {
    if [[ "$DRY_RUN" -eq 0 && "$EUID" -ne 0 ]]; then
        abbruch "Bitte als root ausführen (oder mit --dry-run nur anschauen)."
    fi
    if [[ -d "$PVE_ORDNER" || -x /usr/bin/pveversion ]] && [[ "$PROXMOX_JA" -ne 1 ]]; then
        abbruch "Das ist ein Proxmox-Host. Nur mit --proxmox-ja einrichten, wenn du das wirklich willst (docs/container-logs-logins.md)."
    fi
}

einrichten() {
    pruefungen
    [[ "$SCHLUESSEL" =~ ^(ssh-ed25519|ecdsa-sha2-nistp[0-9]+|ssh-rsa)\ [A-Za-z0-9+/=]+(\ .*)?$ ]] ||
        abbruch "--schluessel muss ein öffentlicher SSH-Schlüssel sein (ssh-ed25519 AAAA…)."
    if [[ -n "$NEXTCLOUD_LOG" && ! -f "$NEXTCLOUD_LOG" && "$DRY_RUN" -eq 0 ]]; then
        abbruch "Die Nextcloud-Logdatei $NEXTCLOUD_LOG gibt es nicht."
    fi
    if [[ -n "$NEXTCLOUD_LOG" && ! "$NEXTCLOUD_LOG" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
        abbruch "Der Pfad zur Nextcloud-Logdatei darf nur Buchstaben, Ziffern, . _ - / enthalten."
    fi

    if id "$BENUTZER" >/dev/null 2>&1; then
        echo "Benutzer $BENUTZER gibt es schon."
    else
        aendern useradd --system --create-home --home-dir "$HEIMAT" --shell /bin/bash \
            --comment "Missa (nur fest erlaubte Befehle)" "$BENUTZER"
    fi
    # Kein Passwort: Anmelden geht nur mit dem Schlüssel.
    aendern passwd --lock "$BENUTZER"
    if getent group systemd-journal >/dev/null 2>&1; then
        aendern usermod --append --groups systemd-journal "$BENUTZER"
    else
        echo "Hinweis: Gruppe systemd-journal fehlt – journal und ssh-log gehen dann nicht."
    fi

    aendern install -d -m 0755 -o root -g root "$(dirname "$BEFEHL_DATEI")"
    datei_schreiben "$BEFEHL_DATEI" 0755 root:root "$(befehl_inhalt)"
    if [[ -n "$DOCKER" ]]; then
        datei_schreiben "$DOCKER_DATEI" 0755 root:root "$(docker_inhalt)"
    fi
    if [[ -n "$APT_GET" ]]; then
        datei_schreiben "$UPDATE_DATEI" 0755 root:root "$(update_inhalt)"
    fi
    if [[ "$CADDY_JA" -eq 1 ]]; then
        [[ -n "$CADDY_BIN" || "$DRY_RUN" -eq 1 ]] || abbruch "--caddy: caddy ist auf diesem Host nicht installiert."
        datei_schreiben "$CADDY_DATEI" 0755 root:root "$(caddy_inhalt)"
        # Eigener Ordner, den nur missa beschreibt; das Caddyfile wird NICHT verändert.
        aendern install -d -m 0755 -o "$BENUTZER" -g "$BENUTZER" "$CADDY_ORDNER"
        if ! grep -Fq "import $CADDY_ORDNER/missa-*.caddy" "$CADDYFILE" 2>/dev/null; then
            echo "Hinweis: Füge in $CADDYFILE von Hand diese Zeile ganz unten ein (außerhalb jedes Blocks):"
            echo "  import $CADDY_ORDNER/missa-*.caddy"
        fi
    fi

    local heimat
    heimat="$(getent passwd "$BENUTZER" | cut -d: -f6 || true)"
    heimat="${heimat:-$HEIMAT}"
    aendern install -d -m 0700 -o "$BENUTZER" -g "$BENUTZER" "$heimat/.ssh"
    datei_schreiben "$heimat/.ssh/authorized_keys" 0600 "$BENUTZER:$BENUTZER" \
        "restrict,command=\"$BEFEHL_DATEI\" $SCHLUESSEL"

    local inhalt zwischen
    inhalt="$(sudoers_inhalt)"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'würde schreiben: %s (0440, root:root):\n%s\n' "$SUDOERS_DATEI" "$inhalt"
    else
        zwischen="$(mktemp)"
        printf '%s\n' "$inhalt" >"$zwischen"
        visudo -cf "$zwischen" >/dev/null || abbruch "sudoers-Datei ungültig – nichts geändert."
        install -m 0440 -o root -g root "$zwischen" "$SUDOERS_DATEI"
        rm -f "$zwischen"
    fi

    if [[ -n "$NEXTCLOUD_LOG" ]]; then
        # Nur diese eine Datei lesbar machen, dazu Durchgang (x) durch die Ordner darüber.
        aendern setfacl -m "u:$BENUTZER:r" "$NEXTCLOUD_LOG"
        local ordner
        ordner="$(dirname "$NEXTCLOUD_LOG")"
        while [[ "$ordner" != "/" ]]; do
            aendern setfacl -m "u:$BENUTZER:x" "$ordner"
            ordner="$(dirname "$ordner")"
        done
    fi

    cat <<EOF

Fertig: Benutzer $BENUTZER auf $(hostname) eingerichtet.
Auf ai01 den Host in /etc/missa/missa.env eintragen (SSH_HOSTS=…) und den Host-Schlüssel
prüfen und übernehmen (siehe docs/container-logs-logins.md). Test von ai01 aus:
  sudo -u missa ssh -i /etc/missa/ssh/id_ed25519 $BENUTZER@<adresse> pruefen
EOF
}

entfernen() {
    pruefungen
    aendern rm -f "$SUDOERS_DATEI" "$BEFEHL_DATEI" "$DOCKER_DATEI" "$UPDATE_DATEI" "$CADDY_DATEI"
    if id "$BENUTZER" >/dev/null 2>&1; then
        aendern userdel --remove "$BENUTZER"
    fi
    echo "Benutzer $BENUTZER, sudo-Regeln und missa-befehl entfernt. ACLs auf einer Nextcloud-Logdatei"
    echo "bitte bei Bedarf von Hand entfernen: setfacl -x u:$BENUTZER <datei>"
}

case "$AKTION" in
    sudoers) sudoers_inhalt ;;
    befehl) befehl_inhalt ;;
    docker) docker_inhalt ;;
    update) update_inhalt ;;
    caddy) caddy_inhalt ;;
    entfernen) entfernen ;;
    *) einrichten ;;
esac
