#!/usr/bin/env bash
set -Eeuo pipefail
export HTTP_PROXY="${SYMPHONY_HTTP_PROXY:-}" HTTPS_PROXY="${SYMPHONY_HTTPS_PROXY:-}"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
export NO_PROXY="${SYMPHONY_NO_PROXY:-}" no_proxy="${SYMPHONY_NO_PROXY:-}"
export PIP_CONFIG_FILE=/src/deploy/pip.conf

/virtualenv/foyr/bin/python /symphony-runtime/runtime-dependencies.py foyr

exec /virtualenv/foyr/bin/gunicorn -k uvicorn.workers.UvicornWorker \
    -b 0.0.0.0:6666 --access-logfile - --error-logfile - manage:cx_app
