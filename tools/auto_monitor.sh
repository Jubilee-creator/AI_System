#!/bin/bash

PROJECT="/Users/samuel/Desktop/AI_System"
cd "$PROJECT" || exit 1

mkdir -p logs

echo "" >> logs/auto_monitor.log
echo "==================================================" >> logs/auto_monitor.log
echo "[AUTO_MONITOR] $(date)" >> logs/auto_monitor.log
echo "==================================================" >> logs/auto_monitor.log

echo "[1/3] Auto-settling trades..." >> logs/auto_monitor.log
/usr/bin/python3 auto_settle_trades.py --execute >> logs/auto_monitor.log 2>&1

echo "" >> logs/auto_monitor.log
echo "[2/3] Performance report..." >> logs/auto_monitor.log
/usr/bin/python3 tools/performance_report.py >> logs/auto_monitor.log 2>&1

if [ -f "$PROJECT/.env.monitor" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT/.env.monitor"
  set +a
fi

echo "" >> logs/auto_monitor.log
echo "[3/3] Risk snapshot..." >> logs/auto_monitor.log
/usr/bin/python3 - <<'PY' >> logs/auto_monitor.log 2>&1
import json, os, subprocess
from pathlib import Path

state_path = Path("data/risk_state.json")
if not state_path.exists():
    print("[WARN] risk_state.json not found")
    raise SystemExit

s = json.loads(state_path.read_text())

daily = float(s.get("daily_pnl", 0))
exposure = float(s.get("total_exposure", s.get("open_risk_exposure", 0)))
limit = float(s.get("daily_loss_limit", -50)) if "daily_loss_limit" in s else -50.0
effective = daily - exposure
room = abs(limit) - abs(effective)
open_pos = int(s.get("open_positions", 0))
kill = bool(s.get("kill_switch_active", False))
loss_streak = int(s.get("loss_streak", 0))

print(f"daily_pnl={daily:.2f}")
print(f"open_exposure={exposure:.2f}")
print(f"effective_risk={effective:.2f}")
print(f"loss_limit={limit:.2f}")
print(f"risk_room={room:.2f}")
print(f"open_positions={open_pos}")
print(f"loss_streak={loss_streak}")
print(f"kill_switch={kill}")

status = "NORMAL"
msg = f"AI_System: Normal. P&L ${daily:.2f}, exposure ${exposure:.2f}, room ${room:.2f}"

if kill:
    status = "KILL SWITCH"
    msg = "AI_System: KILL SWITCH ACTIVE"
elif effective <= limit:
    status = "HARD STOP"
    msg = f"AI_System: HARD STOP. Effective risk ${effective:.2f}"
elif room <= 10:
    status = "NEAR LIMIT"
    msg = f"AI_System: Near limit. Risk room ${room:.2f}"

print(f"system_status={status}")

# Pushover phone alert
try:
    should_push = status in {"HARD STOP", "KILL SWITCH"} or room <= 10
    user_key = os.environ.get("PUSHOVER_USER_KEY", "")
    api_token = os.environ.get("PUSHOVER_API_TOKEN", "")

    if should_push:
        if user_key and api_token:
            result = subprocess.run([
                "curl",
                "-sS",
                "--fail",
                "https://api.pushover.net/1/messages.json",
                "-F", f"token={api_token}",
                "-F", f"user={user_key}",
                "-F", f"title=AI_System Monitor",
                "-F", f"message={msg}",
            ], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                print("[PUSHOVER] alert sent")
            else:
                err = (result.stderr or result.stdout or "").strip()
                print(f"[WARN] pushover failed: {err}")
        else:
            print("[PUSHOVER] skipped: PUSHOVER_USER_KEY/PUSHOVER_API_TOKEN not set")
    else:
        print("[PUSHOVER] skipped: status below alert threshold")
except Exception as e:
    print(f"[WARN] pushover notification failed: {e}")

# macOS notification
try:
    title = "AI_System Monitor"
    script = f'''
ObjC.import("Cocoa");
var notification = $.NSUserNotification.alloc.init;
notification.setTitle({json.dumps(title)});
notification.setInformativeText({json.dumps(msg)});
$.NSUserNotificationCenter.defaultUserNotificationCenter.deliverNotification(notification);
'''
    subprocess.run([
        "osascript",
        "-l", "JavaScript",
        "-e", script,
    ], check=False)
except Exception as e:
    print(f"[WARN] notification failed: {e}")
PY

echo "[AUTO_MONITOR] Done." >> logs/auto_monitor.log
