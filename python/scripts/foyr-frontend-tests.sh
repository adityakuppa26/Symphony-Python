#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# -eq 1 ]] || exit 125
selector="$1"
export HTTP_PROXY="${SYMPHONY_HTTP_PROXY:-}" HTTPS_PROXY="${SYMPHONY_HTTPS_PROXY:-}"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
# The lockfiles use the corporate mirror, which is reachable directly over VPN
# but times out through the inherited external HTTP proxy.
export NO_PROXY="${SYMPHONY_NO_PROXY:+$SYMPHONY_NO_PROXY,}artifacthub-phx.oci.oraclecorp.com,127.0.0.1,localhost"
export no_proxy="$NO_PROXY"
export npm_config_noproxy="$NO_PROXY"

# Build outputs and translation generation belong in container scratch space,
# never in the exact implementation snapshot that Symphony is verifying.
/virtualenv/foyr/bin/python - <<'PY'
from pathlib import Path
import shutil

source = Path('/src')
target = Path('/tmp/symphony-frontend-build')
if target.is_symlink():
    target.unlink()
elif target.exists():
    shutil.rmtree(target)
def ignored(directory, names):
    result = {name for name in names if name in {'.git', '.symphony', 'node_modules', '__pycache__'}}
    if Path(directory) == source / 'foyr':
        result.update({'web', 'test_report', 'coverage_report'} & set(names))
    return result
shutil.copytree(source, target, ignore=ignored, symlinks=True)
PY

cd /tmp/symphony-frontend-build
export PYTHONPATH="$PWD"
export npm_config_cache=/symphony-cache/npm
export npm_config_prefer_offline=true
export PUPPETEER_SKIP_DOWNLOAD=true PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export NODE_OPTIONS=--max-old-space-size=1280
export npm_config_audit=false npm_config_fund=false
for project in . foyr; do
    [[ -f "$project/package-lock.json" ]] || { echo "Missing frontend lockfile: $project"; exit 125; }
    # The selected Karma suite uses host Chrome. Other test tools' install hooks
    # download obsolete browsers; build the selected suite explicitly below.
    timeout --kill-after=10s 600s npm --prefix "$project" ci --ignore-scripts --no-audit --no-fund || exit 125
done
node node_modules/grunt-cli/bin/grunt default_hot
KARMA_TEST="$selector" \
    node node_modules/karma/bin/karma start --single-run --auto-watch=false --browsers=
