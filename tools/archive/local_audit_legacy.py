#!/usr/bin/env python3
"""
local_audit.py

Usage:
  python3 local_audit.py
  # or:
  python3 local_audit.py /path/to/AI_System
"""

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ----------------------------
# Helpers
# ----------------------------

def load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                # skip malformed line
                continue
    return rows


def load_json(path: Path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def fmt_money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$0.00"


def bucket_conf(c):
    # confidence in [0,1]
    if c is None:
        return "unknown"
    try:
        c = float(c)
    except Exception:
        return "unknown"
    if c < 0.50: return "<0.50"
    if c < 0.55: return "0.50-0.54"
    if c < 0.60: return "0.55-0.59"
    if c < 0.65: return "0.60-0.64"
    if c < 0.70: return "0.65-0.69"
    if c < 0.75: return "0.70-0.74"
    if c < 0.80: return "0.75-0.79"
    return ">=0.80"


def bucket_edge(e):
    # edge in decimals (0.03 = 3%)
    if e is None:
        return "unknown"
    try:
        e = float(e)
    except Exception:
        return "unknown"
    if e < 0.00: return "<0.00"
    if e < 0.01: return "0.00-0.009"
    if e < 0.02: return "0.01-0.019"
    if e < 0.03: return "0.02-0.029"
    if e < 0.05: return "0.03-0.049"
    if e < 0.08: return "0.05-0.079"
    return ">=0.08"


def normalize_strategy(s):
    if s is None:
        return "UNKNOWN"
    s = str(s).strip().upper()
    if not s:
        return "UNKNOWN"
    # Handles SIGNAL_CRYPTO / TREND_SPORTS, etc.
    if "_" in s:
        base = s.split("_", 1)[0]
        if base in {"SIGNAL", "TREND", "ARB"}:
            return base
    return s


def learning_flag(tr):
    # Approximation from paper_trader flow:
    # learning override when 0.50 <= confidence < min_conf (default 0.65),
    # often minimum size; but we classify by confidence band only.
    c = tr.get("confidence")
    try:
        c = float(c)
    except Exception:
        return False
    return 0.50 <= c < 0.65


def dedupe_latest_by_key(trades):
    # Mirror PaperTrader rebuild behavior:
    # dedupe key = (ticker, timestamp), last line wins.
    d = {}
    for tr in trades:
        key = (tr.get("ticker", ""), tr.get("timestamp", ""))
        d[key] = tr
    return list(d.values())


def settled_records(trades):
    return [t for t in trades if str(t.get("status", "")).upper() == "SETTLED"]


def open_records(trades):
    return [t for t in trades if str(t.get("status", "")).upper() == "OPEN"]


def win_loss_counts(settled):
    wins = sum(1 for t in settled if str(t.get("result", "")).upper() == "WIN")
    losses = sum(1 for t in settled if str(t.get("result", "")).upper() == "LOSS")
    pushes = sum(1 for t in settled if str(t.get("result", "")).upper() in {"PUSH", "NEUTRAL"})
    return wins, losses, pushes


def pnl_sum(rows):
    total = 0.0
    for r in rows:
        try:
            total += float(r.get("pnl", 0.0) or 0.0)
        except Exception:
            pass
    return total


def avg(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals)/len(vals)) if vals else 0.0


def extract_block_reason(evt):
    details = evt.get("details", {}) if isinstance(evt.get("details"), dict) else {}
    # preference order
    for k in ("reason", "message"):
        v = details.get(k)
        if v:
            return str(v)
    et = evt.get("event_type")
    if et:
        return str(et)
    return "UNKNOWN"


# ----------------------------
# Main
# ----------------------------

def main():
    root = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path.cwd().resolve()

    trades_path = root / "logs" / "paper_trades.jsonl"
    risk_events_path = root / "logs" / "risk_events.jsonl"
    risk_state_path = root / "data" / "risk_state.json"

    trades_raw = load_jsonl(trades_path)
    trades = dedupe_latest_by_key(trades_raw)
    risk_events = load_jsonl(risk_events_path)
    risk_state = load_json(risk_state_path)

    now = datetime.now(timezone.utc)

    # Split trades
    opens = open_records(trades)
    settled = settled_records(trades)

    # (1) Current open trades
    print("\n" + "="*90)
    print("1) CURRENT OPEN TRADES")
    print("="*90)
    if not opens:
        print("No open trades.")
    else:
        opens_sorted = sorted(opens, key=lambda x: parse_ts(x.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
        total_open_size = sum(float(t.get("size", 0.0) or 0.0) for t in opens_sorted)
        print(f"Open count: {len(opens_sorted)} | Total open size: {fmt_money(total_open_size)}")
        print("-"*90)
        for t in opens_sorted:
            ts = t.get("timestamp", "")
            print(
                f"{ts} | {t.get('ticker','?')} | {t.get('action','?')} | "
                f"size={fmt_money(t.get('size',0))} | entry={t.get('entry_price')} | "
                f"conf={t.get('confidence')} | edge={t.get('edge')} | strat={t.get('strategy')}"
            )

    # (2) Clean settled stats
    print("\n" + "="*90)
    print("2) CLEAN SETTLED STATS")
    print("="*90)
    wins, losses, pushes = win_loss_counts(settled)
    total_settled = len(settled)
    total_pnl = pnl_sum(settled)
    win_rate = (wins / (wins + losses)) if (wins + losses) else 0.0
    avg_pnl = (total_pnl / total_settled) if total_settled else 0.0
    print(f"Settled trades: {total_settled}")
    print(f"Wins: {wins} | Losses: {losses} | Push/Neutral: {pushes}")
    print(f"Win rate (W/(W+L)): {win_rate:.2%}")
    print(f"Total settled PnL: {fmt_money(total_pnl)}")
    print(f"Avg PnL / settled: {fmt_money(avg_pnl)}")

    # (3) Learning vs normal trade stats
    print("\n" + "="*90)
    print("3) LEARNING VS NORMAL TRADE STATS")
    print("="*90)
    settled_learning = [t for t in settled if learning_flag(t)]
    settled_normal = [t for t in settled if not learning_flag(t)]

    def print_group(name, rows):
        w, l, p = win_loss_counts(rows)
        pn = pnl_sum(rows)
        wr = (w / (w + l)) if (w + l) else 0.0
        print(f"{name}: n={len(rows)}, W={w}, L={l}, P={p}, win_rate={wr:.2%}, total_pnl={fmt_money(pn)}")

    print_group("LEARNING (0.50<=conf<0.65)", settled_learning)
    print_group("NORMAL   (outside that band)", settled_normal)

    # (4) Strategy stats
    print("\n" + "="*90)
    print("4) STRATEGY STATS (SETTLED)")
    print("="*90)
    by_strategy = defaultdict(list)
    for t in settled:
        by_strategy[normalize_strategy(t.get("strategy"))].append(t)

    if not by_strategy:
        print("No settled strategy data.")
    else:
        for strat, rows in sorted(by_strategy.items(), key=lambda kv: len(kv[1]), reverse=True):
            w, l, p = win_loss_counts(rows)
            pn = pnl_sum(rows)
            wr = (w/(w+l)) if (w+l) else 0.0
            print(f"{strat:10s} | n={len(rows):4d} | W={w:3d} L={l:3d} P={p:3d} | win_rate={wr:6.2%} | pnl={fmt_money(pn)}")

    # (5) Confidence bucket stats
    print("\n" + "="*90)
    print("5) CONFIDENCE BUCKET STATS (SETTLED)")
    print("="*90)
    conf_groups = defaultdict(list)
    for t in settled:
        conf_groups[bucket_conf(t.get("confidence"))].append(t)

    order_conf = ["<0.50","0.50-0.54","0.55-0.59","0.60-0.64","0.65-0.69","0.70-0.74","0.75-0.79",">=0.80","unknown"]
    for b in order_conf:
        rows = conf_groups.get(b, [])
        if not rows:
            continue
        w, l, p = win_loss_counts(rows)
        pn = pnl_sum(rows)
        wr = (w/(w+l)) if (w+l) else 0.0
        print(f"{b:10s} | n={len(rows):4d} | W={w:3d} L={l:3d} | win_rate={wr:6.2%} | pnl={fmt_money(pn)}")

    # (6) Edge bucket stats
    print("\n" + "="*90)
    print("6) EDGE BUCKET STATS (SETTLED)")
    print("="*90)
    edge_groups = defaultdict(list)
    for t in settled:
        edge_groups[bucket_edge(t.get("edge"))].append(t)

    order_edge = ["<0.00","0.00-0.009","0.01-0.019","0.02-0.029","0.03-0.049","0.05-0.079",">=0.08","unknown"]
    for b in order_edge:
        rows = edge_groups.get(b, [])
        if not rows:
            continue
        w, l, p = win_loss_counts(rows)
        pn = pnl_sum(rows)
        wr = (w/(w+l)) if (w+l) else 0.0
        print(f"{b:10s} | n={len(rows):4d} | W={w:3d} L={l:3d} | win_rate={wr:6.2%} | pnl={fmt_money(pn)}")

    # (7) Top risk block reasons
    print("\n" + "="*90)
    print("7) TOP RISK BLOCK REASONS")
    print("="*90)
    blocked_events = []
    for e in risk_events:
        et = str(e.get("event_type", "")).upper()
        # Include trade blocked and common hard-stop events
        if et in {"TRADE_BLOCKED", "LOSS_LIMIT_HIT", "WEEKLY_LOSS_LIMIT_HIT", "COOLDOWN_ACTIVE", "MAX_POSITIONS_REACHED"}:
            blocked_events.append(e)

    if not blocked_events:
        print("No block-like events found in risk_events.jsonl.")
    else:
        rc = Counter(extract_block_reason(e) for e in blocked_events)
        for reason, n in rc.most_common(10):
            print(f"{n:4d}  {reason}")

    # (8) Current risk state
    print("\n" + "="*90)
    print("8) CURRENT RISK STATE")
    print("="*90)
    if not risk_state:
        print("risk_state.json missing or unreadable.")
    else:
        # show key fields with fallbacks
        daily_pnl = float(risk_state.get("daily_pnl", 0.0) or 0.0)
        weekly_pnl = float(risk_state.get("weekly_pnl", 0.0) or 0.0)
        open_positions = int(risk_state.get("open_positions", 0) or 0)
        total_exposure = float(risk_state.get("total_exposure", 0.0) or 0.0)
        trades_today = int(risk_state.get("trades_today", 0) or 0)
        loss_streak = int(risk_state.get("loss_streak", 0) or 0)
        kill_switch_active = bool(risk_state.get("kill_switch_active", False))
        trading_paused = bool(risk_state.get("trading_paused", False))
        cooldown_until = parse_ts(risk_state.get("cooldown_until"))
        cooldown_reason = risk_state.get("cooldown_reason", "")

        print(f"daily_pnl         : {fmt_money(daily_pnl)}")
        print(f"weekly_pnl        : {fmt_money(weekly_pnl)}")
        print(f"open_positions    : {open_positions}")
        print(f"total_exposure    : {fmt_money(total_exposure)}")
        print(f"trades_today      : {trades_today}")
        print(f"loss_streak       : {loss_streak}")
        print(f"kill_switch_active: {kill_switch_active}")
        print(f"trading_paused    : {trading_paused}")
        print(f"cooldown_until    : {risk_state.get('cooldown_until')}")
        print(f"cooldown_reason   : {cooldown_reason}")
        print(f"last_updated      : {risk_state.get('last_updated')}")

    # (9) Whether system can trade right now
    print("\n" + "="*90)
    print("9) CAN SYSTEM TRADE RIGHT NOW?")
    print("="*90)

    # Constants from your config defaults (adjust here if changed)
    DAILY_LOSS_LIMIT = -50.0
    MAX_TRADES_PER_DAY = 20

    if not risk_state:
        print("UNKNOWN (risk_state unavailable).")
        can_trade = None
        blockers = ["missing risk_state.json"]
    else:
        daily_pnl = float(risk_state.get("daily_pnl", 0.0) or 0.0)
        total_exposure = float(risk_state.get("total_exposure", 0.0) or 0.0)
        trades_today = int(risk_state.get("trades_today", 0) or 0)
        kill_switch_active = bool(risk_state.get("kill_switch_active", False))
        trading_paused = bool(risk_state.get("trading_paused", False))
        cooldown_until = parse_ts(risk_state.get("cooldown_until"))

        effective_daily_risk = daily_pnl - total_exposure
        cooldown_active = bool(cooldown_until and now < cooldown_until)

        blockers = []
        if kill_switch_active:
            blockers.append("kill_switch_active")
        if trading_paused:
            blockers.append("trading_paused")
        if daily_pnl <= DAILY_LOSS_LIMIT:
            blockers.append(f"daily_pnl<=limit ({daily_pnl:.2f}<={DAILY_LOSS_LIMIT:.2f})")
        if effective_daily_risk <= DAILY_LOSS_LIMIT:
            blockers.append(
                f"effective_daily_risk<=limit ({effective_daily_risk:.2f}<={DAILY_LOSS_LIMIT:.2f})"
            )
        if trades_today >= MAX_TRADES_PER_DAY:
            blockers.append(f"max_trades_per_day reached ({trades_today}/{MAX_TRADES_PER_DAY})")
        if cooldown_active:
            blockers.append("cooldown_active")

        can_trade = len(blockers) == 0

        print(f"can_trade: {can_trade}")
        print(f"daily_pnl: {daily_pnl:.2f}")
        print(f"total_exposure: {total_exposure:.2f}")
        print(f"effective_daily_risk (daily_pnl - exposure): {effective_daily_risk:.2f}")
        print(f"daily_loss_limit: {DAILY_LOSS_LIMIT:.2f}")
        print(f"trades_today: {trades_today}/{MAX_TRADES_PER_DAY}")
        print(f"cooldown_active: {cooldown_active}")

        if blockers:
            print("blockers:")
            for b in blockers:
                print(f" - {b}")

    # (10) Biggest bottleneck right now
    print("\n" + "="*90)
    print("10) BIGGEST BOTTLENECK RIGHT NOW")
    print("="*90)

    # Heuristic: if currently blocked, first active blocker in risk precedence.
    # Else: top historical block reason from risk_events.
    if 'can_trade' in locals() and can_trade is False:
        precedence = [
            "kill_switch_active",
            "trading_paused",
            "daily_pnl<=limit",
            "effective_daily_risk<=limit",
            "max_trades_per_day reached",
            "cooldown_active",
        ]
        chosen = None
        for p in precedence:
            for b in blockers:
                if b.startswith(p):
                    chosen = b
                    break
            if chosen:
                break
        print(f"Current bottleneck: {chosen or blockers[0]}")
    else:
        blocked_events = []
        for e in risk_events:
            et = str(e.get("event_type", "")).upper()
            if et in {"TRADE_BLOCKED", "LOSS_LIMIT_HIT", "WEEKLY_LOSS_LIMIT_HIT", "COOLDOWN_ACTIVE", "MAX_POSITIONS_REACHED"}:
                blocked_events.append(e)
        if blocked_events:
            rc = Counter(extract_block_reason(e) for e in blocked_events)
            reason, n = rc.most_common(1)[0]
            print(f"Historical bottleneck: {reason} (count={n})")
        else:
            print("No clear bottleneck (no block events found and system appears tradable).")

    # Footer
    print("\n" + "="*90)
    print("FILES READ")
    print("="*90)
    print(f"- {trades_path}")
    print(f"- {risk_events_path}")
    print(f"- {risk_state_path}")


if __name__ == "__main__":
    main()
