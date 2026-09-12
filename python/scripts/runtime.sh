#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${1:-} == "down" && $# -eq 2 ]]; then
    mkdir -p /home/adkuppa/.local/state/symphony
    exec 9>/home/adkuppa/.local/state/symphony/compost-runtime.lock
    flock -w 600 9 || exit 125
    exec python3 "$SCRIPT_DIR/runtime-services.py" down --workspace "$2" \
        --state "$SCRIPT_DIR/../.symphony/runtime-services/services.json"
fi

if [[ ${1:-} == "--help" || $# -lt 3 || ${1:-} != "up" ]]; then
    printf 'Usage: %s up <issue-workspace> <cpm|foyr2|pi> [repositories...]\n       %s down <issue-workspace>\n' "$0" "$0"
    [[ ${1:-} == "--help" ]] && exit 0
    exit 2
fi
workspace="$2"
shift 2
for repository in "$@"; do
    "$SCRIPT_DIR/test.sh" "$repository" "$workspace" --prepare-only || exit 125
done
