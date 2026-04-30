"""
dashboard.py — SAM's Live Trading OS Dashboard v3 (PAPER TRADING EDITION)
Integrates crypto scanner + Bayesian engine + AI edge signals + PAPER TRADER
Every signal is auto-logged. After 100 trades, you'll know if you have edge.
Run: python3 ~/Desktop/AI_System/Dashboard.py
Open: http://localhost:5001
"""

import os
import sys
import json
import time
import threading
import io
import contextlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

try:
    from flask import Flask, jsonify, render_template_string
    from flask_cors import CORS
except ImportError:
    print("Run: pip3 install flask flask-cors")
    exit()

# ─────────────────────────────────────────
# IMPORT BRAIN MODULES (PHASE 3 - UPDATED IMPORTS)
# ─────────────────────────────────────────

try:
    from brain.market_scanner import scan_crypto_markets, fetch_and_enrich_crypto_markets, build_signal
    from engine.decision_engine import analyze_market, compute_arb_edge
    BRAIN_OK = True
except ImportError as e:
    print(f"[WARN] Brain modules not found: {e}")
    BRAIN_OK = False

# Import paper trader
try:
    from brain.paper_trader import PaperTrader, MAX_CONCURRENT_OPEN_TRADES
    from engine.edge_calculator import MarketData
    PAPER_TRADER_OK = True
except ImportError as e:
    print(f"[WARN] Paper trader not found: {e}")
    PAPER_TRADER_OK = False
    MAX_CONCURRENT_OPEN_TRADES = 0

try:
    from tools.performance_report import (
        load_trades,
        get_pnl,
        get_size,
        get_clv,
        build_terminal_key_sets,
        classify_open_records,
        classify_settled_records,
    )
    PERFORMANCE_REPORT_OK = True
except ImportError as e:
    print(f"[WARN] Performance report helpers not found: {e}")
    PERFORMANCE_REPORT_OK = False

# Fallback: use existing kalshi_arb if brain not available
try:
    from brain.kalshi_arb import fetch_markets as fetch_all_markets
    LEGACY_OK = True
except ImportError:
    LEGACY_OK = False


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

SCAN_INTERVAL = 30       # seconds — fast scan for crypto markets
PORT = 5001
BANKROLL = float(os.getenv("BANKROLL", "500"))
AUTO_BET_THRESHOLD = 0.70  # confidence needed for auto-bet (not enabled yet)
RECENT_RISK_EVENT_LIMIT = 250
MAX_DISPLAY_OPPORTUNITIES = 300
LIVE_EVENT_LIMIT = 80

app = Flask(__name__)
CORS(app)

ROOT = Path(__file__).parent
RISK_EVENTS_LOG = ROOT / "logs" / "risk_events.jsonl"

# Initialize paper trader
paper_trader = None
if PAPER_TRADER_OK:
    paper_trader = PaperTrader(
        bankroll=BANKROLL,
        min_edge=0.03,
        min_confidence=0.65,
        max_bet_size=50.0,
        kelly_fraction=0.25
    )
    paper_trader.enable()
    print("[INIT] Paper trader enabled - will auto-log ALL signals")

# ─────────────────────────────────────────
# HELPER: Real-time risk status snapshot (M-18)
# ─────────────────────────────────────────

class _TeeStdout(io.StringIO):
    """Capture process_signal traces while preserving normal terminal output."""

    def __init__(self, passthrough):
        super().__init__()
        self.passthrough = passthrough

    def write(self, text):
        self.passthrough.write(text)
        return super().write(text)

    def flush(self):
        self.passthrough.flush()
        return super().flush()


def call_paper_trader_with_trace(*args, **kwargs) -> tuple:
    capture = _TeeStdout(sys.stdout)
    with contextlib.redirect_stdout(capture):
        trade = paper_trader.process_signal(*args, **kwargs)
    return trade, capture.getvalue()


def classify_execution_trace(trace_text: str, trade) -> dict:
    trace = trace_text or ""
    info = {
        "market_filter_blocked": 0,
        "council_blocked": 0,
        "council_overridden": 0,
        "risk_blocked": 0,
        "trade_opened": 0,
        "other_blocked": 0,
    }

    if "DATA_COLLECTION_OVERRIDE" in trace:
        info["council_overridden"] = 1

    if trade:
        info["trade_opened"] = 1
        return info

    if "market quality filter" in trace or "[FILTER] skipped" in trace:
        info["market_filter_blocked"] = 1
    elif "stop: council block" in trace or "[COUNCIL] BLOCKED:" in trace:
        info["council_blocked"] = 1
    elif "risk decision=BLOCK" in trace or "Trade blocked by risk manager" in trace:
        info["risk_blocked"] = 1
    else:
        info["other_blocked"] = 1

    return info


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quote_price_for_action(quote, action: str):
    if not quote:
        return None

    if action == "BET_NO":
        value = quote.get("no_ask", quote.get("price_no"))
    else:
        value = quote.get("yes_ask", quote.get("price_yes"))

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _current_quote_index() -> dict:
    return {
        opp.get("ticker"): opp
        for opp in state.get("opportunities", [])
        if opp.get("ticker")
    }


def _mark_open_trade(rec: dict, quote_index: dict) -> dict:
    quote = quote_index.get(rec.get("ticker"))
    mark_price = _quote_price_for_action(quote, rec.get("action"))
    entry_price = _safe_float(rec.get("entry_price"), None)
    size = get_size(rec)

    unrealized_pnl = None
    if mark_price is not None and entry_price not in (None, 0):
        contracts = size / entry_price
        unrealized_pnl = round((mark_price - entry_price) * contracts, 2)

    return {
        "mark_price": mark_price,
        "unrealized_pnl": unrealized_pnl,
    }


def _display_opportunities(opportunities: list) -> list:
    return opportunities[:MAX_DISPLAY_OPPORTUNITIES]


def summarize_performance() -> dict:
    if not PERFORMANCE_REPORT_OK:
        return {"error": "performance_report helpers unavailable"}

    all_records = load_trades()
    total_lines = len(all_records)
    raw_settled = [r for r in all_records if r.get("status") == "SETTLED"]
    raw_wins = [r for r in raw_settled if get_pnl(r) > 0]
    raw_losses = [r for r in raw_settled if get_pnl(r) < 0]
    raw_pnl = sum(get_pnl(r) for r in raw_settled)

    active_opens, stale_opens = classify_open_records(all_records)
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted_settled = classify_settled_records(
        all_records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )

    wins = [r for r in clean_settled if get_pnl(r) > 0]
    losses = [r for r in clean_settled if get_pnl(r) < 0]
    pushes = [r for r in clean_settled if get_pnl(r) == 0]
    total_pnl = sum(get_pnl(r) for r in clean_settled)
    total_wagered = sum(get_size(r) for r in clean_settled)
    conf_vals = [_safe_float(r.get("confidence")) for r in clean_settled if r.get("confidence") is not None]
    edge_vals = [_safe_float(r.get("edge")) for r in clean_settled if r.get("edge") is not None]
    clv_vals = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    quote_index = _current_quote_index()
    realized_pnl = round(total_pnl, 2)
    unrealized_vals = []

    clv_by_strategy = defaultdict(lambda: {"count": 0, "total": 0.0, "positive": 0, "negative": 0, "flat": 0})
    for rec in clean_settled:
        clv = get_clv(rec)
        if clv is None:
            continue
        strategy = rec.get("strategy") or rec.get("raw_strategy") or "UNKNOWN"
        row = clv_by_strategy[strategy]
        row["count"] += 1
        row["total"] += clv
        if clv > 0:
            row["positive"] += 1
        elif clv < 0:
            row["negative"] += 1
        else:
            row["flat"] += 1

    active_trade_cards = []
    for rec in sorted(active_opens, key=lambda x: x.get("timestamp", "")):
        mark = _mark_open_trade(rec, quote_index)
        if mark["unrealized_pnl"] is not None:
            unrealized_vals.append(mark["unrealized_pnl"])
        active_trade_cards.append({
            "timestamp": str(rec.get("timestamp", ""))[:19],
            "ticker": rec.get("ticker"),
            "action": rec.get("action"),
            "size": get_size(rec),
            "entry_price": rec.get("entry_price"),
            "strategy": rec.get("strategy"),
            "raw_strategy": rec.get("raw_strategy"),
            "original_confidence": rec.get("original_confidence") or rec.get("raw_confidence"),
            "council_confidence": rec.get("council_confidence") or rec.get("confidence"),
            "original_edge": rec.get("original_edge"),
            "adjusted_edge": rec.get("adjusted_edge") or rec.get("edge"),
            "risk_edge": rec.get("risk_edge"),
            "open_trade_mark_price": mark["mark_price"],
            "open_trade_unrealized_pnl": mark["unrealized_pnl"],
        })

    clv_strategy_rows = []
    for strategy, row in sorted(clv_by_strategy.items()):
        avg = row["total"] / row["count"] if row["count"] else 0.0
        clv_strategy_rows.append({
            "strategy": strategy,
            "count": row["count"],
            "avg_clv": round(avg, 4),
            "positive": row["positive"],
            "negative": row["negative"],
            "flat": row["flat"],
        })

    return {
        "raw": {
            "total_records": total_lines,
            "settled_rows": len(raw_settled),
            "open_raw": sum(1 for r in all_records if r.get("status") == "OPEN"),
            "forced_close": sum(1 for r in all_records if r.get("status") == "FORCED_CLOSE"),
            "voided": sum(1 for r in all_records if r.get("status") == "VOID_LEGACY_DUPLICATE"),
            "no_status": sum(1 for r in all_records if "status" not in r),
            "wins": len(raw_wins),
            "losses": len(raw_losses),
            "win_rate": len(raw_wins) / len(raw_settled) if raw_settled else 0.0,
            "total_pnl": round(raw_pnl, 2),
        },
        "clean": {
            "settled_trades": len(clean_settled),
            "conflicted_settled": len(conflicted_settled),
            "active_open": len(active_opens),
            "stale_open": len(stale_opens),
            "wins": len(wins),
            "losses": len(losses),
            "pushes": len(pushes),
            "win_rate": len(wins) / len(clean_settled) if clean_settled else 0.0,
            "total_pnl": round(total_pnl, 2),
            "total_wagered": round(total_wagered, 2),
            "roi": round(total_pnl / total_wagered, 4) if total_wagered else 0.0,
            "avg_confidence": round(sum(conf_vals) / len(conf_vals), 4) if conf_vals else None,
            "avg_edge": round(sum(edge_vals) / len(edge_vals), 4) if edge_vals else None,
            "avg_clv": round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None,
            "clv_positive": sum(1 for v in clv_vals if v > 0),
            "clv_negative": sum(1 for v in clv_vals if v < 0),
            "clv_flat": sum(1 for v in clv_vals if v == 0),
        },
        "live_pnl": {
            "realized_pnl": realized_pnl,
            "unrealized_pnl": round(sum(unrealized_vals), 2) if unrealized_vals else 0.0,
            "live_total_pnl": round(realized_pnl + sum(unrealized_vals), 2),
            "marked_open_trades": len(unrealized_vals),
            "unmarked_open_trades": len(active_opens) - len(unrealized_vals),
        },
        "active_trades": active_trade_cards,
        "clv_by_strategy": clv_strategy_rows,
    }


def read_recent_risk_events(limit: int = RECENT_RISK_EVENT_LIMIT) -> list:
    if not RISK_EVENTS_LOG.exists():
        return []
    rows = []
    with open(RISK_EVENTS_LOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def summarize_recent_blocked_reasons() -> dict:
    events = read_recent_risk_events()
    reason_counts = Counter()
    event_counts = Counter()
    for event in events:
        event_type = event.get("event_type", "UNKNOWN")
        event_counts[event_type] += 1
        details = event.get("details") or {}
        reason = details.get("message") or details.get("reason") or event_type
        if event_type in {
            "TRADE_BLOCKED",
            "MAX_POSITIONS_REACHED",
            "INSUFFICIENT_EDGE",
            "INSUFFICIENT_CONFIDENCE",
            "COOLDOWN_ACTIVE",
        }:
            reason_counts[str(reason)] += 1

    return {
        "window_events": len(events),
        "event_counts": dict(event_counts.most_common(10)),
        "blocked_reasons": [
            {"reason": reason, "count": count}
            for reason, count in reason_counts.most_common(8)
        ],
    }


def _event_timestamp(value=None) -> str:
    if value:
        return str(value)
    return datetime.now(timezone.utc).isoformat()


def _live_event(source: str, level: str, message: str, timestamp=None) -> dict:
    return {
        "timestamp": _event_timestamp(timestamp),
        "source": source,
        "level": level,
        "message": message,
    }


def _event_sort_key(event: dict) -> str:
    return str(event.get("timestamp") or "")


def _fmt_event_money(value) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):.2f}"


def _trade_event(rec: dict) -> dict:
    status = str(rec.get("status") or "UNKNOWN")
    result = str(rec.get("result") or "")
    ticker = rec.get("ticker") or "UNKNOWN"
    action = rec.get("action") or "--"
    pnl = get_pnl(rec) if PERFORMANCE_REPORT_OK else _safe_float(rec.get("pnl"))
    edge = rec.get("risk_edge", rec.get("edge"))
    timestamp = rec.get("settled_at") or rec.get("timestamp")

    if status == "OPEN":
        level = "SUCCESS"
        message = (
            f"Trade opened {ticker} {action} "
            f"${get_size(rec):.2f} @ {_safe_float(rec.get('entry_price')):.4f}"
        )
        if edge is not None:
            message += f" edge={_safe_float(edge):+.4f}"
        return _live_event("paper_trader", level, message, timestamp)

    if status == "SETTLED":
        level = "SUCCESS" if pnl > 0 else "WARN" if pnl < 0 else "INFO"
        message = f"Trade settled {ticker} {result or status} PnL {_fmt_event_money(pnl)}"
        if rec.get("exit_price") is not None:
            message += f" exit={_safe_float(rec.get('exit_price')):.4f}"
        if rec.get("clv") is not None:
            message += f" CLV={_safe_float(rec.get('clv')):+.4f}"
        return _live_event("settlement", level, message, timestamp)

    if status == "FORCED_CLOSE":
        level = "WARN" if pnl < 0 else "INFO"
        reason = rec.get("reason") or rec.get("cleanup_reason") or result or "FORCED_CLOSE"
        message = f"Forced close {ticker} {reason} PnL {_fmt_event_money(pnl)}"
        if rec.get("exit_price") is not None:
            message += f" exit={_safe_float(rec.get('exit_price')):.4f}"
        if rec.get("clv") is not None:
            message += f" CLV={_safe_float(rec.get('clv')):+.4f}"
        return _live_event("settlement", level, message, timestamp)

    return _live_event("paper_trader", "INFO", f"Trade record {ticker} status={status}", timestamp)


def _risk_event_level(event: dict) -> str:
    raw = str(event.get("severity") or event.get("level") or "").upper()
    if raw in {"ERROR", "WARN", "WARNING", "SUCCESS"}:
        return "WARN" if raw == "WARNING" else raw
    event_type = str(event.get("event_type") or "")
    if event_type in {"TRADE_BLOCKED", "COOLDOWN_ACTIVE", "MAX_POSITIONS_REACHED"}:
        return "WARN"
    if event_type in {"KILL_SWITCH_ACTIVE", "HARD_STOP"}:
        return "ERROR"
    return "INFO"


def _risk_event_message(event: dict) -> str:
    event_type = event.get("event_type") or "RISK_EVENT"
    details = event.get("details") or {}
    reason = details.get("message") or details.get("reason")
    ticker = details.get("ticker") or details.get("market") or details.get("symbol")
    parts = [str(event_type)]
    if ticker:
        parts.append(str(ticker))
    if reason:
        parts.append(str(reason))
    return " | ".join(parts)


def build_live_events(limit: int = LIVE_EVENT_LIMIT) -> list:
    events = []

    for item in state.get("scan_log", [])[:20]:
        events.append(_live_event(
            "scanner",
            "INFO" if "ERROR" not in str(item.get("msg", "")).upper() else "ERROR",
            str(item.get("msg") or "scan event"),
            item.get("time"),
        ))

    funnel = state.get("execution_funnel") or {}
    funnel_has_activity = any(
        int(funnel.get(k) or 0) > 0
        for k in (
            "scanned",
            "actionable",
            "entered_paper_trader",
            "market_filter_blocked",
            "council_blocked",
            "council_overridden",
            "risk_blocked",
            "trade_opened",
            "other_blocked",
        )
    )
    if funnel and funnel_has_activity:
        events.append(_live_event(
            "dashboard",
            "INFO",
            (
                f"Execution funnel scanned={funnel.get('scanned', 0)} "
                f"actionable={funnel.get('actionable', 0)} "
                f"entered={funnel.get('entered_paper_trader', 0)} "
                f"opened={funnel.get('trade_opened', 0)} "
                f"risk_blocked={funnel.get('risk_blocked', 0)}"
            ),
            funnel.get("last_updated") or state.get("last_scan"),
        ))
        if funnel.get("council_blocked"):
            events.append(_live_event(
                "paper_trader",
                "WARN",
                f"Council blocked {funnel.get('council_blocked')} signal(s) in latest scan",
                funnel.get("last_updated") or state.get("last_scan"),
            ))
        if funnel.get("council_overridden"):
            events.append(_live_event(
                "paper_trader",
                "INFO",
                f"Data collection override allowed {funnel.get('council_overridden')} signal(s)",
                funnel.get("last_updated") or state.get("last_scan"),
            ))
        if funnel.get("market_filter_blocked"):
            events.append(_live_event(
                "paper_trader",
                "WARN",
                f"Market quality filter blocked {funnel.get('market_filter_blocked')} signal(s)",
                funnel.get("last_updated") or state.get("last_scan"),
            ))
        if funnel.get("risk_blocked"):
            events.append(_live_event(
                "risk",
                "WARN",
                f"Risk manager blocked {funnel.get('risk_blocked')} signal(s) in latest scan",
                funnel.get("last_updated") or state.get("last_scan"),
            ))

    for event in read_recent_risk_events(60):
        events.append(_live_event(
            "risk",
            _risk_event_level(event),
            _risk_event_message(event),
            event.get("timestamp"),
        ))

    if PERFORMANCE_REPORT_OK:
        for rec in load_trades()[-60:]:
            events.append(_trade_event(rec))

    risk = get_risk_status() if paper_trader else {}
    if risk:
        status = risk.get("system_status") or "NORMAL"
        level = "ERROR" if status in {"KILL_SWITCH", "HARD_STOP"} else "WARN" if status == "NEAR_LIMIT" else "INFO"
        events.append(_live_event(
            "risk",
            level,
            (
                f"Risk status {status} daily={_fmt_event_money(risk.get('daily_pnl'))} "
                f"weighted_exposure={_fmt_event_money(risk.get('weighted_exposure'))} "
                f"room={_fmt_event_money(risk.get('remaining_risk_room'))} "
                f"open={risk.get('open_positions', 0)}/{risk.get('max_open_trade_slots', 0)}"
            ),
        ))

    events.sort(key=_event_sort_key, reverse=True)
    return events[:limit]


def get_risk_status() -> dict:
    if not paper_trader:
        return {}
    rm   = paper_trader.risk_manager
    rm_s = rm.get_status()
    open_trades    = paper_trader.open_trades
    open_count     = len(open_trades)
    total_exposure = round(sum(float(t.get("size", 0.0)) for t in open_trades), 2)
    weighted_exposure = round(total_exposure * 0.5, 2)
    daily_pnl            = rm.daily_pnl
    loss_limit           = rm_s["daily_loss_limit"]
    effective_daily_risk = round(daily_pnl - weighted_exposure, 2)
    remaining_risk_room  = round(effective_daily_risk - loss_limit, 2)
    risk_used_pct = (round(abs(effective_daily_risk) / abs(loss_limit) * 100, 1)
                     if loss_limit != 0 else 0.0)
    cooldown_active    = rm_s["cooldown_active"]
    cooldown_remaining = rm_s["cooldown_remaining_minutes"]
    cooldown_reason    = rm_s["cooldown_reason"]
    if rm.kill_switch_active:                system_status = "KILL_SWITCH"
    elif effective_daily_risk <= loss_limit: system_status = "HARD_STOP"
    elif remaining_risk_room <= 10:          system_status = "NEAR_LIMIT"
    else:                                    system_status = "NORMAL"
    can_trade = (not rm.kill_switch_active and not cooldown_active
                 and effective_daily_risk > loss_limit)
    last_result = last_pnl = None
    if paper_trader.trade_history:
        lt = paper_trader.trade_history[-1]
        last_result = lt.get("result")
        last_pnl    = lt.get("pnl")
    exposure_breakdown = [
        {"ticker": t.get("ticker", "?"), "action": t.get("action", "?"),
         "size": float(t.get("size", 0.0)), "entry_price": float(t.get("entry_price", 0.0))}
        for t in open_trades
    ]
    return {
        "daily_pnl": round(daily_pnl, 2), "weekly_pnl": round(rm.weekly_pnl, 2),
        "open_positions": open_count, "total_exposure": total_exposure,
        "full_exposure": total_exposure, "weighted_exposure": weighted_exposure,
        "max_open_trade_slots": MAX_CONCURRENT_OPEN_TRADES,
        "open_trade_slots_available": max(0, MAX_CONCURRENT_OPEN_TRADES - open_count),
        "effective_daily_risk": effective_daily_risk, "daily_loss_limit": loss_limit,
        "remaining_risk_room": remaining_risk_room, "risk_used_pct": risk_used_pct,
        "loss_streak": rm.loss_streak, "kill_switch_active": rm.kill_switch_active,
        "cooldown_active": cooldown_active, "cooldown_remaining_min": cooldown_remaining,
        "cooldown_reason": cooldown_reason, "system_status": system_status,
        "can_trade": can_trade, "last_result": last_result, "last_pnl": last_pnl,
        "exposure_breakdown": exposure_breakdown,
    }


# Shared state
state = {
    "markets": [],          # raw crypto markets
    "opportunities": [],    # AI-analyzed opportunities
    "display_opportunities": [],
    "market_count": 0,
    "alerts": [],           # high-confidence alerts
    "scan_log": [],
    "last_scan": "Never",
    "total_scans": 0,
    "alerts_today": 0,
    "bankroll": BANKROLL,
    "pnl_today": 0.0,
    "arb_count": 0,
    "bet_count": 0,
    "brain_ok": BRAIN_OK,
    "paper_trader_ok": PAPER_TRADER_OK,
    "paper_stats": {},      # Paper trading stats
    "risk_status": {},      # Risk manager snapshot (M-18)
    "performance_report": {},
    "execution_funnel": {
        "scanned": 0,
        "actionable": 0,
        "entered_paper_trader": 0,
        "market_filter_blocked": 0,
        "council_blocked": 0,
        "council_overridden": 0,
        "risk_blocked": 0,
        "trade_opened": 0,
        "other_blocked": 0,
        "last_updated": None,
    },
    "recent_blocked_reasons": {},
}


# ─────────────────────────────────────────
# HELPER: Determine event type from ticker/title
# ─────────────────────────────────────────

def detect_event_type(ticker, title):
    """Classify market as Sports, Crypto, Politics, or Other"""
    text = (ticker + " " + title).lower()
    
    # Sports keywords
    sports_kw = ["nfl", "nba", "mlb", "nhl", "wnba", "ncaa", "football", "basketball", 
                 "baseball", "hockey", "soccer", "game", "match", "team", "player",
                 "chiefs", "lakers", "yankees", "premier league", "world cup"]
    if any(kw in text for kw in sports_kw):
        return "SPORTS"
    
    # Crypto keywords
    crypto_kw = ["btc", "eth", "bitcoin", "ethereum", "crypto", "sol", "solana", 
                 "doge", "ada", "bnb", "xrp", "usdc", "usdt", "defi"]
    if any(kw in text for kw in crypto_kw):
        return "CRYPTO"
    
    # Politics keywords
    politics_kw = ["election", "trump", "biden", "congress", "senate", "president", 
                   "vote", "poll", "debate", "bill", "law", "supreme court"]
    if any(kw in text for kw in politics_kw):
        return "POLITICS"
    
    return "OTHER"


# ─────────────────────────────────────────
# HELPER: Determine trade reason tag
# ─────────────────────────────────────────

def determine_reason_tag(opportunity):
    """Tag each trade with reason: ARB, SIGNAL, NEWS, TREND, RANDOM"""
    action = opportunity.get("action", "")
    
    if action == "ARB":
        return "ARB"
    elif "BET" in action:
        # Check if edge is from bayesian analysis
        if opportunity.get("confidence", 0) >= 0.75:
            return "SIGNAL"
        elif opportunity.get("edge", 0) >= 0.10:
            return "TREND"
        else:
            return "SIGNAL"
    else:
        return "PASS"


# ─────────────────────────────────────────
# BACKGROUND SCANNER THREAD (ENHANCED)
# ─────────────────────────────────────────

def background_scan():
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            if BRAIN_OK:
                opportunities = scan_crypto_markets(bankroll=BANKROLL)
                
            elif LEGACY_OK:
                pass  # handled in the second branch below
            if BRAIN_OK:

                state["opportunities"] = opportunities
                state["display_opportunities"] = _display_opportunities(opportunities)
                state["market_count"] = len(opportunities)
                state["last_scan"] = now
                state["total_scans"] += 1

                # Update alert counts
                arbs = [o for o in opportunities if o["action"] == "ARB"]
                bets = [o for o in opportunities if "BET" in o["action"]]
                state["arb_count"] = len(arbs)
                state["bet_count"] = len(bets)
                funnel = {
                    "scanned": len(opportunities),
                    "actionable": sum(1 for o in opportunities if o.get("action") != "PASS"),
                    "entered_paper_trader": 0,
                    "market_filter_blocked": 0,
                    "council_blocked": 0,
                    "council_overridden": 0,
                    "risk_blocked": 0,
                    "trade_opened": 0,
                    "other_blocked": 0,
                    "last_updated": now,
                }

                # ─── PAPER TRADE EVERY SIGNAL ───
                if paper_trader and PAPER_TRADER_OK:
                    for opp in opportunities:
                        # Skip PASS signals
                        if opp["action"] == "PASS":
                            continue
                        
                        # Detect event type
                        event_type = detect_event_type(
                            opp.get("ticker", ""),
                            opp.get("title", "")
                        )
                        
                        # Determine reason tag
                        reason_tag = determine_reason_tag(opp)
                        strategy_label = f"{reason_tag}_{event_type}"
                        
                        # Build MarketData with real quotes from scanner
                        _yes_ask = opp.get("yes_ask", opp.get("price_yes", 0.5))
                        _yes_bid = opp.get("yes_bid", _yes_ask)
                        _no_ask  = opp.get("no_ask",  opp.get("price_no",  0.5))
                        _no_bid  = opp.get("no_bid",  _no_ask)
                        _spread  = round(_yes_ask - _yes_bid, 4)
                        market_data = MarketData(
                            ticker=opp.get("ticker", "UNKNOWN"),
                            yes_price=_yes_ask,
                            no_price=_no_ask,
                            yes_bid=_yes_bid,
                            yes_ask=_yes_ask,
                            no_bid=_no_bid,
                            no_ask=_no_ask,
                            volume_24h=opp.get("volume", 0),
                            spread=_spread,
                            liquidity=opp.get("volume", 0) // 10,
                            fee_rate=0.01,
                            time_to_expiry=24.0,
                            venue="kalshi"
                        )
                        metadata_passthrough = {
                            "market_id": opp.get("market_id"),
                            "event_id": opp.get("event_id"),
                            "title": opp.get("title"),
                            "question": opp.get("question"),
                            "close_time": opp.get("close_time"),
                            "result_time": opp.get("result_time"),
                            "scanner_source": opp.get("scanner_source"),
                            "horizon": opp.get("horizon"),
                            "raw_strategy": strategy_label,
                            "reasoning": opp.get("reasoning"),
                            "decision_reason": opp.get("decision_reason"),
                            "scanner_edge": opp.get("edge"),
                            "scanner_confidence": opp.get("confidence"),
                            "arb_edge": opp.get("arb_edge"),
                            "z_score": opp.get("z_score"),
                            "kelly_frac": opp.get("kelly_frac"),
                            "scanner_timestamp": opp.get("timestamp"),
                        }
                        if opp.get("time_to_expiry") is not None:
                            metadata_passthrough["time_to_expiry"] = opp.get("time_to_expiry")
                        for key, value in metadata_passthrough.items():
                            if value is not None and value != "":
                                setattr(market_data, key, value)
                        
                        # Process signal through paper trader
                        estimated_prob = opp.get("confidence", 0.5)
                        if estimated_prob > 0:
                            funnel["entered_paper_trader"] += 1
                            trade, trace_text = call_paper_trader_with_trace(
                                market_data=market_data,
                                estimated_prob=estimated_prob,
                                strategy=strategy_label
                            )
                            trace_counts = classify_execution_trace(trace_text, trade)
                            for key, value in trace_counts.items():
                                funnel[key] += value
                    
                    # Update paper stats
                    state["paper_stats"] = paper_trader.get_stats()
                    state["risk_status"] = get_risk_status()
                    state["performance_report"] = summarize_performance()
                    state["recent_blocked_reasons"] = summarize_recent_blocked_reasons()

                state["execution_funnel"] = funnel

                # Fire alerts for high-confidence signals
                for o in opportunities:
                    if o["confidence"] >= AUTO_BET_THRESHOLD and o["action"] != "PASS":
                        alert = {
                            "time": now,
                            "ticker": o["ticker"],
                            "action": o["action"],
                            "confidence": o["confidence"],
                            "edge": o["edge"],
                            "bet_size": o["bet_size"],
                            "reasoning": o["reasoning"],
                        }
                        # Avoid duplicate alerts
                        existing = [a for a in state["alerts"] if a["ticker"] == o["ticker"]]
                        if not existing:
                            state["alerts"].insert(0, alert)
                            state["alerts_today"] += 1

                state["alerts"] = state["alerts"][:20]

                log_msg = (
                    f"Scan #{state['total_scans']} — "
                    f"{len(opportunities)} crypto opps | "
                    f"{len(arbs)} ARB | {len(bets)} BET signals"
                )

            elif LEGACY_OK:
                # Fallback to legacy scanner
                markets_raw = fetch_all_markets(limit=100)
                crypto_markets = [
                    m for m in markets_raw
                    if any(kw in (m.get("title","") + m.get("ticker","")).lower()
                           for kw in ["btc","eth","bitcoin","ethereum","crypto","sol"])
                ]
                state["opportunities"] = [{
                    "ticker": m.get("ticker",""),
                    "title": m.get("title","")[:55],
                    "action": "UNKNOWN",
                    "confidence": 0,
                    "edge": 0,
                    "bet_size": 0,
                    "price_yes": (m.get("yes_ask",0) or 0) / 100,
                    "price_no": (m.get("no_ask",0) or 0) / 100,
                    "yes_plus_no": ((m.get("yes_ask",0) or 0) + (m.get("no_ask",0) or 0)) / 100,
                    "volume": m.get("volume",0),
                    "reasoning": "Brain offline — install brain/market_scanner.py",
                } for m in crypto_markets]
                state["display_opportunities"] = _display_opportunities(state["opportunities"])
                state["market_count"] = len(state["opportunities"])
                state["last_scan"] = now
                state["total_scans"] += 1
                log_msg = f"Scan #{state['total_scans']} (legacy) — {len(crypto_markets)} crypto markets"

            else:
                log_msg = "No scanner available. Check brain/ modules."

        except Exception as e:
            log_msg = f"ERROR: {str(e)[:80]}"
            print(f"[SCANNER ERROR] {e}")

        state["scan_log"].insert(0, {"time": now, "msg": log_msg})
        state["scan_log"] = state["scan_log"][:50]
        time.sleep(SCAN_INTERVAL)


# ─────────────────────────────────────────
# HTML DASHBOARD (ENHANCED WITH PAPER STATS)
# ─────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>AI_SYSTEM // QUANT OS (PAPER TRADING MODE)</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Orbitron:wght@400;700;900&display=swap');

:root {
  --bg: #020202;
  --panel: #050a05;
  --border: #0d280d;
  --green: #00ff41;
  --green2: #39ff14;
  --green-dim: #1a4d1a;
  --red: #ff1744;
  --yellow: #ffd600;
  --cyan: #00e5ff;
  --purple: #c084fc;
  --text: #8db88d;
  --muted: #234d23;
  --dim: #0a1a0a;
}

* { margin:0; padding:0; box-sizing:border-box; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  overflow-x: hidden;
  height: 100vh;
  overflow-y: hidden;
}

/* CRT scanlines */
body::after {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,255,65,0.012) 2px, rgba(0,255,65,0.012) 4px);
  pointer-events: none;
  z-index: 9999;
}

/* ── HEADER ── */
.hdr {
  height: 44px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 14px;
  gap: 24px;
  position: relative;
}

.logo {
  font-family: 'Orbitron', monospace;
  font-size: 13px;
  font-weight: 900;
  color: var(--green);
  letter-spacing: 4px;
  text-shadow: 0 0 20px var(--green);
  white-space: nowrap;
}

.paper-badge {
  background: rgba(255,214,0,0.15);
  border: 1px solid var(--yellow);
  color: var(--yellow);
  padding: 3px 8px;
  border-radius: 3px;
  font-size: 9px;
  font-family: 'Orbitron', monospace;
  font-weight: 700;
  letter-spacing: 1px;
  text-shadow: 0 0 10px var(--yellow);
}

.hdr-stat { display:flex; flex-direction:column; align-items:center; min-width:60px; }
.hdr-val {
  font-family: 'Orbitron', monospace;
  font-size: 14px;
  font-weight: 700;
  color: var(--green2);
  text-shadow: 0 0 10px var(--green2);
  line-height: 1;
}
.hdr-val.arb { color: var(--yellow); text-shadow: 0 0 10px var(--yellow); }
.hdr-val.bet { color: var(--cyan); text-shadow: 0 0 10px var(--cyan); }
.hdr-label { font-size: 8px; color: var(--muted); letter-spacing: 2px; margin-top: 2px; }

.live-dot {
  width: 7px; height: 7px;
  background: var(--green);
  border-radius: 50%;
  box-shadow: 0 0 8px var(--green);
  animation: blink 1.2s infinite;
  flex-shrink: 0;
}
@keyframes blink { 0%,100%{opacity:1;} 50%{opacity:0.25;} }

.hdr-right { margin-left:auto; display:flex; align-items:center; gap:16px; }

.prog-bar-wrap {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 2px;
  background: var(--dim);
}
#prog-bar {
  height: 100%;
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
  transition: width 1s linear;
}

/* ── GRID ── */
.grid {
  display: grid;
  grid-template-columns: 380px 1fr 320px;
  height: calc(100vh - 44px);
  gap: 1px;
  background: var(--border);
}

.col { background: var(--panel); display:flex; flex-direction:column; overflow:hidden; }

/* ── PANEL HEADER ── */
.ph {
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(0,255,65,0.02);
  flex-shrink: 0;
}
.ph-title {
  font-family: 'Orbitron', monospace;
  font-size: 8px;
  letter-spacing: 3px;
  color: var(--green);
  text-transform: uppercase;
}
.ph-sub { font-size: 9px; color: var(--muted); }

/* ── SCROLLABLE BODY ── */
.pb { flex:1; overflow-y:auto; overflow-x:hidden; }
.pb::-webkit-scrollbar { width: 3px; }
.pb::-webkit-scrollbar-thumb { background: var(--green-dim); }

/* ── OPPORTUNITY ROWS ── */
.opp-row {
  display: grid;
  grid-template-columns: 100px 1fr 52px 52px 52px 58px;
  gap: 0;
  padding: 5px 10px;
  border-bottom: 1px solid rgba(13,40,13,0.6);
  align-items: center;
  transition: background 0.15s;
  cursor: default;
}
.opp-row:hover { background: rgba(0,255,65,0.03); }
.opp-row.arb-row { background: rgba(255,214,0,0.04); border-left: 2px solid var(--yellow); }
.opp-row.bet-row { border-left: 2px solid var(--green); }
.opp-row.pass-row { opacity: 0.45; }

.opp-ticker { font-size: 10px; color: var(--cyan); font-weight: 500; }
.opp-title { font-size: 10px; color: var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding: 0 6px; }

.action-badge {
  font-size: 9px; font-weight: 700; text-align: center;
  padding: 2px 0;
  border-radius: 2px;
  font-family: 'Orbitron', monospace;
  letter-spacing: 0.5px;
}
.ab-arb { color: var(--yellow); text-shadow: 0 0 8px var(--yellow); }
.ab-yes { color: var(--green2); text-shadow: 0 0 8px var(--green2); }
.ab-no  { color: var(--red); }
.ab-pass{ color: var(--muted); }

.conf-val { font-size: 10px; text-align:center; }
.edge-val { font-size: 10px; text-align:center; color: var(--muted); }
.edge-val.hot { color: var(--yellow); }
.bet-val  { font-size: 10px; text-align:right; color: var(--green); }

/* ── TABLE HEADER ── */
.tbl-head {
  display: grid;
  grid-template-columns: 100px 1fr 52px 52px 52px 58px;
  padding: 4px 10px;
  border-bottom: 1px solid var(--border);
  background: rgba(0,0,0,0.4);
}
.tbl-head span { font-size: 8px; color: var(--muted); letter-spacing: 1.5px; text-transform: uppercase; }
.tbl-head .r { text-align: right; }
.tbl-head .c { text-align: center; }

/* ── SCAN LOG ── */
.log-row {
  padding: 4px 10px;
  border-bottom: 1px solid rgba(13,40,13,0.4);
  font-size: 10px;
  color: var(--muted);
  line-height: 1.5;
}
.log-time { color: var(--green-dim); margin-right: 8px; font-size: 9px; }

/* ── LIVE OPS FEED ── */
.event-row {
  padding: 6px 10px;
  border-bottom: 1px solid rgba(13,40,13,0.45);
  font-size: 10px;
  line-height: 1.45;
}
.event-row.info { border-left: 2px solid var(--green-dim); }
.event-row.success { border-left: 2px solid var(--green2); background: rgba(0,255,65,0.025); }
.event-row.warn { border-left: 2px solid var(--yellow); background: rgba(255,214,0,0.025); }
.event-row.error { border-left: 2px solid var(--red); background: rgba(255,23,68,0.04); }
.event-top {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--muted);
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.event-source { color: var(--cyan); }
.event-level.info { color: var(--muted); }
.event-level.success { color: var(--green2); }
.event-level.warn { color: var(--yellow); }
.event-level.error { color: var(--red); }
.event-msg { color: var(--text); margin-top: 2px; }

/* ── ALERT ROWS ── */
.alert-row {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  border-left: 2px solid var(--green);
  background: rgba(0,255,65,0.025);
}
.alert-row.arb { border-left-color: var(--yellow); background: rgba(255,214,0,0.03); }
.alert-action {
  font-family: 'Orbitron', monospace; font-size: 10px; font-weight:700;
  color: var(--green2); text-shadow: 0 0 8px var(--green2);
}
.alert-action.arb { color: var(--yellow); text-shadow: 0 0 8px var(--yellow); }
.alert-ticker { color: var(--cyan); font-size: 10px; }
.alert-meta { color: var(--muted); font-size: 9px; margin-top: 3px; }
.alert-reason { color: var(--text); font-size: 9px; margin-top: 2px; }

/* ── PAPER TRADE STATS ── */
.stat-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--border);
  margin: 0;
}

.stat-cell {
  padding: 10px;
  background: var(--panel);
  text-align: center;
}

.stat-val-big {
  font-family: 'Orbitron', monospace;
  font-size: 18px;
  font-weight: 900;
  color: var(--green2);
  text-shadow: 0 0 15px var(--green2);
  line-height: 1;
}
.stat-val-big.positive { color: var(--green2); }
.stat-val-big.negative { color: var(--red); text-shadow: 0 0 15px var(--red); }
.stat-val-big.neutral { color: var(--yellow); text-shadow: 0 0 15px var(--yellow); }

.stat-label-small {
  font-size: 7px;
  color: var(--muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-top: 3px;
}

.verdict-box {
  padding: 12px 10px;
  border-top: 1px solid var(--border);
  background: rgba(0,255,65,0.02);
}

.verdict-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 10px;
}

.verdict-icon {
  width: 12px;
  text-align: center;
  font-weight: 700;
}
.verdict-icon.pass { color: var(--green2); }
.verdict-icon.fail { color: var(--red); }

.empty { padding: 20px 10px; color: var(--muted); font-size: 10px; text-align: center; }

/* ── RISK STATUS PANEL (M-18) ── */
#risk-status-box {
  flex-shrink: 0;
  border-top: 2px solid var(--border);
  overflow-y: auto;
  max-height: 48vh;
}
#risk-status-box::-webkit-scrollbar { width: 3px; }
#risk-status-box::-webkit-scrollbar-thumb { background: var(--green-dim); }

.risk-ph {
  padding: 6px 10px;
  background: rgba(0,255,65,0.03);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.risk-ph-title {
  font-family: 'Orbitron', monospace;
  font-size: 8px;
  letter-spacing: 3px;
  color: var(--green);
  text-transform: uppercase;
}
.status-pill {
  font-family: 'Orbitron', monospace;
  font-size: 8px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 3px;
  letter-spacing: 1px;
}
.sp-normal     { background: rgba(0,255,65,0.12);  color: var(--green2);  border: 1px solid var(--green2); }
.sp-near_limit { background: rgba(255,214,0,0.12); color: var(--yellow);  border: 1px solid var(--yellow); }
.sp-hard_stop  { background: rgba(255,23,68,0.15); color: var(--red);     border: 1px solid var(--red); }
.sp-kill_switch{ background: rgba(255,23,68,0.25); color: #fff;           border: 1px solid var(--red); }

.risk-body { padding: 8px 10px; }
.risk-row  { display: flex; justify-content: space-between; align-items: center; padding: 3px 0; border-bottom: 1px solid rgba(13,40,13,0.4); }
.risk-label { font-size: 9px; color: var(--muted); letter-spacing: 1px; }
.risk-val   { font-size: 10px; color: var(--text); font-weight: 500; }
.risk-val.positive { color: var(--green2); }
.risk-val.negative { color: var(--red); }
.risk-val.warning  { color: var(--yellow); }

.can-trade-yes { color: var(--green2); font-weight: 700; font-size: 10px; }
.can-trade-no  { color: var(--red);    font-weight: 700; font-size: 10px; }

.exp-section-hdr {
  font-size: 8px; color: var(--muted); letter-spacing: 2px;
  text-transform: uppercase; margin: 8px 0 4px;
}
.exp-row {
  display: flex; justify-content: space-between;
  font-size: 9px; padding: 2px 0;
  border-bottom: 1px solid rgba(13,40,13,0.3);
}
.exp-ticker { color: var(--cyan); }
.exp-size   { color: var(--green); }

.mini-section {
  border-top: 1px solid var(--border);
  padding: 8px 10px;
}
.mini-title {
  font-size: 8px;
  color: var(--muted);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 6px;
}
.mini-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(13,40,13,0.35);
  font-size: 9px;
}
.mini-key { color: var(--muted); }
.mini-val { color: var(--text); text-align: right; }
.mini-val.good { color: var(--green2); }
.mini-val.bad { color: var(--red); }
.mini-val.warn { color: var(--yellow); }
.active-card {
  padding: 6px 0;
  border-bottom: 1px solid rgba(13,40,13,0.5);
}
.active-card-top {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--cyan);
  font-size: 9px;
}
.active-card-meta {
  color: var(--muted);
  font-size: 8px;
  margin-top: 3px;
  line-height: 1.5;
}
.bar-row {
  display: grid;
  grid-template-columns: 1fr 34px;
  gap: 6px;
  align-items: center;
  padding: 2px 0;
  font-size: 9px;
}
.bar-label {
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar-count { color: var(--green2); text-align: right; }
</style>
</head>
<body>

<!-- ── HEADER ── -->
<div class="hdr">
  <div class="logo">AI_SYSTEM</div>
  <div class="paper-badge">PAPER MODE</div>
  <div class="live-dot"></div>

  <div class="hdr-stat">
    <div class="hdr-val" id="h-opps">--</div>
    <div class="hdr-label">Opps</div>
  </div>
  <div class="hdr-stat">
    <div class="hdr-val arb" id="h-arb">--</div>
    <div class="hdr-label">ARB</div>
  </div>
  <div class="hdr-stat">
    <div class="hdr-val bet" id="h-bet">--</div>
    <div class="hdr-label">BET</div>
  </div>
  <div class="hdr-stat">
    <div class="hdr-val" id="h-scans">--</div>
    <div class="hdr-label">Scans</div>
  </div>
  <div class="hdr-stat">
    <div class="hdr-val" id="h-paper-trades">--</div>
    <div class="hdr-label">Paper Trades</div>
  </div>

  <div class="hdr-right">
    <span style="font-size:9px;color:var(--muted)" id="h-last">Last: --</span>
    <span style="font-family:Orbitron;font-size:11px;color:var(--green)" id="h-clock">--:--:--</span>
  </div>

  <div class="prog-bar-wrap"><div id="prog-bar" style="width:100%"></div></div>
</div>

<!-- ── GRID ── -->
<div class="grid">

  <!-- COL 1: AI EDGE SCANNER -->
  <div class="col">
    <div class="ph">
      <span class="ph-title">⚡ AI Edge Scanner — Crypto</span>
      <span class="ph-sub" id="opp-count">0 markets</span>
    </div>
    <div class="tbl-head">
      <span>TICKER</span>
      <span>MARKET</span>
      <span class="c">ACTION</span>
      <span class="c">CONF</span>
      <span class="c">EDGE</span>
      <span class="r">BET $</span>
    </div>
    <div class="pb" id="opp-feed">
      <div class="empty">Scanning crypto markets...</div>
    </div>
  </div>

  <!-- COL 2: ALERTS + LOG -->
  <div class="col" style="display:grid;grid-template-rows:auto 1fr auto 1fr auto 1.2fr">

    <!-- ARB OPPORTUNITIES -->
    <div class="ph">
      <span class="ph-title">🔥 ARB Opportunities</span>
      <span class="ph-sub" id="arb-label">YES+NO &lt; $1</span>
    </div>
    <div class="pb" id="arb-feed">
      <div class="empty">No arb found yet. Scanning...</div>
    </div>

    <!-- SCAN LOG -->
    <div class="ph" style="margin-top:1px">
      <span class="ph-title">📡 Scan Log</span>
      <span class="ph-sub" id="log-count">--</span>
    </div>
    <div class="pb" id="scan-log">
      <div class="log-row">Initializing scanner...</div>
    </div>

    <!-- LIVE OPS FEED -->
    <div class="ph" style="margin-top:1px">
      <span class="ph-title">AI_SYSTEM LIVE OPS / AGENT CONSOLE</span>
      <span class="ph-sub" id="live-events-count">--</span>
    </div>
    <div class="pb" id="live-events-feed">
      <div class="empty">Waiting for live system events...</div>
    </div>
  </div>

  <!-- COL 3: PAPER TRADE STATS -->
  <div class="col">
    <div class="ph">
      <span class="ph-title">📊 Paper Trade Performance</span>
      <span class="ph-sub" id="paper-progress">0 / 100 trades</span>
    </div>
    <div class="pb" style="padding:0;min-height:0">

      <!-- Stats Grid -->
      <div class="stat-grid">
        <div class="stat-cell">
          <div class="stat-val-big" id="p-trades">0</div>
          <div class="stat-label-small">Total Records</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-settled">0</div>
          <div class="stat-label-small">Clean Settled</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-winrate">--</div>
          <div class="stat-label-small">Win Rate</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-pnl">$0</div>
          <div class="stat-label-small">Total P&L</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-edge">--</div>
          <div class="stat-label-small">Avg Edge</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-ev">--</div>
          <div class="stat-label-small">ROI</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-clv">--</div>
          <div class="stat-label-small">Avg CLV</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-sharpe">--</div>
          <div class="stat-label-small">Sharpe</div>
        </div>
      </div>

      <div class="mini-section">
        <div class="mini-title">Clean vs Raw Truth</div>
        <div class="mini-row"><span class="mini-key">Clean Settled</span><span id="m-clean-settled" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Raw Settled Rows</span><span id="m-raw-settled" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Conflicted Settled</span><span id="m-conflicted" class="mini-val warn">--</span></div>
        <div class="mini-row"><span class="mini-key">Stale Open Rows</span><span id="m-stale-open" class="mini-val warn">--</span></div>
      </div>

      <div class="mini-section">
        <div class="mini-title">Live Mark-to-Market P&amp;L</div>
        <div class="mini-row"><span class="mini-key">Realized P&amp;L</span><span id="live-realized-pnl" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Unrealized P&amp;L</span><span id="live-unrealized-pnl" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Live Total P&amp;L</span><span id="live-total-pnl" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Marked / Missing Quotes</span><span id="live-mark-count" class="mini-val">--</span></div>
      </div>

      <div class="mini-section">
        <div class="mini-title">Execution Funnel</div>
        <div id="funnel-box"></div>
      </div>

      <div class="mini-section">
        <div class="mini-title">Recent Blocked Reasons</div>
        <div id="blocked-reasons-box"></div>
      </div>

      <div class="mini-section">
        <div class="mini-title">CLV</div>
        <div class="mini-row"><span class="mini-key">Positive / Negative</span><span id="clv-dist" class="mini-val">--</span></div>
        <div id="clv-strategy-box"></div>
      </div>

      <div class="mini-section">
        <div class="mini-title">Active Trade Cards</div>
        <div id="active-trades-box"></div>
      </div>

      <!-- Verdict -->
      <div class="verdict-box">
        <div style="font-size:8px;color:var(--muted);letter-spacing:2px;margin-bottom:8px">PROOF CHECKLIST (100 TRADES)</div>
        <div class="verdict-line">
          <span class="verdict-icon" id="v-profitable">—</span>
          <span id="v-profitable-text">Profitable?</span>
        </div>
        <div class="verdict-line">
          <span class="verdict-icon" id="v-edge">—</span>
          <span id="v-edge-text">Positive edge?</span>
        </div>
        <div class="verdict-line">
          <span class="verdict-icon" id="v-clv">—</span>
          <span id="v-clv-text">Beat closing line?</span>
        </div>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:9px;color:var(--text)" id="verdict-msg">
          Paper trading in progress...<br>Need 100 trades to prove edge.
        </div>
      </div>

    </div>

    <!-- RISK STATUS — outside .pb so it's always visible (M-18) -->
    <div id="risk-status-box">
      <div class="risk-ph">
        <span class="risk-ph-title">🛡 Risk Status</span>
        <span id="r-status-pill" class="status-pill sp-normal">🟢 NORMAL</span>
      </div>
      <div class="risk-body">
        <div class="risk-row">
          <span class="risk-label">CAN OPEN NEW TRADES</span>
          <span id="r-can-trade" class="can-trade-yes">YES</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Daily P&amp;L</span>
          <span id="r-daily-pnl" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Weekly P&amp;L</span>
          <span id="r-weekly-pnl" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Open Positions</span>
          <span id="r-open-pos" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Open Slots</span>
          <span id="r-open-slots" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Full Exposure</span>
          <span id="r-exposure" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Weighted Exposure</span>
          <span id="r-weighted-exposure" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Effective Daily Risk</span>
          <span id="r-eff-risk" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Daily Loss Limit</span>
          <span id="r-loss-limit" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Remaining Room</span>
          <span id="r-room" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Risk Used</span>
          <span id="r-risk-pct" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Loss Streak</span>
          <span id="r-streak" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Kill Switch</span>
          <span id="r-kill" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Cooldown</span>
          <span id="r-cooldown" class="risk-val">--</span>
        </div>
        <div class="risk-row">
          <span class="risk-label">Last Trade</span>
          <span id="r-last-trade" class="risk-val">--</span>
        </div>
        <div id="r-exp-section" style="display:none">
          <div class="exp-section-hdr">Open Exposure Breakdown</div>
          <div id="r-exp-list"></div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
let scanInterval = 30; // matches server
let progInterval;

function startProgressBar() {
  clearInterval(progInterval);
  const bar = document.getElementById('prog-bar');
  bar.style.width = '100%';
  let pct = 100;
  progInterval = setInterval(() => {
    pct -= (100 / scanInterval);
    bar.style.width = Math.max(0, pct) + '%';
    if (pct <= 0) clearInterval(progInterval);
  }, 1000);
}

function actionClass(action) {
  if (action === 'ARB') return 'ab-arb';
  if (action.includes('YES')) return 'ab-yes';
  if (action.includes('NO')) return 'ab-no';
  return 'ab-pass';
}

function rowClass(action) {
  if (action === 'ARB') return 'arb-row';
  if (action.includes('BET')) return 'bet-row';
  return 'pass-row';
}

function confColor(conf) {
  if (conf >= 0.75) return 'var(--green2)';
  if (conf >= 0.65) return 'var(--yellow)';
  return 'var(--muted)';
}

function renderOpportunities(opps, totalCount) {
  const el = document.getElementById('opp-feed');
  const total = totalCount != null ? totalCount : opps.length;
  const shown = opps.length;
  document.getElementById('opp-count').textContent =
    total + ' markets' + (shown < total ? ` (${shown} shown)` : '');

  if (!total) {
    el.innerHTML = '<div class="empty">No crypto markets found.<br>Markets are most liquid 9am–4pm ET + sports events.</div>';
    return;
  }
  if (!opps.length) {
    el.innerHTML = '<div class="empty">Markets scanned, display list unavailable.</div>';
    return;
  }

  el.innerHTML = opps.map(o => {
    const isArb = o.action === 'ARB';
    const edgeHot = o.edge > 0.05;
    const yesNo = o.yes_plus_no || 0;
    const arbFlag = yesNo > 0 && yesNo < 0.98 ? ' ⚡' : '';

    return `<div class="opp-row ${rowClass(o.action)}">
      <div class="opp-ticker">${o.ticker || '—'}</div>
      <div class="opp-title" title="${o.title || ''}">${(o.title || '').slice(0,45)}</div>
      <div class="action-badge ${actionClass(o.action)}">${o.action}</div>
      <div class="conf-val" style="color:${confColor(o.confidence)}">${o.confidence ? (o.confidence*100).toFixed(0)+'%' : '—'}</div>
      <div class="edge-val ${edgeHot ? 'hot' : ''}">${o.edge ? o.edge.toFixed(3) : '—'}</div>
      <div class="bet-val">${o.bet_size > 0 ? '$'+o.bet_size.toFixed(0) : '—'}${arbFlag}</div>
    </div>`;
  }).join('');
}

function renderArbs(opps) {
  const arbs = opps.filter(o => o.action === 'ARB' || (o.yes_plus_no && o.yes_plus_no < 0.98));
  const el = document.getElementById('arb-feed');
  document.getElementById('arb-label').textContent = arbs.length + ' found';

  if (!arbs.length) {
    el.innerHTML = '<div class="empty">No arb windows open.<br>Watching for YES+NO &lt; $1...</div>';
    return;
  }

  el.innerHTML = arbs.map(o => `
    <div class="arb-list-item" style="padding:6px 10px;border-bottom:1px solid rgba(13,40,13,0.5);border-left:2px solid var(--yellow);background:rgba(255,214,0,0.03);margin:2px 0">
      <div style="display:flex;justify-content:space-between">
        <span style="color:var(--yellow);font-size:10px;font-weight:700">${o.ticker}</span>
        <span style="color:var(--green2);font-size:10px">+${o.arb_edge ? (o.arb_edge*100).toFixed(1) : ((1-o.yes_plus_no)*100).toFixed(1)}¢ edge</span>
      </div>
      <div style="color:var(--text);font-size:9px;margin-top:2px">YES ${(o.price_yes*100).toFixed(0)}¢ + NO ${(o.price_no*100).toFixed(0)}¢ = ${(o.yes_plus_no*100).toFixed(0)}¢ total</div>
      <div style="color:var(--muted);font-size:9px;margin-top:2px">${(o.reasoning||'').slice(0,80)}</div>
    </div>`).join('');
}

function renderLog(logs) {
  const el = document.getElementById('scan-log');
  document.getElementById('log-count').textContent = logs.length + ' events';
  if (!logs.length) return;
  el.innerHTML = logs.map(l =>
    `<div class="log-row"><span class="log-time">${l.time}</span>${l.msg}</div>`
  ).join('');
}

function escapeHtml(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function renderLiveEvents(events) {
  const el = document.getElementById('live-events-feed');
  const countEl = document.getElementById('live-events-count');
  if (!el || !countEl) return;
  const rows = events || [];
  countEl.textContent = rows.length + ' real events';
  if (!rows.length) {
    el.innerHTML = '<div class="empty">No live events available yet.</div>';
    return;
  }

  el.innerHTML = rows.map(ev => {
    const level = String(ev.level || 'INFO').toLowerCase();
    const safeLevel = ['info', 'success', 'warn', 'error'].includes(level) ? level : 'info';
    const timestamp = escapeHtml(String(ev.timestamp || '').slice(0, 19).replace('T', ' '));
    const source = escapeHtml(ev.source || 'system');
    const message = escapeHtml(ev.message || '');
    return `<div class="event-row ${safeLevel}">
      <div class="event-top">
        <span><span class="event-source">${source}</span> ${timestamp}</span>
        <span class="event-level ${safeLevel}">${escapeHtml(ev.level || 'INFO')}</span>
      </div>
      <div class="event-msg">${message}</div>
    </div>`;
  }).join('');
}

function fmtMoney(v) {
  if (v == null) return '--';
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
}

function moneyClass(v) {
  return 'mini-val ' + (v > 0 ? 'good' : v < 0 ? 'bad' : '');
}

function renderPaperStats(stats) {
  if (!stats || Object.keys(stats).length === 0) {
    document.getElementById('p-trades').textContent = '0';
    document.getElementById('p-settled').textContent = '0';
    document.getElementById('p-winrate').textContent = '--';
    document.getElementById('p-pnl').textContent = '$0';
    document.getElementById('p-edge').textContent = '--';
    document.getElementById('p-ev').textContent = '--';
    document.getElementById('p-clv').textContent = '--';
    document.getElementById('p-sharpe').textContent = '--';
    document.getElementById('paper-progress').textContent = '0 / 100 trades';
    document.getElementById('live-realized-pnl').textContent = '--';
    document.getElementById('live-unrealized-pnl').textContent = '--';
    document.getElementById('live-total-pnl').textContent = '--';
    document.getElementById('live-mark-count').textContent = '--';
    return;
  }

  const clean = stats.clean || stats;
  const raw = stats.raw || {};
  const total = raw.total_records || stats.total_trades || 0;
  const settled = clean.settled_trades || 0;
  const winRate = clean.win_rate || 0;
  const pnl = clean.total_pnl || 0;
  const edge = clean.avg_edge || 0;
  const ev = clean.roi || 0;
  const clv = clean.avg_clv;
  const sharpe = stats.sharpe || 0;

  document.getElementById('p-trades').textContent = total;
  document.getElementById('p-settled').textContent = settled;
  document.getElementById('p-winrate').textContent = settled > 0 ? (winRate * 100).toFixed(1) + '%' : '--';
  
  const pnlEl = document.getElementById('p-pnl');
  pnlEl.textContent = pnl >= 0 ? '+$' + pnl.toFixed(2) : '-$' + Math.abs(pnl).toFixed(2);
  pnlEl.className = 'stat-val-big ' + (pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral');

  document.getElementById('p-edge').textContent = settled > 0 ? (edge > 0 ? '+' : '') + (edge * 100).toFixed(2) + '%' : '--';
  document.getElementById('p-ev').textContent = settled > 0 ? (ev > 0 ? '+' : '') + (ev * 100).toFixed(1) + '%' : '--';
  document.getElementById('p-clv').textContent = (settled > 0 && clv != null) ? (clv > 0 ? '+' : '') + clv.toFixed(3) : '--';
  document.getElementById('p-sharpe').textContent = settled > 1 ? sharpe.toFixed(2) : '--';
  document.getElementById('paper-progress').textContent = settled + ' clean / 100 trades';

  document.getElementById('m-clean-settled').textContent = settled;
  document.getElementById('m-raw-settled').textContent = raw.settled_rows != null ? raw.settled_rows : '--';
  document.getElementById('m-conflicted').textContent = clean.conflicted_settled != null ? clean.conflicted_settled : '--';
  document.getElementById('m-stale-open').textContent = clean.stale_open != null ? clean.stale_open : '--';

  const live = stats.live_pnl || {};
  const realizedEl = document.getElementById('live-realized-pnl');
  const unrealizedEl = document.getElementById('live-unrealized-pnl');
  const liveTotalEl = document.getElementById('live-total-pnl');
  realizedEl.textContent = fmtMoney(live.realized_pnl);
  unrealizedEl.textContent = fmtMoney(live.unrealized_pnl);
  liveTotalEl.textContent = fmtMoney(live.live_total_pnl);
  realizedEl.className = moneyClass(live.realized_pnl || 0);
  unrealizedEl.className = moneyClass(live.unrealized_pnl || 0);
  liveTotalEl.className = moneyClass(live.live_total_pnl || 0);
  document.getElementById('live-mark-count').textContent =
    `${live.marked_open_trades || 0} / ${live.unmarked_open_trades || 0}`;

  document.getElementById('clv-dist').textContent =
    `${clean.clv_positive || 0} / ${clean.clv_negative || 0}` +
    (clean.clv_flat ? ` / flat ${clean.clv_flat}` : '');

  const clvStrategyBox = document.getElementById('clv-strategy-box');
  const clvRows = stats.clv_by_strategy || [];
  clvStrategyBox.innerHTML = clvRows.length ? clvRows.map(r => `
    <div class="mini-row">
      <span class="mini-key">${r.strategy}</span>
      <span class="mini-val ${r.avg_clv > 0 ? 'good' : r.avg_clv < 0 ? 'bad' : ''}">
        n=${r.count} avg=${r.avg_clv > 0 ? '+' : ''}${r.avg_clv.toFixed(4)}
      </span>
    </div>`).join('') : '<div class="mini-row"><span class="mini-key">No CLV by strategy yet</span><span class="mini-val">--</span></div>';

  const activeBox = document.getElementById('active-trades-box');
  const active = stats.active_trades || [];
  activeBox.innerHTML = active.length ? active.map(t => `
    <div class="active-card">
      <div class="active-card-top">
        <span>${t.ticker || 'UNKNOWN'}</span>
        <span>$${Number(t.size || 0).toFixed(2)} @ ${Number(t.entry_price || 0).toFixed(4)}</span>
      </div>
      <div class="active-card-meta">
        ${t.action || '--'} | strategy=${t.strategy || '--'} | raw=${t.raw_strategy || '--'}<br>
        conf raw=${t.original_confidence != null ? Number(t.original_confidence).toFixed(3) : '--'}
        council=${t.council_confidence != null ? Number(t.council_confidence).toFixed(3) : '--'}<br>
        edge raw=${t.original_edge != null ? Number(t.original_edge).toFixed(4) : '--'}
        adjusted=${t.adjusted_edge != null ? Number(t.adjusted_edge).toFixed(4) : '--'}
        risk=${t.risk_edge != null ? Number(t.risk_edge).toFixed(4) : '--'}<br>
        mark=${t.open_trade_mark_price != null ? Number(t.open_trade_mark_price).toFixed(4) : '--'}
        unrealized=${t.open_trade_unrealized_pnl != null ? fmtMoney(Number(t.open_trade_unrealized_pnl)) : '--'}
      </div>
    </div>`).join('') : '<div class="empty">No active trades.</div>';

  // Verdicts
  const profitable = pnl > 0;
  const hasEdge = edge > 0;
  const beatsClosing = clv != null && clv > 0;

  document.getElementById('v-profitable').textContent = profitable ? '✓' : '✗';
  document.getElementById('v-profitable').className = 'verdict-icon ' + (profitable ? 'pass' : 'fail');
  document.getElementById('v-profitable-text').textContent = profitable ? 'Profitable ✓' : 'Not profitable yet';

  document.getElementById('v-edge').textContent = hasEdge ? '✓' : '✗';
  document.getElementById('v-edge').className = 'verdict-icon ' + (hasEdge ? 'pass' : 'fail');
  document.getElementById('v-edge-text').textContent = hasEdge ? 'Positive edge ✓' : 'Negative edge';

  document.getElementById('v-clv').textContent = beatsClosing ? '✓' : '✗';
  document.getElementById('v-clv').className = 'verdict-icon ' + (beatsClosing ? 'pass' : 'fail');
  document.getElementById('v-clv-text').textContent = beatsClosing ? 'Beat closing line ✓' : 'Losing to closing line';

  // Final verdict message
  let msg = '';
  if (settled < 100) {
    msg = `Paper trading in progress...<br>Need ${100 - total} more trades to prove edge.`;
  } else if (profitable && hasEdge && beatsClosing) {
    msg = '<span style="color:var(--green2);font-weight:700">✓ EDGE PROVEN</span><br>Ready for small live bets ($5-10)';
  } else if (profitable && hasEdge) {
    msg = '<span style="color:var(--yellow)">⚠ PROFITABLE BUT</span><br>Not beating closing line consistently';
  } else {
    msg = '<span style="color:var(--red)">✗ NO EDGE DETECTED</span><br>Do NOT go live. Fix model first.';
  }
  document.getElementById('verdict-msg').innerHTML = msg;
}

function renderExecutionFunnel(funnel) {
  const box = document.getElementById('funnel-box');
  if (!box) return;
  if (!funnel || Object.keys(funnel).length === 0) {
    box.innerHTML = '<div class="mini-row"><span class="mini-key">No scan yet</span><span class="mini-val">--</span></div>';
    return;
  }
  const rows = [
    ['scanned', 'Scanned'],
    ['actionable', 'Actionable'],
    ['entered_paper_trader', 'Entered PaperTrader'],
    ['market_filter_blocked', 'Market Filter Blocked'],
    ['council_blocked', 'Council Blocked'],
    ['council_overridden', 'Council Overridden'],
    ['risk_blocked', 'Risk Blocked'],
    ['trade_opened', 'Trade Opened'],
    ['other_blocked', 'Other Blocked'],
  ];
  box.innerHTML = rows.map(([key, label]) => `
    <div class="mini-row">
      <span class="mini-key">${label}</span>
      <span class="mini-val">${funnel[key] != null ? funnel[key] : 0}</span>
    </div>`).join('');
}

function renderBlockedReasons(summary) {
  const box = document.getElementById('blocked-reasons-box');
  if (!box) return;
  const reasons = (summary && summary.blocked_reasons) || [];
  if (!reasons.length) {
    box.innerHTML = '<div class="mini-row"><span class="mini-key">No recent risk blocks</span><span class="mini-val">--</span></div>';
    return;
  }
  box.innerHTML = reasons.map(r => `
    <div class="bar-row" title="${r.reason}">
      <span class="bar-label">${r.reason}</span>
      <span class="bar-count">${r.count}</span>
    </div>`).join('');
}

function renderRiskStatus(risk) {
  if (!risk || Object.keys(risk).length === 0) return;

  const status = (risk.system_status || 'NORMAL').toUpperCase();
  const pillEl = document.getElementById('r-status-pill');
  const pillMap = {
    'NORMAL':      ['sp-normal',      '🟢 NORMAL'],
    'NEAR_LIMIT':  ['sp-near_limit',  '🟡 NEAR LIMIT'],
    'HARD_STOP':   ['sp-hard_stop',   '🔴 HARD STOP'],
    'KILL_SWITCH': ['sp-kill_switch', '🚨 KILL SWITCH'],
  };
  const [pillCls, pillTxt] = pillMap[status] || pillMap['NORMAL'];
  pillEl.className = 'status-pill ' + pillCls;
  pillEl.textContent = pillTxt;

  const canEl = document.getElementById('r-can-trade');
  canEl.textContent = risk.can_trade ? 'YES' : 'NO';
  canEl.className = risk.can_trade ? 'can-trade-yes' : 'can-trade-no';

  function fmtPnl(v) {
    if (v == null) return '--';
    return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
  }
  function pnlClass(v) {
    return v > 0 ? 'risk-val positive' : v < 0 ? 'risk-val negative' : 'risk-val';
  }

  const dpEl = document.getElementById('r-daily-pnl');
  dpEl.textContent = fmtPnl(risk.daily_pnl);
  dpEl.className = pnlClass(risk.daily_pnl);

  const wpEl = document.getElementById('r-weekly-pnl');
  wpEl.textContent = fmtPnl(risk.weekly_pnl);
  wpEl.className = pnlClass(risk.weekly_pnl);

  document.getElementById('r-open-pos').textContent  = risk.open_positions != null ? risk.open_positions : '--';
  document.getElementById('r-open-slots').textContent = risk.max_open_trade_slots != null
    ? `${risk.open_positions || 0}/${risk.max_open_trade_slots} (${risk.open_trade_slots_available || 0} free)`
    : '--';
  document.getElementById('r-exposure').textContent  = risk.total_exposure  != null ? '$' + risk.total_exposure.toFixed(2) : '--';
  document.getElementById('r-weighted-exposure').textContent = risk.weighted_exposure != null ? '$' + risk.weighted_exposure.toFixed(2) : '--';

  const effEl = document.getElementById('r-eff-risk');
  effEl.textContent = fmtPnl(risk.effective_daily_risk);
  effEl.className = pnlClass(risk.effective_daily_risk);

  document.getElementById('r-loss-limit').textContent = risk.daily_loss_limit != null ? '$' + risk.daily_loss_limit.toFixed(2) : '--';

  const roomEl = document.getElementById('r-room');
  roomEl.textContent = risk.remaining_risk_room != null ? fmtPnl(risk.remaining_risk_room) : '--';
  roomEl.className = (risk.remaining_risk_room != null && risk.remaining_risk_room <= 10)
    ? 'risk-val warning' : pnlClass(risk.remaining_risk_room);

  const pctEl = document.getElementById('r-risk-pct');
  pctEl.textContent = risk.risk_used_pct != null ? risk.risk_used_pct.toFixed(1) + '%' : '--';
  pctEl.className = (risk.risk_used_pct >= 90) ? 'risk-val negative' : (risk.risk_used_pct >= 70) ? 'risk-val warning' : 'risk-val';

  document.getElementById('r-streak').textContent = risk.loss_streak != null ? risk.loss_streak : '--';

  document.getElementById('r-kill').textContent = risk.kill_switch_active ? '🔴 ACTIVE' : '⚫ OFF';
  document.getElementById('r-kill').className = risk.kill_switch_active ? 'risk-val negative' : 'risk-val';

  let coolTxt = 'None';
  if (risk.cooldown_active) {
    coolTxt = '⏳ ' + (risk.cooldown_remaining_min || 0).toFixed(0) + ' min';
    if (risk.cooldown_reason) coolTxt += ' — ' + risk.cooldown_reason;
  }
  document.getElementById('r-cooldown').textContent = coolTxt;
  document.getElementById('r-cooldown').className = risk.cooldown_active ? 'risk-val warning' : 'risk-val';

  let lastTxt = '--';
  if (risk.last_result != null) {
    lastTxt = risk.last_result + (risk.last_pnl != null ? ' ' + fmtPnl(risk.last_pnl) : '');
  }
  const ltEl = document.getElementById('r-last-trade');
  ltEl.textContent = lastTxt;
  ltEl.className = (risk.last_result === 'WIN') ? 'risk-val positive' : (risk.last_result === 'LOSS') ? 'risk-val negative' : 'risk-val';

  const expSection = document.getElementById('r-exp-section');
  const expList    = document.getElementById('r-exp-list');
  const breakdown  = risk.exposure_breakdown || [];
  if (breakdown.length > 0) {
    expSection.style.display = '';
    expList.innerHTML = breakdown.map(e =>
      `<div class="exp-row">
        <span class="exp-ticker">${e.ticker} ${e.action}</span>
        <span class="exp-size">$${e.size.toFixed(2)} @ ${e.entry_price.toFixed(4)}</span>
      </div>`
    ).join('');
  } else {
    expSection.style.display = 'none';
  }
}

async function fetchState() {
  try {
    const [stateResp, riskResp] = await Promise.all([
      fetch('/api/state'),
      fetch('/api/risk_status'),
    ]);
    const d    = await stateResp.json();
    const risk = await riskResp.json();
    console.log("RISK STATUS DATA:", risk);

    // Header
    const displayOpps = d.display_opportunities || d.opportunities || [];
    const totalOpps = d.market_count != null ? d.market_count : (d.opportunities || []).length;
    const arbs = d.arb_count != null ? d.arb_count : displayOpps.filter(o => o.action === 'ARB').length;
    const bets = d.bet_count != null ? d.bet_count : displayOpps.filter(o => o.action && o.action.includes('BET')).length;
    const paperTrades = (d.performance_report && d.performance_report.clean && d.performance_report.clean.settled_trades)
      || (d.paper_stats && d.paper_stats.total_trades) || 0;

    document.getElementById('h-opps').textContent = totalOpps;
    document.getElementById('h-arb').textContent = arbs;
    document.getElementById('h-bet').textContent = bets;
    document.getElementById('h-scans').textContent = d.total_scans;
    document.getElementById('h-paper-trades').textContent = paperTrades;
    document.getElementById('h-last').textContent = 'Last: ' + (d.last_scan||'--');

    renderOpportunities(displayOpps, totalOpps);
    renderArbs(displayOpps);
    renderLog(d.scan_log || []);
    renderLiveEvents(d.live_events || []);
    renderPaperStats(d.performance_report || d.paper_stats || {});
    renderExecutionFunnel(d.execution_funnel || {});
    renderBlockedReasons(d.recent_blocked_reasons || {});
    renderRiskStatus(risk);

    startProgressBar();
  } catch(e) {
    console.error(e);
  }
}

// Clock
setInterval(() => {
  const t = new Date().toTimeString().slice(0,8);
  document.getElementById('h-clock').textContent = t;
}, 1000);

// Fetch loop
setInterval(fetchState, 5000);
fetchState();
startProgressBar();
</script>
</body>
</html>"""


# ─────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/state")
def api_state():
    opportunities = state.get("opportunities") or []
    state["market_count"] = len(opportunities)
    state["display_opportunities"] = _display_opportunities(opportunities)
    state["arb_count"] = sum(1 for o in opportunities if o.get("action") == "ARB")
    state["bet_count"] = sum(1 for o in opportunities if "BET" in str(o.get("action", "")))
    funnel = state.get("execution_funnel") or {}
    if opportunities and not funnel.get("scanned"):
        state["execution_funnel"] = {
            **state["execution_funnel"],
            "scanned": len(opportunities),
            "actionable": sum(1 for o in opportunities if o.get("action") != "PASS"),
            "entered_paper_trader": (
                sum(1 for o in opportunities if o.get("action") != "PASS")
                if paper_trader and PAPER_TRADER_OK else 0
            ),
            "last_updated": state.get("last_scan"),
        }
    state["performance_report"] = summarize_performance()
    state["recent_blocked_reasons"] = summarize_recent_blocked_reasons()
    if paper_trader:
        state["risk_status"] = get_risk_status()
    state["live_events"] = build_live_events()
    return jsonify(state)


@app.route("/api/opportunities")
def api_opps():
    return jsonify(state["opportunities"])


@app.route("/api/alerts")
def api_alerts():
    return jsonify(state["alerts"])


@app.route("/api/paper_stats")
def api_paper_stats():
    """Return paper trading stats"""
    return jsonify(summarize_performance())


@app.route("/api/risk_status")
def api_risk_status():
    """Return real-time risk status snapshot (M-18)."""
    return jsonify(get_risk_status())


@app.route("/api/live_events")
def api_live_events():
    """Return read-only live ops feed assembled from dashboard state and logs."""
    return jsonify({"events": build_live_events()})


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  AI_SYSTEM // TRADING OS DASHBOARD v3 (PAPER MODE)")
    print(f"  Open: http://localhost:{PORT}")
    print(f"  Paper Bankroll: ${BANKROLL}")
    print(f"  Brain modules: {'OK' if BRAIN_OK else 'NOT FOUND'}")
    print(f"  Paper trader: {'ENABLED - Auto-logging ALL signals' if PAPER_TRADER_OK else 'NOT FOUND'}")
    print(f"  Scan interval: {SCAN_INTERVAL}s")
    print("  Press Ctrl+C to stop")
    print("=" * 60)

    if not BRAIN_OK:
        print("\n[WARN] brain/market_scanner.py or engine/decision_engine.py not found.")
        print("       Make sure all Phase 3 files are in place.\n")
    
    if not PAPER_TRADER_OK:
        print("\n[WARN] Paper trader not found. Install:")
        print("       - brain/paper_trader.py")
        print("       - engine/edge_calculator.py")
        print("       - logs/trade_logger.py\n")

    t = threading.Thread(target=background_scan, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=PORT, debug=False)
