#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${SYMPHONY_PYTHON:-$script_dir/python/.venv/bin/python}"
workflow="$script_dir/python/WORKFLOW.md"

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    printf 'Usage: %s [workflow-path] [OPTIONS]\n' "$0"
    printf 'Defaults: python/.venv/bin/python and python/WORKFLOW.md beside this launcher.\n'
    printf 'Set SYMPHONY_PYTHON to use another Python interpreter.\n'
    printf 'Polls Jira continuously. Press Ctrl-C to stop.\n'
    exit 0
fi

if [[ $# -gt 0 && $1 != -* ]]; then
    workflow="$1"
    shift
fi

if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf 'Python interpreter not found: %s\n' "$python_bin" >&2
    printf 'Create python/.venv and install the python/ package, or set SYMPHONY_PYTHON.\n' >&2
    exit 1
fi

export PYTHONPATH="$script_dir/python${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m symphony_jira run "$workflow" "$@"
