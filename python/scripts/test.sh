#!/usr/bin/env bash

set -Eeuo pipefail

readonly COMPOST_DIR="/home/adkuppa/compost"
readonly COMPOSE_FILE="${COMPOST_DIR}/docker-compose.yml"
readonly ENV_FILE="${COMPOST_DIR}/.env"
readonly PODMAN="/usr/bin/podman"
readonly PROJECT_NAME="compost"
readonly SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly LOG_DIR="${SCRIPT_ROOT}/.symphony/test-logs"
readonly LOCK_DIR="/home/adkuppa/.local/state/symphony"
readonly LOCK_FILE="${LOCK_DIR}/compost-runtime.lock"

usage() {
    cat <<'EOF'
Usage:
  ./scripts/test.sh <cpm|foyr2|pi> <workspace-or-repository-path>
  ./scripts/test.sh <cpm|foyr2|pi> <workspace-or-repository-path> -- <command> [args...]
  ./scripts/test.sh <cpm|foyr2|pi> <workspace-or-repository-path> --shell

Examples:
  ./scripts/test.sh cpm /home/adkuppa/codex-workspaces/ICPM-12345
  ./scripts/test.sh foyr2 /home/adkuppa/codex-workspaces/ICPM-12345 -- pytest /src/tests/test_example.py
  ./scripts/test.sh pi /home/adkuppa/codex-workspaces/ICPM-12345 --shell

The path may be either a Symphony workspace containing cpm/, foyr2/, and pi/
or the target repository itself. The script never edits Compost. It overrides
the source mount in the process environment, starts existing images, waits for
Compose health checks, verifies the mounted checkout, and then runs tests.

Default test commands:
  cpm    pytest Test/unit
  foyr2  pytest /src/tests -n 4 --tb=native
  pi     hatch run dev:test
EOF
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
fi

[[ $# -ge 2 ]] || {
    usage >&2
    exit 2
}

repository="$1"
source_input="$2"
shift 2

case "$repository" in
    cpm)
        service="cpm"
        repository_dir="cpm"
        source_variable="CPM_SRC"
        mount_target="/TexturaWD/textura"
        workdir="/TexturaWD/textura"
        dependencies=(oracledb19 memcached)
        recreate_services=(cpm)
        default_command=(pytest Test/unit)
        exec_environment=()
        ;;
    foyr|foyr2)
        repository="foyr2"
        service="foyr"
        repository_dir="foyr2"
        source_variable="FOYR_SRC"
        mount_target="/src"
        workdir="/src"
        dependencies=(oracledb19 memcached)
        recreate_services=(ibis foyr)
        default_command=(
            pytest /src/tests -n 4 --tb=native
            --junitxml=/tmp/symphony-foyr-pytest-results.xml
        )
        exec_environment=(-e FOYR_CONFIG_FILE=/src/tests/testing.yml)
        ;;
    pi)
        service="pi"
        repository_dir="pi"
        source_variable="PI_SRC"
        mount_target="/pi"
        workdir="/pi"
        dependencies=(oracledb23ai)
        recreate_services=(pi)
        default_command=(hatch run dev:test)
        exec_environment=()
        ;;
    *)
        fail "unsupported repository '$repository'; expected cpm, foyr2, or pi"
        ;;
esac

mode="test"
command=()
if [[ ${1:-} == "--shell" ]]; then
    [[ $# -eq 1 ]] || fail "--shell does not accept a test command"
    mode="shell"
elif [[ ${1:-} == "--" ]]; then
    shift
    [[ $# -gt 0 ]] || fail "-- must be followed by a command"
    command=("$@")
elif [[ $# -gt 0 ]]; then
    fail "unexpected argument '$1'; use -- before a custom test command"
else
    command=("${default_command[@]}")
fi

[[ -x "$PODMAN" ]] || fail "Podman is not executable at $PODMAN"
[[ -f "$COMPOSE_FILE" ]] || fail "Compose file does not exist: $COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || fail "Compost environment file does not exist: $ENV_FILE"
[[ -e "$source_input" ]] || fail "source path does not exist: $source_input"

source_input="$(realpath "$source_input")"
if [[ -d "${source_input}/${repository_dir}" ]]; then
    workspace_root="$source_input"
    source_path="${workspace_root}/${repository_dir}"
else
    workspace_root=""
    source_path="$source_input"
fi

[[ -d "$source_path" ]] || fail "repository path is not a directory: $source_path"
if ! git -C "$source_path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [[ -z "$(find "$source_path" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        fail "repository path is empty (stale workspace): $source_path"
    fi
    fail "repository path is not a Git worktree: $source_path"
fi
source_path="$(realpath "$source_path")"
branch_name="$(git -C "$source_path" symbolic-ref --quiet --short HEAD)" \
    || fail "repository checkout has a detached HEAD: $source_path"

if [[ -n "$workspace_root" ]]; then
    workspace_issue="$(basename "$workspace_root")"
    if [[ "$workspace_issue" =~ ^[A-Z][A-Z0-9]+-[0-9]+$ ]]; then
        expected_branch="feature/${workspace_issue}"
        [[ "$branch_name" == "$expected_branch" ]] || fail \
            "stale workspace branch '$branch_name'; expected '$expected_branch'"
    fi
fi

# Shell variables take precedence over Compost's .env without changing that file.
printf -v "$source_variable" '%s' "$source_path"
export "$source_variable"

# A Symphony workspace contains sibling repositories. Override each source that
# exists so recreated dependency containers cannot accidentally mount the normal
# development checkout while the target mounts the issue workspace.
if [[ -n "$workspace_root" ]]; then
    if [[ -d "${workspace_root}/cpm" ]]; then
        export CPM_SRC="$(realpath "${workspace_root}/cpm")"
    fi
    if [[ -d "${workspace_root}/foyr2" ]]; then
        export FOYR_SRC="$(realpath "${workspace_root}/foyr2")"
    fi
    if [[ -d "${workspace_root}/pi" ]]; then
        export PI_SRC="$(realpath "${workspace_root}/pi")"
    fi
fi

compose=(
    "$PODMAN" compose
    --project-name "$PROJECT_NAME"
    --project-directory "$COMPOST_DIR"
    --env-file "$ENV_FILE"
    -f "$COMPOSE_FILE"
)

mkdir -p "$LOG_DIR" "$LOCK_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_file="${LOG_DIR}/${repository}-${timestamp}-$$.log"

exec 9>"$LOCK_FILE"
flock -w 600 9 || fail "timed out waiting for the shared Compost runtime lock"

printf 'Repository: %s\nSource: %s\nBranch: %s\nService: %s\nStarted: %s\n\n' \
    "$repository" "$source_path" "$branch_name" "$service" "$timestamp" \
    >"$log_file"
printf 'Compost verification log: %s\n' "$log_file"

run_logged() {
    printf '\n$' | tee -a "$log_file"
    printf ' %q' "$@" | tee -a "$log_file"
    printf '\n' | tee -a "$log_file"

    set +e
    "$@" 2>&1 | tee -a "$log_file"
    status=${PIPESTATUS[0]}
    set -e
    return "$status"
}

collect_diagnostics() {
    printf '\nContainer diagnostics:\n' | tee -a "$log_file"
    "$PODMAN" ps --format \
        'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1 \
        | tee -a "$log_file" || true
    "${compose[@]}" logs --no-color --tail 200 \
        "${dependencies[@]}" "${recreate_services[@]}" 2>&1 \
        | tee -a "$log_file" || true
}

if ! run_logged "${compose[@]}" up -d --wait --wait-timeout 3600 \
    --pull never --no-recreate --no-build "${dependencies[@]}"; then
    collect_diagnostics
    fail "Compost dependencies failed to become ready; see $log_file"
fi

if ! run_logged "${compose[@]}" up -d --wait --wait-timeout 3600 \
    --pull never --force-recreate --no-build "${recreate_services[@]}"; then
    collect_diagnostics
    fail "Compost target service failed to become ready; see $log_file"
fi

if ! actual_source="$($PODMAN inspect "$service" --format '{{json .Mounts}}' \
    | python3 -c '
import json
import sys

target = sys.argv[1]
mounts = json.load(sys.stdin)
print(next((item["Source"] for item in mounts if item["Destination"] == target), ""))
' "$mount_target")"; then
    collect_diagnostics
    fail "could not inspect the $service source mount; see $log_file"
fi

[[ -n "$actual_source" ]] \
    || fail "container $service has no mount at $mount_target; see $log_file"
actual_source="$(realpath "$actual_source")"
[[ "$actual_source" == "$source_path" ]] || fail \
    "container $service mounts $actual_source at $mount_target, expected $source_path"

printf '\nVerified mount: %s -> %s\n' "$source_path" "$mount_target" \
    | tee -a "$log_file"

if [[ "$mode" == "shell" ]]; then
    printf 'Opening an interactive shell in %s. Shell output is not logged.\n' \
        "$service" | tee -a "$log_file"
    exec "${compose[@]}" exec --workdir "$workdir" "$service" bash
fi

if run_logged "${compose[@]}" exec -T "${exec_environment[@]}" \
    --workdir "$workdir" "$service" "${command[@]}"; then
    printf '\nPASS: %s tests completed successfully.\n' "$repository" \
        | tee -a "$log_file"
    exit 0
else
    status=$?
fi

collect_diagnostics
printf '\nFAIL: %s test command exited %d.\n' "$repository" "$status" \
    | tee -a "$log_file" >&2
exit "$status"
