#!/bin/bash
# ============================================================================
# Printix TonerWatch — container entrypoint
# ============================================================================
# On first start:
#   - generate the Fernet key used to encrypt per-customer BI-DB credentials
#   - ensure /data is writable
# On every start:
#   - export APP_VERSION and FERNET_KEY into the process env
#   - launch uvicorn against the FastAPI application
# ============================================================================

set -euo pipefail

log_info()  { printf '[%s] [INFO]  %s\n'  "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
log_error() { printf '[%s] [ERROR] %s\n'  "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

APP_VERSION="$(cat /app/VERSION 2>/dev/null || echo '0.0.0')"
export APP_VERSION
log_info "Starting Printix TonerWatch v${APP_VERSION}"

if [ ! -w /data ]; then
    log_error "/data is not writable — mount a persistent volume (chown 1000:1000)."
    exit 1
fi

if [ ! -f /data/fernet.key ]; then
    log_info "First start — generating Fernet key for encrypted credential storage."
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > /data/fernet.key
    chmod 600 /data/fernet.key
fi
export FERNET_KEY
FERNET_KEY="$(cat /data/fernet.key)"

export WEB_HOST="${WEB_HOST:-0.0.0.0}"
export WEB_PORT="${WEB_PORT:-8080}"
export DB_PATH="${DB_PATH:-/data/tonerwatch.sqlite}"

log_info "Listening on ${WEB_HOST}:${WEB_PORT}, database ${DB_PATH}"

exec python3 -m uvicorn src.server:app \
    --host "${WEB_HOST}" \
    --port "${WEB_PORT}" \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --no-server-header
