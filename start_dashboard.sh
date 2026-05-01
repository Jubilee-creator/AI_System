#!/bin/bash
# start_dashboard.sh — Starts the Dashboard and the auto-settle background loop.
# Stop both by closing this terminal or pressing Ctrl-C.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Auto-settle background loop ────────────────────────────────────────────────
LOCKFILE="$SCRIPT_DIR/data/auto_settle_loop.lock"

if [ -f "$LOCKFILE" ] && kill -0 "$(cat "$LOCKFILE" 2>/dev/null)" 2>/dev/null; then
    echo "[start_dashboard] auto-settle loop already running (PID $(cat "$LOCKFILE"))"
else
    echo "[start_dashboard] starting auto-settle loop in background..."
    bash "$SCRIPT_DIR/run_auto_settle_loop.sh" &
    SETTLE_PID=$!
    echo "[start_dashboard] auto-settle loop started (PID $SETTLE_PID)"
fi

# Kill the settle loop when this script exits (only if we started it here)
cleanup() {
    if [ -n "${SETTLE_PID:-}" ] && kill -0 "$SETTLE_PID" 2>/dev/null; then
        echo "[start_dashboard] stopping auto-settle loop (PID $SETTLE_PID)..."
        kill "$SETTLE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ── Dashboard ─────────────────────────────────────────────────────────────────
echo "[start_dashboard] starting Dashboard..."
python3 "$SCRIPT_DIR/Dashboard.py"
