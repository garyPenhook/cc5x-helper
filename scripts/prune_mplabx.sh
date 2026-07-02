#!/bin/sh
# Prune MPLAB X down to only what cc5x-helper's ipecmd programming path needs.
# Reviewed against tools/cc5x_setcc_native.py's discover_ipecmd()/discover_pack_roots()
# and verified against the actual .PIC device files for every part this repo targets.
#
# Deletes ~5.25GB of full-IDE/unused-pack content in place, keeping ipecmd.sh's
# hardcoded absolute JRE path (sys/java/...) intact. Requires sudo. Irreversible
# short of reinstalling MPLAB X.
#
# Usage: sudo sh scripts/prune_mplabx.sh [/opt/microchip/mplabx/v6.30]
#
# EDIT THESE on a new machine before running:
#   - DFP_KEEP: pack families for the devices you actually build for. Defaults
#     to this repo's supported scope (PIC10F/PIC12F/PIC16F). Extend it if you
#     add a device outside that scope, or the prune deletes metadata you need.
#   - TP_KEEP: the tool pack(s) matching the programmer(s) you physically own
#     (see `ls "$ROOT/packs/Microchip" | grep _TP` on this machine for names,
#     e.g. Snap_TP, ICD4_TP, ICD5_TP, PKOB4_TP).

set -eu

ROOT="${1:-/opt/microchip/mplabx/v6.30}"

DFP_KEEP="PIC10-12Fxxx_DFP PIC12-16F1xxx_DFP PIC16F1xxxx_DFP PIC16Fxxx_DFP"
TP_KEEP="PICkit4_TP PICkit5_TP"

if [ ! -d "$ROOT/mplab_platform/mplab_ipe" ]; then
    echo "error: $ROOT doesn't look like an MPLAB X version dir (no mplab_platform/mplab_ipe)" >&2
    exit 1
fi

prune_dir() {
    # prune_dir <dir> <keep...>
    dir="$1"; shift
    keep_list=" $* "
    for entry in "$dir"/*; do
        name="$(basename "$entry")"
        case "$keep_list" in
            *" $name "*) ;;  # keep
            *)
                echo "rm -rf $entry"
                rm -rf "$entry"
                ;;
        esac
    done
}

echo "== pruning top level =="
prune_dir "$ROOT" mplab_platform packs sys

echo "== pruning mplab_platform (keep ipecmd + its launcher deps) =="
prune_dir "$ROOT/mplab_platform" \
    bin mplab_ipe dat etc lib nb mplab-packmanagerui mplab-content-manager mcc-update-manager

echo "== pruning packs/ (drop ARM packs entirely, PIC-only repo) =="
prune_dir "$ROOT/packs" Microchip index.idx

echo "== pruning packs/Microchip (keep only PIC10/12/16 DFPs + owned programmers) =="
prune_dir "$ROOT/packs/Microchip" $DFP_KEEP $TP_KEEP

echo "== done =="
du -sh "$ROOT"
echo
echo "Verify ipecmd still runs:"
echo "  $ROOT/mplab_platform/mplab_ipe/ipecmd.sh -P PIC16F1509 -TPPK4 2>&1 | head -20"
