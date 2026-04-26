"""
tools/paper_state.py
--------------------
Paper trading position management utility.

Commands:
  show        — display all OPEN trades (TEST vs real) + current risk state
  sync-risk   — update data/risk_state.json to match real OPEN non-TEST trades
  reset-risk  — zero positions/kill_switch/cooldown in risk_state.json

Does NOT modify logs/paper_trades.jsonl.
Does NOT change strategy, thresholds, or execution logic.
"""

import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── PATHS ──────────────────────────────────────────────────────

ROOT        = Path(__file__).parent.parent
TRADES_LOG  = ROOT / "logs" / "paper_trades.jsonl"
RISK_STATE  = ROOT / "data" / "risk_state.json"


# ─── HELPERS ────────────────────────────────────────────────────

def is_test_ticker(ticker: str) -> bool:
    """
    Identify synthetic test tickers used by paper_trader self-tests.
    Real Kalshi tickers look like: KXBTCD-26APR-T12.5, INXD-26APR28-T5374
    Test tickers are simple names with no date/price components.
    """
    t = ticker.upper()
    if t.startswith("TEST"):
        return True
    # Old-format test tickers from early runs
    if t in {"BTCUSD-UP", "BTCUSD-DOWN", "ETHD-UP", "UNKNOWN"}:
        return True
    return False


def load_trades() -> list:
    """Load all records from paper_trades.jsonl. Skips bad lines with a warning."""
    if not TRADES_LOG.exists():
        print(f"  [WARN] Trade log not found: {TRADES_LOG}")
        return []
    trades = []
    with open(TRADES_LOG) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [WARN] Line {i} skipped (bad JSON): {e}")
    return trades


def get_open_trades(trades: list) -> list:
    """Return only records where status == 'OPEN'."""
    return [t for t in trades if t.get("status") == "OPEN"]


def get_size(trade: dict) -> float:
    """
    Extract dollar size, handling both log formats:
      New format: 'size' field (from paper_trader.py)
      Old format: 'entry_cost' field (from earlier test scripts)
    """
    return float(trade.get("size") or trade.get("entry_cost") or 0.0)


def load_risk_state() -> dict:
    """Load risk_state.json. Returns {} if file is missing or unreadable."""
    if not RISK_STATE.exists():
        return {}
    try:
        with open(RISK_STATE) as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] Could not read {RISK_STATE}: {e}")
        return {}


def save_risk_state(state: dict) -> None:
    """Write risk_state.json, creating data/ directory if needed."""
    RISK_STATE.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(RISK_STATE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  Saved → {RISK_STATE}")


# ─── COMMAND: show ───────────────────────────────────────────────

def cmd_show():
    """Display all OPEN trades (split TEST vs real) and current risk state."""
    trades     = load_trades()
    open_all   = get_open_trades(trades)
    test_open  = [t for t in open_all if     is_test_ticker(t.get("ticker", ""))]
    real_open  = [t for t in open_all if not is_test_ticker(t.get("ticker", ""))]

    real_exposure = sum(get_size(t) for t in real_open)
    test_exposure = sum(get_size(t) for t in test_open)

    print("\n" + "=" * 62)
    print("PAPER POSITION STATE")
    print("=" * 62)
    print(f"  Log:            {TRADES_LOG}")
    print(f"  Total records:  {len(trades)}")
    print(f"  OPEN records:   {len(open_all)}  "
          f"(real: {len(real_open)}, test: {len(test_open)})")

    # ── Real open positions ──────────────────────────────────────
    print()
    print(f"── REAL OPEN POSITIONS ({len(real_open)}) " + "─" * 30)
    if real_open:
        _print_trade_rows(real_open)
        print(f"  {'TOTAL EXPOSURE':<45} ${real_exposure:7.2f}")
    else:
        print("  (none)")

    # ── Test open positions ──────────────────────────────────────
    print()
    print(f"── TEST / STALE OPEN ({len(test_open)}) " + "─" * 32)
    if test_open:
        _print_trade_rows(test_open)
        print(f"  {'TOTAL TEST EXPOSURE':<45} ${test_exposure:7.2f}")
    else:
        print("  (none)")

    # ── Risk state ───────────────────────────────────────────────
    print()
    print("── RISK STATE (" + str(RISK_STATE.relative_to(ROOT)) + ") " + "─" * 20)
    rs = load_risk_state()
    if rs:
        ks  = rs.get("kill_switch_active", False)
        cd  = rs.get("cooldown_until") or "none"
        print(f"  open_positions:  {rs.get('open_positions', 'n/a')}")
        print(f"  total_exposure:  ${float(rs.get('total_exposure', 0)):.2f}")
        print(f"  kill_switch:     {'🚨 ACTIVE' if ks else 'off'}")
        print(f"  cooldown_until:  {cd}")
        print(f"  daily_pnl:       ${rs.get('daily_pnl', 0):.2f}")
        print(f"  loss_streak:     {rs.get('loss_streak', 0)}")
        print(f"  last_updated:    {rs.get('last_updated', 'n/a')}")

        # Warn if risk state is out of sync with real OPEN trades
        rs_pos = rs.get("open_positions", 0)
        if rs_pos != len(real_open):
            print(f"\n  ⚠️  MISMATCH: risk_state says {rs_pos} open, "
                  f"log has {len(real_open)} real OPEN trades.")
            print(f"     Run: python3 tools/paper_state.py sync-risk")
    else:
        print("  (risk_state.json not found — will be created on first trade)")

    print("=" * 62 + "\n")


def _print_trade_rows(trades: list) -> None:
    """Print a compact row per trade."""
    for t in trades:
        ts     = (t.get("timestamp") or "")[:19]
        ticker = t.get("ticker", "?")
        action = t.get("action", "?")
        size   = get_size(t)
        conf   = t.get("confidence", 0)
        edge   = t.get("edge", 0)
        print(f"  {ts}  {ticker:<32} {action:<8}  "
              f"${size:6.2f}  conf={conf:.2f}  edge={edge:.4f}")


# ─── COMMAND: sync-risk ──────────────────────────────────────────

def cmd_sync_risk():
    """
    Update risk_state.json so open_positions and total_exposure
    reflect the actual OPEN non-TEST trades in paper_trades.jsonl.

    All other fields (daily_pnl, loss_streak, dates, etc.) are preserved.
    """
    trades    = load_trades()
    open_all  = get_open_trades(trades)
    real_open = [t for t in open_all if not is_test_ticker(t.get("ticker", ""))]
    exposure  = round(sum(get_size(t) for t in real_open), 2)

    rs = load_risk_state()
    old_pos = rs.get("open_positions", "n/a")
    old_exp = rs.get("total_exposure", "n/a")

    rs["open_positions"] = len(real_open)
    rs["total_exposure"] = exposure

    print("\n" + "=" * 62)
    print("SYNC RISK STATE → real OPEN non-TEST trades")
    print("=" * 62)
    print(f"  open_positions:  {old_pos}  →  {rs['open_positions']}")
    print(f"  total_exposure:  {old_exp}  →  ${rs['total_exposure']:.2f}")
    print(f"  (daily_pnl, loss_streak, kill_switch, dates: unchanged)")
    save_risk_state(rs)
    print("=" * 62 + "\n")


# ─── COMMAND: reset-risk ────────────────────────────────────────

def cmd_reset_risk():
    """
    Zero out position counters and clear kill_switch / cooldown.

    Preserves: daily_pnl, weekly_pnl, loss_streak, date fields.
    Does NOT touch: logs/paper_trades.jsonl.
    """
    rs = load_risk_state()

    print("\n" + "=" * 62)
    print("RESET RISK STATE")
    print("=" * 62)
    print(f"  open_positions:  {rs.get('open_positions', 0)}  →  0")
    print(f"  total_exposure:  {rs.get('total_exposure', 0)}  →  0.0")
    print(f"  kill_switch:     {rs.get('kill_switch_active', False)}  →  False")
    print(f"  cooldown_until:  {rs.get('cooldown_until', None)}  →  None")
    print()
    print(f"  PRESERVED:")
    print(f"    daily_pnl      = ${rs.get('daily_pnl', 0):.2f}")
    print(f"    weekly_pnl     = ${rs.get('weekly_pnl', 0):.2f}")
    print(f"    loss_streak    = {rs.get('loss_streak', 0)}")
    print(f"    trades_today   = {rs.get('trades_today', 0)}")
    print()
    print(f"  paper_trades.jsonl: NOT touched")

    rs["open_positions"]   = 0
    rs["total_exposure"]   = 0.0
    rs["kill_switch_active"] = False
    rs["cooldown_until"]   = None
    rs["cooldown_reason"]  = ""

    save_risk_state(rs)
    print("=" * 62 + "\n")


# ─── ENTRY POINT ────────────────────────────────────────────────

USAGE = """
paper_state.py — Paper trading position management utility

Commands:
  python3 tools/paper_state.py show        View all OPEN trades + risk state
  python3 tools/paper_state.py sync-risk   Sync risk_state.json to real OPEN trades
  python3 tools/paper_state.py reset-risk  Zero positions/kill_switch in risk_state.json
"""

if __name__ == "__main__":
    os.chdir(ROOT)          # ensure relative paths resolve from project root
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "show":
        cmd_show()
    elif cmd == "sync-risk":
        cmd_sync_risk()
    elif cmd == "reset-risk":
        cmd_reset_risk()
    else:
        print(USAGE)
        sys.exit(1)
