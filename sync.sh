#!/usr/bin/env bash
# Thin wrapper around resume-sync.py: single-instance lock, sane PATH, logging.
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

LOCKDIR="/tmp/resume-sync.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "[$(date '+%F %T')] already running - skipping"
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

SINCE_DAYS="${SINCE_DAYS:-21}"
python3 resume-sync.py --snapshot --reconcile --since-days "$SINCE_DAYS" >> resume-sync.log 2>&1