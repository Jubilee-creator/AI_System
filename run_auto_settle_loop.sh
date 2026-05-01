#!/bin/bash
# run_auto_settle_loop.sh
# Runs auto_settle_trades.py --execute every 5 minutes with duplicate prevention.
# Writes a heartbeat file and timestamped log each run.
# Intended to be started from start_dashboard.sh (background) or a separate terminal.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/logs/auto_settle_loop.log"
LOCKFILE="$SCRIPT_DIR/data/auto_settle_loop.lock"
INTERVAL=300   # seconds between settle runs

mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/data"

# ── Duplicate prevention ───────────────────────────────────────────────────────
if [ -f "$LOCKFILE" ]; then
    existing_pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "[AUTO-SETTLE-LOOP] already running (PID $existing_pid) — exiting" | tee -a "$LOG"
        exit 0
    fi
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"; echo "[AUTO-SETTLE-LOOP] stopped (PID $$)" | tee -a "$LOG"' EXIT

echo "[AUTO-SETTLE-LOOP] started (PID $$) interval=${INTERVAL}s" | tee -a "$LOG"

# ── Main loop ─────────────────────────────────────────────────────────────────
while true; do
    ts=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
    echo "" >> "$LOG"
    echo "[$ts] [AUTO-SETTLE-LOOP] run start" >> "$LOG"

    python3 "$SCRIPT_DIR/auto_settle_trades.py" --execute >> "$LOG" 2>&1
    exit_code=$?

    ts_end=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
    if [ $exit_code -ne 0 ]; then
        echo "[$ts_end] [AUTO-SETTLE-LOOP] WARNING: settle exited $exit_code" | tee -a "$LOG"
    else
        echo "[$ts_end] [AUTO-SETTLE-LOOP] run complete" >> "$LOG"
    fi

    sleep $INTERVAL
done
