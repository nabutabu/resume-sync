#!/usr/bin/env bash
# Thin wrapper around resume-sync.py: single-instance lock, sane PATH, logging.
#
# Optional OpenTelemetry observability: if the OTel packages are installed and
# OTEL_CONFIG_FILE points at otel-config.yaml, the script exports traces,
# metrics, and logs to a local OpenTelemetry Collector (see docker-compose.yml).
#
# One-time setup:
#   python3 -m venv .venv
#   ./.venv/bin/pip install -r requirements.txt
#   docker compose up -d          # start the local OTel collector
#
# If you don't want OTel, just don't set OTEL_CONFIG_FILE below / don't install
# the dependencies -- the script runs exactly as before.
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

LOCKDIR="/tmp/resume-sync.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "[$(date '+%F %T')] already running - skipping"
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

# Prefer a local virtualenv if present (keeps system Python clean).
if [ -x "./.venv/bin/python3" ]; then
    PYTHON="./.venv/bin/python3"
else
    PYTHON="python3"
fi

# --- OpenTelemetry --------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export OTEL_CONFIG_FILE="${OTEL_CONFIG_FILE:-$SCRIPT_DIR/otel-config.yaml}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"
# --------------------------------------------------------------------------

SINCE_DAYS="${SINCE_DAYS:-21}"
"$PYTHON" resume-sync.py --snapshot --reconcile --since-days "$SINCE_DAYS" >> resume-sync.log 2>&1
