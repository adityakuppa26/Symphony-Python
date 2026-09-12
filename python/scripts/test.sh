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
  ./scripts/test.sh <cpm|foyr2|pi> <workspace-or-repository-path> --prepare-only
  ./scripts/test.sh <cpm|foyr2|pi> <workspace-or-repository-path> --test-only -- <command> [args...]

Examples:
  ./scripts/test.sh cpm /home/adkuppa/codex-workspaces/ICPM-12345
  ./scripts/test.sh foyr2 /home/adkuppa/codex-workspaces/ICPM-12345 -- pytest /src/tests/test_example.py
  ./scripts/test.sh pi /home/adkuppa/codex-workspaces/ICPM-12345 --shell

The path may be either a Symphony workspace containing cpm/, foyr2/, and pi/
or the target repository itself. The script never writes Compost .env. It overrides checkout variables only in
its process environment, starts existing images, waits for
Compose health checks, verifies the mounted checkout, and then runs tests.

Default test commands:
  cpm    pytest Test/unit
  foyr2  pytest /src/tests -n 1 --tb=native
  pi     hatch run dev:test
EOF
}

fail() {
    printf 'error: %s\n' "$*" >&2
    # Once argument parsing has chosen a mode, failures here are runtime setup,
    # health, branch, or mount failures. Keep them distinct from test exit codes.
    [[ -n ${mode:-} ]] && exit 125
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
        # Test/__init__.py otherwise inherits the functional-test database creation flag.
        exec_environment=(-e CREATE_NEW_DATABASE_WHEN_TESTING=False)
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
            pytest /src/tests -n 1 --tb=native
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
prepare_runtime=true
command=()
if [[ ${1:-} == "--prepare-only" ]]; then
    [[ $# -eq 1 ]] || fail "--prepare-only does not accept a test command"
    mode="prepare"
elif [[ ${1:-} == "--test-only" ]]; then
    prepare_runtime=false
    shift
    [[ ${1:-} == "--" ]] || fail "--test-only requires -- followed by a test command"
    shift
    [[ $# -gt 0 ]] || fail "--test-only requires a test command"
    command=("$@")
elif [[ ${1:-} == "--shell" ]]; then
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
export SYMPHONY_RUNTIME_SCRIPTS="${SCRIPT_ROOT}/scripts"
export SYMPHONY_RUNTIME_CACHE="${SCRIPT_ROOT}/.symphony/runtime-cache"
runtime_owner="${workspace_root:-$source_path}"
runtime_state="${SCRIPT_ROOT}/.symphony/runtime-services/services.json"
mkdir -p "$SYMPHONY_RUNTIME_CACHE"
compose+=(-f "${SCRIPT_ROOT}/scripts/runtime-compose.yml")

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

# Always remove only the disposable job we created, including on timeout.
run_container() (
    limit="$1"; shift
    job_name="symphony-runtime-$$-$RANDOM"
    trap '"$PODMAN" rm -f --ignore "$job_name" >/dev/null 2>&1 || true' EXIT
    trap 'exit 125' TERM INT
    if [[ -n ${SYMPHONY_HOST_BROWSER:-} ]]; then
        timeout --kill-after=10s "$limit" python3 "$SCRIPT_ROOT/scripts/frontend-host.py" \
            "$SYMPHONY_HOST_BROWSER" -- "$PODMAN" run --name "$job_name" --rm \
            --memory=2g --memory-swap=2g --cpus=2 --pids-limit=256 \
            --label symphony.runtime=job --label "symphony.workspace=${runtime_owner:-}" "$@"
    else
        timeout --kill-after=10s "$limit" "$PODMAN" run --name "$job_name" --rm \
            --memory=2g --memory-swap=2g --cpus=2 --pids-limit=256 \
            --label symphony.runtime=job --label "symphony.workspace=${runtime_owner:-}" "$@"
    fi
)

collect_diagnostics() {
    printf '\nContainer diagnostics:\n' | tee -a "$log_file"
    "$PODMAN" ps --format \
        'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1 \
        | tee -a "$log_file" || true
    "${compose[@]}" logs --no-color --tail 200 \
        "${dependencies[@]}" "${recreate_services[@]}" 2>&1 \
        | tee -a "$log_file" || true
}

runtime_snapshot=""
record_runtime() {
    if [[ -n "$runtime_snapshot" ]]; then
        if python3 "$SCRIPT_ROOT/scripts/runtime-services.py" finish --workspace "$runtime_owner" \
            --state "$runtime_state" --podman "$PODMAN" --snapshot "$runtime_snapshot"; then
            runtime_snapshot=""
            compose=("${compose[@]:0:${#compose[@]}-2}")
        else
            printf 'Runtime ownership recording failed; after-run cleanup will recover the labelled setup.\n' >&2
        fi
    fi
}
trap record_runtime EXIT
trap 'exit 125' TERM INT

if [[ "$repository" == "foyr2" && ${command[1]:-} == "/symphony-runtime/foyr-frontend-tests.sh" ]]; then
    # Fetch large external artifacts on the host; container networking may differ
    # from the host's VPN/proxy route. Reuse the local cache on later test rounds.
    browser_cache="${SCRIPT_ROOT}/.symphony/runtime-cache"
    mkdir -p "$browser_cache"
    browser_package="${browser_cache}/google-chrome.rpm"
    if [[ ! -s "$browser_package" ]]; then
        browser_download="$(mktemp "${browser_cache}/chrome.XXXXXX")"
        if ! run_logged curl -fL --retry 2 --connect-timeout 30 --max-time 300 \
            https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm \
            -o "$browser_download"; then
            fail "could not prepare the Chrome package on the host"
        fi
        mv "$browser_download" "$browser_package"
    fi
    frontend_image="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["foyr"]["image"])')"
    host_browser="$browser_cache/chrome-host/opt/google/chrome/chrome"
    if [[ ! -x "$host_browser" ]]; then
        mkdir -p "$browser_cache/chrome-host"
        (cd "$browser_cache/chrome-host" && rpm2cpio "$browser_package" | cpio -idm --quiet --no-absolute-filenames) \
            || fail "could not extract the cached browser package"
    fi
    "$host_browser" --version || fail "host browser libraries are unavailable"
    export SYMPHONY_HOST_BROWSER="$host_browser"
    export SYMPHONY_HTTP_PROXY="${HTTP_PROXY:-}" SYMPHONY_HTTPS_PROXY="${HTTPS_PROXY:-}"
    export SYMPHONY_NO_PROXY="${NO_PROXY:-}"
    # Browser tests use a disposable instance of the same image. Host networking
    # is limited to this test job; the application service keeps its Compose network.
    if run_logged run_container 1800s --pull=never --network host \
        --entrypoint /bin/bash -e SYMPHONY_HTTP_PROXY -e SYMPHONY_HTTPS_PROXY -e SYMPHONY_NO_PROXY \
        -v "$source_path:/src:ro" -v "$SYMPHONY_RUNTIME_SCRIPTS:/symphony-runtime:ro" \
        -v "$SYMPHONY_RUNTIME_CACHE:/symphony-cache" \
        "$frontend_image" "${command[@]:1}"; then
        exit 0
    else
        frontend_status=$?
        [[ "$frontend_status" == 124 || "$frontend_status" == 137 ]] && exit 125
        exit "$frontend_status"
    fi
fi

if [[ "$prepare_runtime" == true ]]; then
runtime_snapshot="$("${compose[@]}" config --format json | python3 "$SCRIPT_ROOT/scripts/runtime-services.py" begin \
    --workspace "$runtime_owner" --state "$runtime_state" --podman "$PODMAN" \
    --services "${dependencies[@]}" "${recreate_services[@]}")" || fail "could not capture test runtime ownership"
compose+=(-f "$runtime_snapshot.override")
if [[ "$repository" == "foyr2" ]]; then
    for runtime_service in ibis foyr; do
        runtime_image="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$runtime_service")"
        if [[ "$runtime_service" == "ibis" ]]; then runtime_mount=/ibis; else runtime_mount=/src; fi
        runtime_source="$("${compose[@]}" config --format json | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"][sys.argv[1]]; print(next(v["source"] for v in s["volumes"] if v.get("target")==sys.argv[2]))' "$runtime_service" "$runtime_mount")"
        run_logged run_container 600s --pull=never --network host \
            --entrypoint "/virtualenv/$runtime_service/bin/python" \
            -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \
            -e PIP_CONFIG_FILE="$runtime_mount/deploy/pip.conf" \
            -v "$runtime_source:$runtime_mount:ro" \
            -v "$SYMPHONY_RUNTIME_SCRIPTS:/symphony-runtime:ro" \
            -v "$SYMPHONY_RUNTIME_CACHE:/symphony-cache" \
            "$runtime_image" /symphony-runtime/runtime-dependencies.py "$runtime_service" --prepare \
            || fail "could not cache $runtime_service runtime dependencies"
    done
fi
restart_services=()
for candidate in "${recreate_services[@]}"; do
    if [[ "$("$PODMAN" inspect "$candidate" --format '{{.State.Running}}' 2>/dev/null || true)" == "true" ]]; then
        restart_services+=("$candidate")
    fi
done
if ! run_logged "${compose[@]}" up -d --wait --wait-timeout 3600 \
    --pull never --no-recreate --no-build "${dependencies[@]}"; then
    collect_diagnostics
    fail "Compost dependencies failed to become ready; see $log_file"
fi

if ! run_logged "${compose[@]}" up -d --wait --wait-timeout 3600 \
    --pull never --no-build "${recreate_services[@]}"; then
    collect_diagnostics
    fail "Compost target service failed to become ready; see $log_file"
fi
# Retain prepared container dependencies, but reload application code for each
# verification round. Compose recreates containers itself when source mounts change.
if [[ ${#restart_services[@]} -gt 0 ]]; then
    run_logged "${compose[@]}" restart "${restart_services[@]}" \
        || fail "could not reload the changed application services"
    run_logged "${compose[@]}" up -d --wait --wait-timeout 3600 \
        --pull never --no-recreate --no-build "${recreate_services[@]}" \
        || fail "reloaded services are not healthy"
fi
fi

record_runtime

"$PODMAN" inspect "$service" --format '{{json .State}}' | python3 -c '
import json, sys
state = json.load(sys.stdin)
health = (state.get("Health") or state.get("Healthcheck") or {}).get("Status")
sys.exit(0 if state.get("Running") and health in (None, "", "healthy") else 1)
' || fail "test service $service is not healthy; prepare the runtime first"

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

if [[ "$repository" == "foyr2" && -n "$workspace_root" ]]; then
    # Foyr calls iBIS: validate the dependency's CPM checkout as well as /src.
    ibis_source="$($PODMAN inspect ibis --format '{{json .Mounts}}' | python3 -c '
import json, sys
print(next((m["Source"] for m in json.load(sys.stdin) if m["Destination"] == "/ibis"), ""))
')"
    [[ -n "$ibis_source" && "$(realpath "$ibis_source")" == "$CPM_SRC" ]] \
        || fail "ibis is not mounted to this issue workspace's cpm checkout"
fi

if [[ "$mode" == "prepare" ]]; then
    printf '\nREADY: %s test runtime uses %s\n' "$repository" "$source_path" | tee -a "$log_file"
    exit 0
fi

if [[ "$mode" == "shell" ]]; then
    printf 'Opening an interactive shell in %s. Shell output is not logged.\n' \
        "$service" | tee -a "$log_file"
    exec "${compose[@]}" exec "${exec_environment[@]}" --workdir "$workdir" "$service" bash
fi


test_timeout="${SYMPHONY_TEST_TIMEOUT_SECONDS:-1200}"
if [[ -n ${SYMPHONY_VERIFICATION_DEADLINE_EPOCH:-} ]]; then
    test_timeout=$((SYMPHONY_VERIFICATION_DEADLINE_EPOCH - $(date +%s) - 15))
fi
[[ "$test_timeout" =~ ^[0-9]+$ && "$test_timeout" -gt 0 ]] || fail "verification time budget exhausted before tests"
runtime_execution="$(python3 "$SCRIPT_ROOT/scripts/runtime-services.py" exec-begin --workspace "$runtime_owner" \
    --state "$runtime_state" --podman "$PODMAN" --services "$service")" || fail "could not register the test runner"
read -r runtime_exec_token runtime_exec_container <<< "$runtime_execution"
# Bind execution to the inspected container ID and tag its processes for cleanup.
# Timeout runs inside the container so it also stops pytest when the host caller exits.
if run_logged "$PODMAN" exec "${exec_environment[@]}" -e "SYMPHONY_TEST_TOKEN=$runtime_exec_token" \
    --workdir "$workdir" "$runtime_exec_container" timeout --kill-after=5s "${test_timeout}s" "${command[@]}"; then
    printf '\nPASS: %s tests completed successfully.\n' "$repository" \
        | tee -a "$log_file"
    exit 0
else
    status=$?
fi

[[ "$status" == 124 || "$status" == 137 ]] && status=125
collect_diagnostics
printf '\nFAIL: %s test command exited %d.\n' "$repository" "$status" \
    | tee -a "$log_file" >&2
exit "$status"
