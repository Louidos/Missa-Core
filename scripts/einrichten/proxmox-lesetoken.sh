#!/usr/bin/env bash
# Legt auf dem Proxmox-Host (pve-id01) ein NUR LESENDES API-Token für Missa an.
# Louis führt das Script von Hand als root auf pve-id01 aus – Missa selbst nie.
#
#   bash proxmox-lesetoken.sh            # anlegen (gibt das Secret einmalig aus)
#   bash proxmox-lesetoken.sh --dry-run  # nur zeigen, was passieren würde
#
# Was es tut (und nicht mehr):
#   - Benutzer missa@pve ohne Passwort (kein Login in der Weboberfläche möglich)
#   - Rolle PVEAuditor (nur lesen) auf / für den Benutzer
#   - Token missa@pve!monitor mit eigener Rechte-Trennung (privsep) und ebenfalls nur PVEAuditor
# Kein Token-Recht zum Starten, Stoppen, Ändern oder Löschen. Spätere Schreibrechte (eigener
# Pool für Missa-VMs) bekommen ein eigenes Token mit eigenem Script.
# Beschreibung: docs/server-alarme.md (Abschnitt „Proxmox einrichten“)

set -Eeuo pipefail

BENUTZER="${PROXMOX_BENUTZER:-missa@pve}"
TOKEN="${PROXMOX_TOKEN:-monitor}"
ROLLE="PVEAuditor"
PFAD="/"
DRY_RUN=0

for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        -h | --help)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)
            echo "Unbekannte Option: $argument (erlaubt: --dry-run)" >&2
            exit 2
            ;;
    esac
done

# Ändernde Befehle laufen nur ohne --dry-run; lesende immer.
aendern() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'würde ausführen: %s\n' "$*"
    else
        "$@"
    fi
}

main() {
    if ! command -v pveum >/dev/null 2>&1; then
        echo "pveum fehlt – das Script läuft nur auf dem Proxmox-Host (pve-id01)." >&2
        exit 1
    fi
    if [[ "$DRY_RUN" -eq 0 && "$EUID" -ne 0 ]]; then
        echo "Bitte als root ausführen (oder mit --dry-run nur anschauen)." >&2
        exit 1
    fi

    if pveum user list --output-format json | grep -Eq "\"userid\": ?\"$BENUTZER\""; then
        echo "Benutzer $BENUTZER gibt es schon."
    else
        aendern pveum user add "$BENUTZER" --comment "Missa (nur lesen, Monitoring)"
    fi

    aendern pveum acl modify "$PFAD" --users "$BENUTZER" --roles "$ROLLE" --propagate 1

    if pveum user token list "$BENUTZER" --output-format json 2>/dev/null |
        grep -Eq "\"tokenid\": ?\"$TOKEN\""; then
        echo "Token $BENUTZER!$TOKEN gibt es schon. Neu erzeugen (altes Secret wird ungültig):"
        echo "  pveum user token remove $BENUTZER $TOKEN && bash $0"
    else
        echo "Neues Token $BENUTZER!$TOKEN – das Secret wird nur jetzt angezeigt:"
        aendern pveum user token add "$BENUTZER" "$TOKEN" --privsep 1 \
            --comment "Missa: Monitoring, nur lesen"
    fi
    aendern pveum acl modify "$PFAD" --tokens "$BENUTZER!$TOKEN" --roles "$ROLLE" --propagate 1

    cat <<EOF

Fertig. In /etc/missa/missa.env auf ai01 eintragen:
  PROXMOX_URL=https://<adresse-von-pve-id01>:8006
  PROXMOX_TOKEN_ID=$BENUTZER!$TOKEN
  PROXMOX_TOKEN_SECRET=<das Secret von oben, Feld "value">
Für die Zertifikatsprüfung /etc/pve/pve-root-ca.pem nach ai01 kopieren und
PROXMOX_CA_DATEI darauf zeigen lassen. Danach: sudo systemctl restart missa-core
Netzfreigabe nötig: ai01 → pve-id01 TCP 8006 (siehe docs/netzfreigaben.md).
EOF
}

main
