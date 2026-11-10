#!/usr/bin/env bash
# Vorlage: Missa als Bot in Nextcloud Talk einrichten. Läuft auf dem Nextcloud-Server (int01).
# Enthält keine echten Geheimnisse – das Bot-Secret wird abgefragt oder aus
# NC_TALK_BOT_SECRET gelesen und muss dasselbe sein wie in /etc/missa/missa.env auf ai01.
#
#   ./talk-bot-einrichten.sh installieren      Bot "Missa" in Nextcloud installieren
#   ./talk-bot-einrichten.sh liste             installierte Bots anzeigen (Bot-ID ablesen)
#   ./talk-bot-einrichten.sh einrichten <bot-id> <token>   Bot in einer Unterhaltung aktivieren
#   ./talk-bot-einrichten.sh entfernen <bot-id> <token>    Bot aus einer Unterhaltung entfernen
#
# Anpassbar über Umgebungsvariablen (Beispiele):
#   OCC="sudo -u www-data php /var/www/nextcloud/occ"                (Standard)
#   OCC="docker exec -u www-data nextcloud php occ"                  (Nextcloud im Container)
#   MISSA_WEBHOOK="http://<IP-von-ai01>:8000/channels/nextcloud-talk/webhook"
#   DRY_RUN=1   nur anzeigen, nichts ausführen
#
# Ausführliche Anleitung: docs/nextcloud-talk.md

set -euo pipefail

OCC="${OCC:-sudo -u www-data php /var/www/nextcloud/occ}"
BOT_NAME="${BOT_NAME:-Missa}"
BOT_BESCHREIBUNG="${BOT_BESCHREIBUNG:-Persönliche Assistentin von Louis}"
MISSA_WEBHOOK="${MISSA_WEBHOOK:-}"
DRY_RUN="${DRY_RUN:-0}"
MASKE=""

occ() {
    local -a befehl
    read -r -a befehl <<<"$OCC"
    if [[ "$DRY_RUN" == "1" ]]; then
        local teil
        printf 'würde ausführen: %s' "$OCC"
        for teil in "$@"; do
            [[ -n "$MASKE" && "$teil" == "$MASKE" ]] && teil="<secret>"
            printf ' "%s"' "$teil"
        done
        printf '\n'
        return 0
    fi
    "${befehl[@]}" "$@"
}

hilfe() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

installieren() {
    local secret="${NC_TALK_BOT_SECRET:-}"
    if [[ -z "$MISSA_WEBHOOK" ]]; then
        read -r -p "Webhook-Adresse von Missa (z. B. http://<IP-von-ai01>:8000/channels/nextcloud-talk/webhook): " MISSA_WEBHOOK
    fi
    if [[ -z "$secret" ]]; then
        read -r -s -p "Bot-Secret (gleicher Wert wie NC_TALK_BOT_SECRET auf ai01): " secret
        printf '\n'
    fi
    MASKE="$secret"
    if ((${#secret} < 40 || ${#secret} > 128)); then
        echo "Das Secret muss 40 bis 128 Zeichen lang sein (erzeugen: openssl rand -hex 32)." >&2
        exit 1
    fi
    occ talk:bot:install \
        --feature webhook --feature response --feature reaction \
        "$BOT_NAME" "$secret" "$MISSA_WEBHOOK" "$BOT_BESCHREIBUNG"
    echo "Fertig. Jetzt die Bot-ID ablesen: $0 liste"
}

case "${1:-}" in
    installieren) installieren ;;
    liste) occ talk:bot:list ;;
    einrichten)
        [[ $# -eq 3 ]] || {
            echo "Aufruf: $0 einrichten <bot-id> <token der Unterhaltung>" >&2
            exit 1
        }
        occ talk:bot:setup "$2" "$3"
        ;;
    entfernen)
        [[ $# -eq 3 ]] || {
            echo "Aufruf: $0 entfernen <bot-id> <token der Unterhaltung>" >&2
            exit 1
        }
        occ talk:bot:remove "$2" "$3"
        ;;
    -h | --help | "") hilfe ;;
    *)
        echo "Unbekannter Befehl: $1" >&2
        hilfe
        exit 1
        ;;
esac
