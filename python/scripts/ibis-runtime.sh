#!/usr/bin/env bash
set -Eeuo pipefail

# The cached image's entrypoint clears proxy variables. Restore only the host's
# runtime proxy settings for dependency preparation; never print their values.
export HTTP_PROXY="${SYMPHONY_HTTP_PROXY:-}" HTTPS_PROXY="${SYMPHONY_HTTPS_PROXY:-}"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
export NO_PROXY="${SYMPHONY_NO_PROXY:-}" no_proxy="${SYMPHONY_NO_PROXY:-}"

/virtualenv/ibis/bin/python /symphony-runtime/runtime-dependencies.py ibis

if [[ ! -f /TexturaWD/textura/locales/en/LC_MESSAGES/US.mo ]]; then
    make -C /TexturaWD/textura
fi
header_args=()
gunicorn_help="$(/virtualenv/ibis/bin/gunicorn --help)"
if [[ "$gunicorn_help" == *"--header-map"* ]]; then
    header_args=(--header-map dangerous)
fi
exec /virtualenv/ibis/bin/gunicorn -w 4 -k gthread --threads 8 --timeout 9999 \
    -b 0.0.0.0:5000 "${header_args[@]}" --access-logfile - --error-logfile - api.app:wsgi_cx_app
