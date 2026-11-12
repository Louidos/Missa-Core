#!/usr/bin/env bash
# Richtet auf pve-id01 den Proxmox-Zugang für Missas eigene VMs ein (Pool „missa“).
# Louis führt das Script von Hand als root auf pve-id01 aus – Missa selbst nie.
#
#   bash proxmox-missa.sh --template 9000 --storage local-lvm --vlans 70 --dry-run
#   bash proxmox-missa.sh --template 9000 --storage local-lvm --vlans 70,80 [--bridge vmbr0]
#
# Was es einrichtet (und nicht mehr):
#   - Benutzer missa@pve ohne Passwort (gibt es nach proxmox-lesetoken.sh schon)
#   - Pool „missa“ und Rolle „MissaVMs“: VMs anlegen, klonen, konfigurieren, starten, stoppen,
#     löschen – aber nur auf /pool/missa, also nur für VMs in diesem Pool
#   - Klonen aus genau einem Template (PVETemplateUser auf /vms/<template>)
#   - Platz auf genau einem Storage (PVEDatastoreUser auf /storage/<storage>)
#   - Netz nur in den erlaubten VLANs (PVESDNUser auf /sdn/zones/localnetwork/<bridge>/<vlan>)
#   - Token missa@pve!vms mit eigener Rechte-Trennung (privsep) und genau diesen Rechten
# Keine Rechte auf andere VMs, keinen Root-Zugang, keine Konsole.
# Beschreibung: docs/proxmox-vms.md

set -Eeuo pipefail

BENUTZER="${PROXMOX_BENUTZER:-missa@pve}"
TOKEN="vms"
POOL="missa"
ROLLE="MissaVMs"
RECHTE="VM.Allocate VM.Clone VM.Audit VM.PowerMgmt VM.Config.CPU VM.Config.Memory VM.Config.Disk VM.Config.Network VM.Config.Cloudinit VM.Config.Options VM.Config.HWType VM.Config.CDROM Pool.Audit"
TEMPLATE=""
STORAGE=""
BRIDGE="vmbr0"
VLANS=""
DRY_RUN=0

abbruch() {
    echo "Fehler: $*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --template)
            TEMPLATE="${2:-}"
            shift
            ;;
        --storage)
            STORAGE="${2:-}"
            shift
            ;;
        --bridge)
            BRIDGE="${2:-}"
            shift
            ;;
        --vlans)
            VLANS="${2:-}"
            shift
            ;;
        --dry-run) DRY_RUN=1 ;;
        -h | --help)
            sed -n '2,/^$/p' "$0"
            exit 0
            ;;
        *) abbruch "Unbekannte Option: $1 (siehe --help)" ;;
    esac
    shift
done

[[ "$TEMPLATE" =~ ^[0-9]+$ ]] || abbruch "--template braucht die VM-ID des Cloud-Init-Templates"
[[ "$STORAGE" =~ ^[A-Za-z0-9._-]+$ ]] || abbruch "--storage braucht den Namen des Storage (z. B. local-lvm)"
[[ "$BRIDGE" =~ ^[A-Za-z0-9._-]+$ ]] || abbruch "--bridge ist ungültig"
[[ "$VLANS" =~ ^[0-9]+(,[0-9]+)*$ ]] || abbruch "--vlans braucht die erlaubten VLANs, z. B. 70 oder 70,80"

aendern() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'würde ausführen: %s\n' "$*"
    else
        "$@"
    fi
}

recht_geben() {
    local pfad="$1" rolle="$2"
    aendern pveum acl modify "$pfad" --roles "$rolle" --users "$BENUTZER" \
        --tokens "$BENUTZER!$TOKEN" --propagate 1
}

main() {
    command -v pveum >/dev/null 2>&1 || abbruch "pveum fehlt – das Script läuft nur auf dem Proxmox-Host."
    if [[ "$DRY_RUN" -eq 0 && "$EUID" -ne 0 ]]; then
        abbruch "Bitte als root ausführen (oder mit --dry-run nur anschauen)."
    fi

    if pveum user list --output-format json | grep -Eq "\"userid\": ?\"$BENUTZER\""; then
        echo "Benutzer $BENUTZER gibt es schon."
    else
        aendern pveum user add "$BENUTZER" --comment "Missa (eigener VM-Pool)"
    fi
    if pveum pool list --output-format json | grep -Eq "\"poolid\": ?\"$POOL\""; then
        echo "Pool $POOL gibt es schon."
    else
        aendern pveum pool add "$POOL" --comment "VMs, die Missa anlegt und verwaltet"
    fi
    if pveum role list --output-format json | grep -Eq "\"roleid\": ?\"$ROLLE\""; then
        aendern pveum role modify "$ROLLE" --privs "$RECHTE"
    else
        aendern pveum role add "$ROLLE" --privs "$RECHTE"
    fi

    if pveum user token list "$BENUTZER" --output-format json 2>/dev/null |
        grep -Eq "\"tokenid\": ?\"$TOKEN\""; then
        echo "Token $BENUTZER!$TOKEN gibt es schon."
    else
        echo "Neues Token $BENUTZER!$TOKEN – das Secret wird nur jetzt angezeigt:"
        aendern pveum user token add "$BENUTZER" "$TOKEN" --privsep 1 \
            --comment "Missa: VMs im Pool missa"
    fi

    recht_geben "/pool/$POOL" "$ROLLE"
    recht_geben "/vms/$TEMPLATE" "PVETemplateUser"
    recht_geben "/storage/$STORAGE" "PVEDatastoreUser"
    local vlan
    IFS=',' read -r -a vlan_liste <<<"$VLANS"
    for vlan in "${vlan_liste[@]}"; do
        recht_geben "/sdn/zones/localnetwork/$BRIDGE/$vlan" "PVESDNUser"
    done

    cat <<EOF

Fertig. In /etc/missa/missa.env auf ai01 eintragen:
  PROXMOX_VM_TOKEN_ID=$BENUTZER!$TOKEN
  PROXMOX_VM_TOKEN_SECRET=<das Secret von oben, Feld "value">
  VM_TEMPLATE_ID=$TEMPLATE
  VM_STORAGE=$STORAGE
  VM_BRIDGE=$BRIDGE
  VM_POOL=$POOL
  VM_NETZE=<VLAN>:<von-IP>-<bis-IP>/<Präfix>:<Gateway>  (für die VLANs $VLANS)
Danach: sudo systemctl restart missa-core
EOF
}

main
