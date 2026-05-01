#!/usr/bin/env python3
"""
tools/report_health.py
----------------------
Read-only operational health check for the AI_System paper trading system.

Checks:
  - Auto-settle loop status (running / last run / heartbeat age)
  - Open trades count, overdue trades, exposure
  - Risk state (kill switch, cooldown, daily P&L vs limit)
  - Dashboard process status
  - Kill switch and cooldown status
  - BTC snapshot log freshness

Does not trade, modify state, or block any gate.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (
    DATA_COLLECTION_MODE,
    GLOBAL_FORCED_LEARNING_MODE,
    MIN_CONFIDENCE,
)
from tools.clean_truth_report import classify_records, evaluate_proof_gates
from tools.performance_report import load_trades

HEARTBEAT_FILE = ROOT / "data" / "auto_settle_last_run.json"
LOCK_FILE = ROOT / "data" / "auto_settle_loop.lock"
SETTLE_LOG = ROOT / "logs" / "auto_settle_loop.log"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
BTC_SNAPSHOT_LOG = ROOT / "logs" / "btc_15m_entry_window_snapshots.jsonl"
MAX_TRADE_DURATION_SECONDS = 7200  # matches auto_settle_trades.py


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_str(ts: Optional[datetime]) -> str:
    if ts is None:
        return "unknown"
    secs = (_now() - ts).total_seconds()
    if secs < 0:
        return "future?"
    if secs < 60:
        return f"{secs:.0f}s ago"
    if secs < 3600:
        return f"{secs/60:.1f}m ago"
    return f"{secs/3600:.1f}h ago"


def _pid_alive(pid_str: str) -> bool:
    try:
        pid = int(pid_str.strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def check_settle_loop() -> dict:
    """Return status of the auto-settle background loop."""
    running = False
    loop_pid = None

    if LOCK_FILE.exists():
        raw = LOCK_FILE.read_text().strip()
        if _pid_alive(raw):
            running = True
            loop_pid = raw

    heartbeat: dict = {}
    heartbeat_age: Optional[float] = None
    if HEARTBEAT_FILE.exists():
        try:
            heartbeat = json.loads(HEARTBEAT_FILE.read_text())
            hb_ts = _parse_ts(heartbeat.get("timestamp_utc"))
            if hb_ts:
                heartbeat_age = (_now() - hb_ts).total_seconds()
        except Exception:
            pass

    last_log_line = ""
    if SETTLE_LOG.exists():
        try:
            lines = SETTLE_LOG.read_text().splitlines()
            for line in reversed(lines):
                if line.strip():
                    last_log_line = line.strip()
                    break
        except Exception:
            pass

    return {
        "running": running,
        "loop_pid": loop_pid,
        "heartbeat": heartbeat,
        "heartbeat_age_seconds": heartbeat_age,
        "last_log_line": last_log_line,
    }


def check_open_trades() -> dict:
    """Parse paper_trades.jsonl and return open trade stats."""
    if not TRADES_LOG.exists():
        return {"open_count": 0, "overdue_count": 0, "exposure_dollars": 0.0, "trades": []}

    by_ticker: dict[str, dict] = {}
    try:
        for line in TRADES_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticker = row.get("ticker", "?")
            status = str(row.get("status", "")).upper()
            if status in ("OPEN",):
                by_ticker[ticker] = row
            elif status in ("SETTLED", "FORCED_CLOSE", "CLOSED"):
                by_ticker.pop(ticker, None)
    except Exception:
        pass

    now = _now()
    open_trades = list(by_ticker.values())
    overdue = []
    exposure = 0.0

    for t in open_trades:
        size = float(t.get("size", 0.0))
        exposure += size
        entry_ts = _parse_ts(t.get("timestamp_utc") or t.get("entry_time"))
        if entry_ts:
            age = (now - entry_ts).total_seconds()
            if age > MAX_TRADE_DURATION_SECONDS:
                overdue.append({
                    "ticker": t.get("ticker"),
                    "age_minutes": round(age / 60, 1),
                    "size": size,
                })

    return {
        "open_count": len(open_trades),
        "overdue_count": len(overdue),
        "exposure_dollars": round(exposure, 2),
        "overdue_trades": overdue,
    }


def check_risk_state() -> dict:
    """Load risk_state.json and return key fields."""
    state_path = ROOT / "data" / "risk_state.json"
    if not state_path.exists():
        return {"loaded": False}
    try:
        state = json.loads(state_path.read_text())
        cooldown_until_ts = _parse_ts(state.get("cooldown_until"))
        cooldown_active = (
            cooldown_until_ts is not None
            and cooldown_until_ts > _now()
        )
        return {
            "loaded": True,
            "kill_switch_active": state.get("kill_switch_active", False),
            "cooldown_active": cooldown_active,
            "cooldown_until": state.get("cooldown_until"),
            "cooldown_reason": state.get("cooldown_reason", ""),
            "daily_pnl": state.get("daily_pnl"),
            "total_pnl": state.get("total_pnl"),
            "total_exposure": state.get("total_exposure"),
            "loss_streak": state.get("loss_streak"),
        }
    except Exception as exc:
        return {"loaded": False, "error": str(exc)}


def check_dashboard_process() -> dict:
    """Check if Dashboard.py is in the process list."""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "Dashboard.py"],
            capture_output=True, text=True
        )
        pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
        return {"running": bool(pids), "pids": pids}
    except Exception as exc:
        return {"running": False, "error": str(exc)}


def check_btc_snapshots() -> dict:
    """Report age of the most recent BTC15M snapshot."""
    if not BTC_SNAPSHOT_LOG.exists():
        return {"last_snapshot_ts": None, "age_seconds": None}
    try:
        last_ts = None
        for line in BTC_SNAPSHOT_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                ts = _parse_ts(row.get("timestamp_utc"))
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
            except Exception:
                pass
        age = (_now() - last_ts).total_seconds() if last_ts else None
        return {"last_snapshot_ts": last_ts.isoformat() if last_ts else None, "age_seconds": age}
    except Exception as exc:
        return {"last_snapshot_ts": None, "error": str(exc)}


def check_research_truth() -> dict:
    """Return compact proof/mode flags for the operator truth summary."""
    try:
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    except Exception as exc:
        return {"loaded": False, "error": str(exc)}

    health = gate.get("edge_profile_health") or {}
    return {
        "loaded": True,
        "system_mode": "RESEARCH_ONLY",
        "proof_verdict": gate.get("verdict", "UNKNOWN"),
        "proof_reason": gate.get("reason", ""),
        "scale_allowed": bool(gate.get("scale_allowed")),
        "real_money_allowed": bool(gate.get("real_money_allowed")),
        "data_collection_mode": DATA_COLLECTION_MODE,
        "global_forced_learning_mode": GLOBAL_FORCED_LEARNING_MODE,
        "kelly_sizing_used": not GLOBAL_FORCED_LEARNING_MODE,
        "min_confidence": MIN_CONFIDENCE,
        "edge_profile_trusted": health.get("edge_profile_trusted", False),
        "edge_profile_reason": health.get("reason", ""),
        "clean_settled_count": gate.get("clean_settled_count", 0),
        "modern_full_count": gate.get("modern_full_count", 0),
        "normal_modern_count": gate.get("normal_modern_count", 0),
        "data_collection_override_count": gate.get("dc_count", 0),
        "bootstrap_provisional_count": gate.get("bootstrap_count", 0),
        "time_exit_excluded_count": gate.get("time_exit_excluded_count", 0),
    }


def _status_line(ok: bool, label: str, detail: str) -> str:
    mark = "OK  " if ok else "WARN"
    return f"  [{mark}] {label:<30} {detail}"


def main() -> None:
    settle = check_settle_loop()
    trades = check_open_trades()
    risk = check_risk_state()
    dashboard = check_dashboard_process()
    btc = check_btc_snapshots()
    truth = check_research_truth()
    now = _now()

    print("=" * 72)
    print("AI_SYSTEM HEALTH CHECK")
    print(f"  as_of: {now.isoformat()}")
    print("=" * 72)

    # ── Research / proof truth summary ───────────────────────────────────────
    print("\nSYSTEM TRUTH SUMMARY")
    print("-" * 72)
    if not truth.get("loaded"):
        print(_status_line(False, "truth_summary", truth.get("error", "unavailable")))
    else:
        print(_status_line(False, "system_mode", truth["system_mode"]))
        print(_status_line(False, "proof_verdict", truth["proof_verdict"]))
        print(_status_line(bool(truth["edge_profile_trusted"]), "edge_profile_trusted", str(truth["edge_profile_trusted"])))
        print(_status_line(not truth["kelly_sizing_used"], "kelly_execution", "DISABLED" if not truth["kelly_sizing_used"] else "ENABLED"))
        print(_status_line(not truth["data_collection_mode"], "data_collection_mode", str(truth["data_collection_mode"])))
        print(_status_line(True, "min_confidence", f"{truth['min_confidence']:.2f}"))
        print(_status_line(not truth["scale_allowed"], "scale_allowed", "NO" if not truth["scale_allowed"] else "YES"))
        print(_status_line(not truth["real_money_allowed"], "real_money_allowed", "NO" if not truth["real_money_allowed"] else "YES"))
        print(
            "    samples: "
            f"clean_settled={truth['clean_settled_count']}  "
            f"modern_full={truth['modern_full_count']}  "
            f"normal_modern={truth['normal_modern_count']}  "
            f"data_collection={truth['data_collection_override_count']}  "
            f"bootstrap={truth['bootstrap_provisional_count']}  "
            f"time_exit_excluded={truth['time_exit_excluded_count']}"
        )
        reason = str(truth["proof_reason"])
        print(f"    reason: {reason[:120]}{'...' if len(reason) > 120 else ''}")

    # ── Auto-settle loop ─────────────────────────────────────────────────────
    print("\nAUTO-SETTLE LOOP")
    print("-" * 72)
    settle_ok = settle["running"]
    print(_status_line(settle_ok, "loop_running", str(settle["running"]) + (f" (PID {settle['loop_pid']})" if settle["loop_pid"] else "")))

    hb = settle["heartbeat"]
    hb_age = settle["heartbeat_age_seconds"]
    if hb:
        hb_stale = hb_age is not None and hb_age > 900  # >15min is suspicious
        print(_status_line(not hb_stale, "last_heartbeat_age", _age_str(_parse_ts(hb.get("timestamp_utc")))))
        print(f"    mode={hb.get('mode')}  checked={hb.get('checked')}  settled={hb.get('settled')}  forced={hb.get('forced_closed')}  errors={hb.get('errors')}")
        print(f"    pnl_delta=${hb.get('pnl_delta_dollars', 0):+.2f}  remaining_open={hb.get('remaining_open')}")
    else:
        print(_status_line(False, "heartbeat_file", "NOT FOUND — no execute run yet"))

    if settle["last_log_line"]:
        print(f"    last_log: {settle['last_log_line'][:72]}")

    # ── Dashboard ────────────────────────────────────────────────────────────
    print("\nDASHBOARD PROCESS")
    print("-" * 72)
    dash_ok = dashboard["running"]
    dash_detail = f"pids={dashboard.get('pids', [])}" if dash_ok else "NOT RUNNING"
    print(_status_line(dash_ok, "Dashboard.py", dash_detail))

    # ── Open trades ──────────────────────────────────────────────────────────
    print("\nOPEN TRADES")
    print("-" * 72)
    overdue_ok = trades["overdue_count"] == 0
    print(_status_line(True, "open_count", str(trades["open_count"])))
    print(_status_line(overdue_ok, "overdue_trades", str(trades["overdue_count"]) + (" (NEED SETTLE)" if not overdue_ok else "")))
    print(_status_line(True, "open_exposure", f"${trades['exposure_dollars']:.2f}"))
    for ot in trades.get("overdue_trades", []):
        print(f"    OVERDUE: {ot['ticker']}  age={ot['age_minutes']}min  size=${ot['size']:.2f}")

    # ── Risk state ───────────────────────────────────────────────────────────
    print("\nRISK STATE")
    print("-" * 72)
    if not risk["loaded"]:
        print(_status_line(False, "risk_state.json", risk.get("error", "not found")))
    else:
        ks = risk.get("kill_switch_active", False)
        cd = risk.get("cooldown_active", False)
        print(_status_line(not ks, "kill_switch", "ACTIVE 🚨" if ks else "off"))
        cd_detail = (
            f"ACTIVE until {risk.get('cooldown_until', '?')} — {risk.get('cooldown_reason', '')}"
            if cd else "off"
        )
        print(_status_line(not cd, "cooldown", cd_detail))
        dpnl = risk.get("daily_pnl")
        print(_status_line(True, "daily_pnl", f"${dpnl:+.2f}" if dpnl is not None else "n/a"))
        print(_status_line(True, "total_pnl", f"${risk.get('total_pnl', 0):+.2f}" if risk.get("total_pnl") is not None else "n/a"))
        print(_status_line(True, "loss_streak", str(risk.get("loss_streak", 0))))

    # ── BTC snapshots ────────────────────────────────────────────────────────
    print("\nBTC SNAPSHOT FRESHNESS")
    print("-" * 72)
    btc_age = btc.get("age_seconds")
    btc_ok = btc_age is not None and btc_age < 600  # fresh if <10 min
    print(_status_line(btc_ok, "last_snapshot", _age_str(_parse_ts(btc.get("last_snapshot_ts")))))

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\nSUMMARY")
    print("-" * 72)
    alerts = []
    if not settle["running"]:
        alerts.append("AUTO-SETTLE LOOP NOT RUNNING — run start_dashboard.sh or run_auto_settle_loop.sh")
    if trades["overdue_count"] > 0:
        alerts.append(f"{trades['overdue_count']} OVERDUE TRADE(S) — auto-settle should close these")
    if risk.get("kill_switch_active"):
        alerts.append("KILL SWITCH ACTIVE — no new trades will be approved")
    if risk.get("cooldown_active"):
        alerts.append(f"COOLDOWN ACTIVE until {risk.get('cooldown_until')}")
    if not dashboard["running"]:
        alerts.append("DASHBOARD NOT RUNNING")
    if truth.get("loaded") and truth.get("proof_verdict") != "SCALE_ELIGIBLE":
        alerts.append(
            f"RESEARCH ONLY — proof verdict {truth.get('proof_verdict')} "
            "means no scaling or real money"
        )
    if btc_age is not None and btc_age > 600:
        alerts.append(f"BTC SNAPSHOTS STALE ({_age_str(_parse_ts(btc.get('last_snapshot_ts')))}) — dashboard may not be scanning")

    if alerts:
        for a in alerts:
            print(f"  ⚠  {a}")
    else:
        print("  All systems nominal.")
    print()


if __name__ == "__main__":
    main()
