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
from typing import Any, Dict, List, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from config.trading_config import DATA_COLLECTION_MODE, GLOBAL_FORCED_LEARNING_MODE, MIN_CONFIDENCE, MIN_EDGE

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
    from logs.market_snapshot_logger import log_btc15m_snapshots
    BTC15M_SNAPSHOT_LOGGER_OK = True
except ImportError as e:
    print(f"[WARN] BTC 15M snapshot logger not available: {e}")
    BTC15M_SNAPSHOT_LOGGER_OK = False

try:
    from logs.scanner_opportunity_logger import log_scanner_opportunities
    SCANNER_OPPORTUNITY_LOGGER_OK = True
except ImportError as e:
    print(f"[WARN] Scanner opportunity logger not available: {e}")
    SCANNER_OPPORTUNITY_LOGGER_OK = False

try:
    from logs.execution_funnel_logger import log_execution_funnel
    EXECUTION_FUNNEL_LOGGER_OK = True
except ImportError as e:
    print(f"[WARN] Execution funnel logger not available: {e}")
    EXECUTION_FUNNEL_LOGGER_OK = False

try:
    from logs.payoff_aware_shadow_ranking_logger import log_payoff_aware_shadow_ranking
    PAYOFF_AWARE_SHADOW_LOGGER_OK = True
except ImportError as e:
    print(f"[WARN] Payoff-aware shadow ranking logger not available: {e}")
    PAYOFF_AWARE_SHADOW_LOGGER_OK = False

try:
    from brain.side_coverage_queue import (
        log_shadow_diagnostic,
        select_shadow_candidate,
    )
    SIDE_COVERAGE_QUEUE_OK = True
except ImportError as e:
    print(f"[WARN] Side coverage queue not available: {e}")
    SIDE_COVERAGE_QUEUE_OK = False

try:
    from brokers.underlying_price_client import fetch_btc_usd_price
    BTC_PRICE_CLIENT_OK = True
except ImportError as e:
    print(f"[WARN] BTC price client not available: {e}")
    BTC_PRICE_CLIENT_OK = False

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

try:
    from tools.clean_truth_report import (
        classify_records as _classify_records,
        row_quality_group as _row_quality_group,
        evaluate_proof_gates as _evaluate_proof_gates,
        calc_asymmetry as _calc_asymmetry,
        _avg as _truth_avg,
    )
    CLEAN_TRUTH_OK = True
except ImportError:
    CLEAN_TRUTH_OK = False

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
MARKET_HISTORY_LIMIT = 36
DASHBOARD_RUN_ID = f"dashboard_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
TRACKED_MARKET_SYMBOLS = ["BTC", "ETH", "DOGE", "SOL"]

app = Flask(__name__)
CORS(app)

ROOT = Path(__file__).parent
RISK_EVENTS_LOG = ROOT / "logs" / "risk_events.jsonl"

# Initialize paper trader
paper_trader = None
if PAPER_TRADER_OK:
    paper_trader = PaperTrader(
        bankroll=BANKROLL,
        min_edge=MIN_EDGE,
        min_confidence=MIN_CONFIDENCE,
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


def _asset_symbol(rec: dict) -> str:
    text = f"{rec.get('ticker', '')} {rec.get('title', '')} {rec.get('question', '')}".upper()
    for symbol in TRACKED_MARKET_SYMBOLS:
        if symbol in text:
            return symbol
    return "OTHER"


def _quote_mid(rec: dict):
    for key in ("market_mid", "yes_mid"):
        value = _safe_float(rec.get(key), None)
        if value is not None:
            return value

    yes_bid = _safe_float(rec.get("yes_bid"), None)
    yes_ask = _safe_float(rec.get("yes_ask"), None)
    if yes_bid is not None and yes_ask is not None:
        return round((yes_bid + yes_ask) / 2, 4)

    return _safe_float(rec.get("price_yes", rec.get("yes_price")), None)


def _quote_spread(rec: dict):
    spread = _safe_float(rec.get("spread"), None)
    if spread is not None:
        return spread
    yes_bid = _safe_float(rec.get("yes_bid"), None)
    yes_ask = _safe_float(rec.get("yes_ask"), None)
    if yes_bid is not None and yes_ask is not None:
        return round(yes_ask - yes_bid, 4)
    return None


def build_bet_reward_truth(rec: Optional[dict], current_price=None) -> Optional[dict]:
    """
    Return explicit contract economics for one dashboard trade row.

    This is a display helper only. It does not alter trade selection, proof
    gates, risk state, or historical records.
    """
    if not rec:
        return None

    entry_price = _safe_float(rec.get("entry_price"), None)
    size = _safe_float(rec.get("payout_notional"), None)
    if size is None:
        size = _safe_float(rec.get("size"), None)
    if entry_price is None or size is None:
        return None

    accounting_version = rec.get("accounting_version") or "legacy_hybrid_or_unversioned"
    payout_notional = size
    capital_at_risk = _safe_float(rec.get("capital_at_risk"), None)
    if capital_at_risk is None:
        capital_at_risk = entry_price * payout_notional
    max_profit = _safe_float(rec.get("max_profit_if_win"), None)
    if max_profit is None:
        max_profit = (1.0 - entry_price) * payout_notional
    max_loss = _safe_float(rec.get("max_loss_if_loss"), None)
    if max_loss is None:
        max_loss = entry_price * payout_notional

    economic_row = accounting_version == "economic_contract_notional_v1"
    legacy_row = accounting_version == "legacy_hybrid_or_unversioned"
    if economic_row:
        breakeven_wr = entry_price
        breakeven_label = "Economic BE"
    else:
        breakeven_wr = 1.0 / (2.0 - entry_price) if entry_price < 2.0 else None
        breakeven_label = "Legacy BE"

    reward_risk = (max_profit / max_loss) if max_loss and max_loss > 0 else None
    warning_parts = []
    if legacy_row:
        warning_parts.append(
            "Legacy accounting row - historical PnL may be conservative; see Phase 9M/9N reports."
        )
    if max_loss and max_profit is not None and (entry_price >= 0.85 or (reward_risk is not None and reward_risk < 0.25)):
        warning_parts.append(
            f"High-price contract: risking ~${capital_at_risk:.2f} to make ~${max_profit:.2f}."
        )

    economic_pnl = _safe_float(rec.get("economic_pnl"), None)
    recorded_pnl = _safe_float(rec.get("recorded_pnl"), None)
    if recorded_pnl is None:
        recorded_pnl = _safe_float(rec.get("pnl"), None)

    labels = [
        ("Entry Price", entry_price),
        ("Current Price / Mid", _safe_float(current_price, None)),
        ("Payout Notional", payout_notional),
        ("Capital at Risk", capital_at_risk),
        ("Max Profit", max_profit),
        ("Max Loss", max_loss),
        ("Reward/Risk", reward_risk),
        ("Breakeven WR", breakeven_wr),
        ("Accounting Version", accounting_version),
        ("Economic PnL", economic_pnl),
        ("Recorded PnL", recorded_pnl),
    ]

    return {
        "entry_price": round(entry_price, 6),
        "current_price": round(float(current_price), 6) if _safe_float(current_price, None) is not None else None,
        "payout_notional": round(payout_notional, 2),
        "capital_at_risk": round(capital_at_risk, 2),
        "max_profit_if_win": round(max_profit, 2),
        "max_loss_if_loss": round(max_loss, 2),
        "reward_risk": round(reward_risk, 4) if reward_risk is not None else None,
        "breakeven_wr": round(breakeven_wr, 4) if breakeven_wr is not None else None,
        "breakeven_label": breakeven_label,
        "accounting_version": accounting_version,
        "economic_pnl": round(economic_pnl, 2) if economic_pnl is not None else None,
        "recorded_pnl": round(recorded_pnl, 2) if recorded_pnl is not None else None,
        "open_exposure": round(capital_at_risk, 2),
        "is_legacy_accounting": legacy_row,
        "warning": " ".join(warning_parts) if warning_parts else None,
        "labels": [label for label, _ in labels],
    }


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


def _is_time_exit_record(rec: dict) -> bool:
    return (
        rec.get("status") == "FORCED_CLOSE"
        and (
            rec.get("result") == "TIME_EXIT"
            or rec.get("reason") == "TIME_EXIT"
            or rec.get("cleanup_reason") == "TIME_EXIT"
        )
    )


def _overdue_minutes(result_time_str):
    """Return minutes past result_time if result_time has passed, else None."""
    if not result_time_str:
        return None
    try:
        rt = datetime.fromisoformat(result_time_str.replace("Z", "+00:00"))
        delta = (datetime.now(timezone.utc) - rt).total_seconds() / 60
        return round(delta, 1) if delta > 0 else None
    except (TypeError, ValueError):
        return None


def _has_quote_metadata(rec: dict) -> bool:
    if rec.get("price_yes") is not None or rec.get("price_no") is not None:
        return True
    return any(rec.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def _is_modern_full_metadata(rec: dict) -> bool:
    return (
        rec.get("risk_edge") is not None
        and rec.get("model_probability") is not None
        and _has_quote_metadata(rec)
    )


def _avg_numeric(rows: List[dict], key: str):
    values = [_safe_float(r.get(key), None) for r in rows if r.get(key) is not None]
    return round(sum(values) / len(values), 4) if values else None


def _proof_state(condition_pass: bool, condition_fail: bool, watch_reason: str) -> str:
    if watch_reason:
        return "WATCH"
    if condition_pass:
        return "PASS"
    if condition_fail:
        return "FAIL"
    return "WATCH"


def build_proof_checklist(outcome_known_rows: List[dict], time_exit_rows: Optional[List[dict]] = None) -> dict:
    """Build proof status from true outcome-known SETTLED rows only."""
    time_exit_rows = time_exit_rows or []
    modern_rows = [r for r in outcome_known_rows if _is_modern_full_metadata(r)]
    legacy_rows = [r for r in outcome_known_rows if not _is_modern_full_metadata(r)]
    modern_count = len(modern_rows)
    data_collection_count = sum(1 for r in modern_rows if r.get("data_collection_override"))
    bootstrap_provisional_count = sum(1 for r in modern_rows if r.get("bootstrap_provisional"))
    normal_trade_count = max(0, modern_count - data_collection_count - bootstrap_provisional_count)
    modern_pnl = sum(get_pnl(r) for r in modern_rows)
    modern_wagered = sum(get_size(r) for r in modern_rows)
    modern_roi = round(modern_pnl / modern_wagered, 4) if modern_wagered else 0.0
    modern_avg_risk_edge = _avg_numeric(modern_rows, "risk_edge")
    modern_clv_vals = [v for v in (get_clv(r) for r in modern_rows) if v is not None]
    modern_avg_clv = round(sum(modern_clv_vals) / len(modern_clv_vals), 4) if modern_clv_vals else None
    sample_watch = modern_count < 30

    if modern_count == 0 and legacy_rows:
        quality = "LEGACY_CONTAMINATED"
    elif modern_rows and legacy_rows:
        quality = "MIXED"
    elif modern_rows:
        quality = "MODERN_ONLY"
    else:
        quality = "NO_EVALUATED_ROWS"

    watch_reason = "Modern sample below 30 rows" if sample_watch else ""
    profitability_state = _proof_state(
        modern_count >= 30 and modern_roi > 0,
        modern_count >= 30 and modern_roi <= 0,
        watch_reason,
    )
    edge_state = _proof_state(
        modern_count >= 30 and modern_avg_risk_edge is not None and modern_avg_risk_edge > 0 and modern_roi > 0,
        modern_count >= 30 and modern_avg_risk_edge is not None and modern_avg_risk_edge > 0 and modern_roi <= 0,
        watch_reason,
    )
    clv_state = _proof_state(
        modern_count >= 30 and modern_avg_clv is not None and modern_avg_clv > 0,
        modern_count >= 30 and (modern_avg_clv is None or modern_avg_clv <= 0),
        watch_reason,
    )

    # ── Proof gate verdict (inline — same logic as clean_truth_report) ────────
    _eval_wagered = sum(get_size(r) for r in outcome_known_rows)
    _eval_pnl     = sum(get_pnl(r)  for r in outcome_known_rows)
    _overall_roi  = _eval_pnl / _eval_wagered if _eval_wagered else 0.0
    _m_wins_pnl   = sum(get_pnl(r) for r in modern_rows if get_pnl(r) > 0)
    _m_loss_pnl   = sum(get_pnl(r) for r in modern_rows if get_pnl(r) < 0)
    _modern_pf    = (
        _m_wins_pnl / abs(_m_loss_pnl)
        if _m_wins_pnl > 0 and _m_loss_pnl < 0 else None
    )
    if modern_count == 0:
        _scale_verdict = "NOT_PROVEN"
        _scale_reason  = "No modern full-metadata trades evaluated."
    elif normal_trade_count == 0:
        _scale_verdict = "NOT_PROVEN"
        _scale_reason  = (
            f"All {modern_count} modern trades are non-normal proof rows "
            f"({data_collection_count} data_collection, "
            f"{bootstrap_provisional_count} bootstrap provisional). "
            "Zero council-approved modern trades."
        )
    elif modern_count < 30:
        _scale_verdict = "DATA_COLLECTION_ONLY"
        _scale_reason  = f"Modern sample {modern_count}/30 minimum."
    elif normal_trade_count < 30:
        _scale_verdict = "DATA_COLLECTION_ONLY"
        _scale_reason  = f"Council-approved modern: {normal_trade_count}/30 minimum."
    elif (modern_roi <= 0
          or (modern_avg_clv or 0) <= 0
          or _modern_pf is None or _modern_pf <= 1.10
          or _overall_roi <= 0):
        _scale_verdict = "WATCHLIST"
        _scale_reason  = "Sample sufficient but performance gates not met."
    elif modern_count >= 100 and normal_trade_count >= 30:
        _scale_verdict = "SCALE_ELIGIBLE"
        _scale_reason  = "All gates met. Requires explicit human approval."
    else:
        _scale_verdict = "PAPER_VALIDATION_READY"
        _scale_reason  = f"Performance gates pass. Need {max(0, 100 - modern_count)} more modern trades."

    return {
        "modern_evaluated_rows": modern_count,
        "target_minimum": 30,
        "target_proof": 100,
        "proof_scope": "OUTCOME_KNOWN_SETTLED_ONLY",
        "time_exit_excluded_count": len(time_exit_rows),
        "data_quality": quality,
        "legacy_evaluated_rows": len(legacy_rows),
        "data_collection_count": data_collection_count,
        "data_collection_mode": DATA_COLLECTION_MODE,
        "bootstrap_provisional_count": bootstrap_provisional_count,
        "normal_trade_count": normal_trade_count,
        "modern_roi": modern_roi,
        "modern_pnl": round(modern_pnl, 2),
        "modern_avg_risk_edge": modern_avg_risk_edge,
        "modern_avg_clv": modern_avg_clv,
        "scale_verdict": _scale_verdict,
        "scale_verdict_reason": _scale_reason,
        "scale_allowed": False,
        "items": [
            {
                "key": "profitability",
                "state": profitability_state,
                "label": "Profitability",
                "value": f"Modern outcome-known ROI {modern_roi * 100:+.1f}% | {modern_count}/30 rows",
                "explanation": watch_reason or ("Modern ROI is positive" if profitability_state == "PASS" else "Modern ROI is not positive"),
            },
            {
                "key": "edge_validity",
                "state": edge_state,
                "label": "Model edge vs realized outcome",
                "value": (
                    f"avg risk_edge {modern_avg_risk_edge:+.4f} | ROI {modern_roi * 100:+.1f}%"
                    if modern_avg_risk_edge is not None else f"risk_edge unavailable | {modern_count}/30 rows"
                ),
                "explanation": watch_reason or ("Risk edge and outcome-known ROI agree" if edge_state == "PASS" else "Claimed risk edge is not translating to outcome-known ROI"),
            },
            {
                "key": "clv",
                "state": clv_state,
                "label": "Closing line / CLV",
                "value": (
                    f"avg CLV {modern_avg_clv:+.4f} | {modern_count}/30 rows"
                    if modern_avg_clv is not None else f"avg CLV unavailable | {modern_count}/30 rows"
                ),
                "explanation": watch_reason or ("Modern settled trades beat terminal line" if clv_state == "PASS" else "Modern settled trades are not beating terminal line"),
            },
        ],
    }


# ─────────────────────────────────────────
# TRUTH STATE HELPERS (Phase 6F — Control Room)
# ─────────────────────────────────────────

def classify_trade_proof_bucket(rec: dict) -> str:
    """Return proof bucket label for a single trade record."""
    if not CLEAN_TRUTH_OK:
        return "UNKNOWN"
    qg = _row_quality_group(rec)
    if qg == "LEGACY_EDGE_ONLY":
        return "LEGACY"
    if rec.get("data_collection_override"):
        return "DC_OVERRIDE"
    if rec.get("bootstrap_provisional"):
        return "PROVISIONAL"
    if rec.get("bootstrap_era_council_allow"):
        return "ERA_ALLOW"
    if qg == "MODERN_FULL_METADATA":
        return "NORMAL"
    return "PARTIAL"


def compute_bootstrap_path_state(all_records: list) -> dict:
    """Bootstrap path health assessment."""
    try:
        from config.trading_config import (
            BOOTSTRAP_ALLOW_ENABLED,
            BOOTSTRAP_MIN_EDGE,
            BOOTSTRAP_MIN_CONFIDENCE,
        )
    except ImportError:
        BOOTSTRAP_ALLOW_ENABLED = False
        BOOTSTRAP_MIN_EDGE = 0.05
        BOOTSTRAP_MIN_CONFIDENCE = 0.65

    era_allow_count = sum(1 for r in all_records if r.get("bootstrap_era_council_allow"))
    provisional_count = sum(1 for r in all_records if r.get("bootstrap_provisional"))
    recent = all_records[-20:] if len(all_records) >= 20 else all_records
    recent_era = sum(1 for r in recent if r.get("bootstrap_era_council_allow"))
    recent_prov = sum(1 for r in recent if r.get("bootstrap_provisional"))

    if not BOOTSTRAP_ALLOW_ENABLED:
        status = "DISABLED"
        detail = "BOOTSTRAP_ALLOW_ENABLED=False — era_allow path not active"
        action = "Set BOOTSTRAP_ALLOW_ENABLED=True in config/trading_config.py"
    elif era_allow_count > 0:
        status = "ALIVE"
        detail = f"{era_allow_count} era_allow trades confirmed (recent: {recent_era})"
        action = "Normal operation — era_allow trades counting toward proof"
    elif provisional_count > 0:
        status = "DEADLOCK"
        detail = (
            f"All {provisional_count} modern trades tagged PROVISIONAL — "
            "Dashboard running stale module cache"
        )
        action = "RESTART Dashboard: kill process, re-run python3 Dashboard.py"
    else:
        status = "NO_TRADES"
        detail = "No modern trades yet"
        action = "Waiting for first modern trade to flow through"

    return {
        "status": status,
        "enabled": BOOTSTRAP_ALLOW_ENABLED,
        "era_allow_count": era_allow_count,
        "provisional_count": provisional_count,
        "recent_era_allow": recent_era,
        "recent_provisional": recent_prov,
        "detail": detail,
        "action": action,
        "min_edge": BOOTSTRAP_MIN_EDGE,
        "min_confidence": BOOTSTRAP_MIN_CONFIDENCE,
    }


def compute_profitability_reality(clean_settled: list) -> dict:
    """Compute profitability reality panel data."""
    if not clean_settled or not PERFORMANCE_REPORT_OK:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "roi": None, "avg_clv": None, "win_rate": None,
            "breakeven_wr": None, "payoff_ratio": None,
            "avg_win": None, "avg_loss": None,
            "total_pnl": 0.0, "total_wagered": 0.0,
            "n": 0, "gates_passed": 0, "gates_total": 3,
            "gate_roi": False, "gate_clv": False, "gate_pf": False,
            "plain_english": "No settled trades yet.",
        }

    total_wagered = sum(get_size(r) for r in clean_settled)
    total_pnl = sum(get_pnl(r) for r in clean_settled)
    roi = total_pnl / total_wagered if total_wagered else None

    wins = [r for r in clean_settled if get_pnl(r) > 0]
    losses = [r for r in clean_settled if get_pnl(r) < 0]
    n = len(clean_settled)
    win_rate = len(wins) / n if n else None

    clv_vals = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None

    win_pnls = [get_pnl(r) for r in wins]
    loss_pnls = [get_pnl(r) for r in losses]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else None
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else None
    payoff_ratio = None
    if avg_win is not None and avg_loss is not None and avg_loss < 0:
        payoff_ratio = round(abs(avg_win / avg_loss), 4)

    breakeven_wr = None
    if avg_win is not None and avg_loss is not None and avg_loss < 0:
        breakeven_wr = round(abs(avg_loss) / (abs(avg_loss) + avg_win), 4)

    gate_roi = roi is not None and roi > 0
    gate_clv = avg_clv is not None and avg_clv > 0
    gate_pf = payoff_ratio is not None and payoff_ratio > 1.0
    gates_passed = sum([gate_roi, gate_clv, gate_pf])

    if n < 10:
        verdict = "INSUFFICIENT_DATA"
        plain = f"Only {n} settled trades. Need 30+ normal_modern for meaningful proof."
    elif gates_passed == 3:
        verdict = "POTENTIALLY_PROFITABLE"
        plain = f"All 3 gates pass at n={n}. Need 30+ clean normal_modern to confirm."
    elif gates_passed == 0:
        roi_str = f"{roi * 100:+.1f}%" if roi is not None else "n/a"
        clv_str = f"{avg_clv:+.4f}" if avg_clv is not None else "n/a"
        verdict = "NOT_PROFITABLE"
        plain = f"0/3 profitability gates pass. ROI={roi_str} CLV={clv_str}"
    else:
        verdict = "MIXED_SIGNALS"
        plain = f"{gates_passed}/3 gates pass. More normal_modern data needed."

    return {
        "verdict": verdict,
        "roi": round(roi, 4) if roi is not None else None,
        "avg_clv": avg_clv,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "breakeven_wr": breakeven_wr,
        "payoff_ratio": payoff_ratio,
        "avg_win": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 4) if avg_loss is not None else None,
        "total_pnl": round(total_pnl, 2),
        "total_wagered": round(total_wagered, 2),
        "n": n,
        "gates_passed": gates_passed,
        "gates_total": 3,
        "gate_roi": gate_roi,
        "gate_clv": gate_clv,
        "gate_pf": gate_pf,
        "plain_english": plain,
    }


def _compute_next_bottleneck(bootstrap: dict, proof_progress: dict, profitability: dict) -> dict:
    """Single most critical next action card."""
    nm = proof_progress.get("normal_modern", 0)
    bs = bootstrap.get("status", "UNKNOWN")

    if bs == "DEADLOCK":
        return {
            "priority": "CRITICAL",
            "action": "RESTART DASHBOARD",
            "reason": "Bootstrap path deadlocked — all trades going PROVISIONAL, zero count toward proof",
            "command": "Kill Dashboard (Ctrl+C) then: python3 Dashboard.py",
            "impact": "Future trades will get council_decision=ALLOW and count toward normal_modern",
        }
    if bs == "DISABLED":
        return {
            "priority": "HIGH",
            "action": "ENABLE BOOTSTRAP PATH",
            "reason": "BOOTSTRAP_ALLOW_ENABLED=False — era_allow path inactive",
            "command": "Set BOOTSTRAP_ALLOW_ENABLED=True in config/trading_config.py",
            "impact": "Enables signals to route through era_allow path toward proof",
        }
    if nm == 0 and bs in ("ALIVE", "NO_TRADES"):
        return {
            "priority": "HIGH",
            "action": "WAIT FOR ERA_ALLOW SETTLEMENTS",
            "reason": f"Bootstrap is active but 0 era_allow trades have settled",
            "command": "python3 tools/test_modern_only_proof.py",
            "impact": "Each settled era_allow trade increments normal_modern toward 10 (trust gate)",
        }
    if nm < 10:
        return {
            "priority": "HIGH",
            "action": f"COLLECT {10 - nm} MORE NORMAL TRADES (trust gate)",
            "reason": f"edge_profile_trusted requires 10 normal_modern (have {nm})",
            "command": "python3 tools/report_modern_only_proof.py",
            "impact": "Reaching 10 enables edge_profile_trusted=True, unlocks better council decisions",
        }
    if nm < 30:
        return {
            "priority": "MEDIUM",
            "action": f"COLLECT {30 - nm} MORE NORMAL TRADES (scale gate)",
            "reason": f"Proof gate requires 30 normal_modern (have {nm})",
            "command": "python3 tools/report_health.py",
            "impact": "Reaching 30 enables proof gate evaluation for scale readiness",
        }
    if profitability.get("gates_passed", 0) < 3:
        gp = profitability.get("gates_passed", 0)
        return {
            "priority": "MEDIUM",
            "action": "DIAGNOSE MODEL UNDERPERFORMANCE",
            "reason": f"Have {nm} normal trades but only {gp}/3 profitability gates pass",
            "command": "python3 tools/report_asymmetry_edge_inversion.py",
            "impact": "Identifies specific model/edge issues to fix before scale readiness",
        }
    return {
        "priority": "LOW",
        "action": "CONTINUE COLLECTING DATA",
        "reason": f"All 3 gates pass at n={nm}. Build statistical confidence.",
        "command": "python3 tools/report_health.py",
        "impact": "More data = higher confidence. Target 100 normal_modern for full proof.",
    }


def compute_dashboard_truth_state(all_records: list) -> dict:
    """Central truth state computation for the Control Room. Receives all_records to avoid double IO."""
    if not CLEAN_TRUTH_OK or not PERFORMANCE_REPORT_OK:
        return {"error": "required modules not available (clean_truth_report or performance_report)"}
    try:
        buckets = _classify_records(all_records)
        clean_settled = buckets.get("clean_settled", [])
        modern_full = [r for r in clean_settled if _row_quality_group(r) == "MODERN_FULL_METADATA"]
        legacy = [r for r in clean_settled if _row_quality_group(r) == "LEGACY_EDGE_ONLY"]
        dc_override = [r for r in modern_full if r.get("data_collection_override")]
        provisional_rows = [
            r for r in modern_full
            if r.get("bootstrap_provisional") and not r.get("data_collection_override")
        ]
        era_allow = [r for r in modern_full if r.get("bootstrap_era_council_allow")]
        normal_modern = [
            r for r in modern_full
            if not r.get("data_collection_override") and not r.get("bootstrap_provisional")
        ]

        proof_progress = {
            "clean_settled": len(clean_settled),
            "legacy": len(legacy),
            "modern_full": len(modern_full),
            "dc_override": len(dc_override),
            "provisional": len(provisional_rows),
            "era_allow": len(era_allow),
            "normal_modern": len(normal_modern),
            "target_trust": 10,
            "target_scale": 30,
            "target_proof": 100,
        }

        recent = all_records[-15:] if len(all_records) >= 15 else all_records
        trade_feed = []
        for rec in reversed(recent):
            pnl_val = get_pnl(rec)
            clv_val = get_clv(rec)
            trade_feed.append({
                "ticker": (rec.get("ticker") or "?")[:16],
                "status": rec.get("status", "?"),
                "bucket": classify_trade_proof_bucket(rec),
                "era_allow": bool(rec.get("bootstrap_era_council_allow")),
                "provisional": bool(rec.get("bootstrap_provisional")),
                "pnl": round(pnl_val, 2) if pnl_val is not None else None,
                "clv": round(clv_val, 4) if clv_val is not None else None,
            })

        bootstrap = compute_bootstrap_path_state(all_records)
        profitability = compute_profitability_reality(clean_settled)

        nm = len(normal_modern)
        avg_clv = profitability.get("avg_clv")
        pr = profitability.get("payoff_ratio")
        clv_str = f"{avg_clv:.4f}" if avg_clv is not None else "n/a"
        pr_str = f"{pr:.3f}" if pr is not None else "n/a"

        lockdown_reasons = [
            f"normal_modern={nm}/30 — proof base not established",
        ]
        if avg_clv is None or avg_clv <= 0:
            lockdown_reasons.append(f"avg_CLV={clv_str} — model does not show positive value")
        if pr is None or pr < 1.0:
            lockdown_reasons.append(f"payoff_ratio={pr_str} — structural asymmetry unresolved")
        lockdown_reasons.append("scale_allowed=False (hardcoded)")
        lockdown_reasons.append("real_money_allowed=False (hardcoded)")

        funnel = state.get("execution_funnel", {})
        machine_map = [
            {
                "key": "scanner",
                "label": "SCANNER",
                "icon": "📡",
                "active": BRAIN_OK,
                "detail": f"{len(state.get('opportunities', []))} opps",
            },
            {
                "key": "council",
                "label": "COUNCIL",
                "icon": "⚖️",
                "active": True,
                "detail": f"blocked={funnel.get('council_blocked', 0)}",
            },
            {
                "key": "trader",
                "label": "TRADER",
                "icon": "📝",
                "active": PAPER_TRADER_OK,
                "detail": f"opened={funnel.get('trade_opened', 0)}",
            },
            {
                "key": "proof",
                "label": "PROOF",
                "icon": "🔬",
                "active": True,
                "detail": f"n={nm}",
            },
            {
                "key": "readiness",
                "label": "READY",
                "icon": "🔒",
                "active": False,
                "detail": f"{nm}/30",
            },
        ]

        if bootstrap.get("status") == "DEADLOCK":
            system_verdict = "DEADLOCK"
        elif nm == 0 and bootstrap.get("status") == "NO_TRADES":
            system_verdict = "COLLECTING"
        elif nm == 0:
            system_verdict = "BOOTSTRAP_PENDING"
        elif nm < 10:
            system_verdict = "EARLY_DATA"
        elif nm < 30:
            system_verdict = "BUILDING_PROOF"
        elif profitability.get("gates_passed", 0) == 3:
            system_verdict = "PROOF_CANDIDATE"
        else:
            system_verdict = "WATCHLIST"

        bottleneck = _compute_next_bottleneck(bootstrap, proof_progress, profitability)

        return {
            "system_verdict": system_verdict,
            "proof_progress": proof_progress,
            "bootstrap_path": bootstrap,
            "trade_feed": trade_feed,
            "profitability": profitability,
            "lockdown_reasons": lockdown_reasons,
            "machine_map": machine_map,
            "next_bottleneck": bottleneck,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        import traceback
        return {
            "error": str(exc)[:200],
            "traceback": traceback.format_exc()[-500:],
        }


def update_market_history(opportunities: list, timestamp: str) -> None:
    history = state.setdefault("market_history", {})
    for opp in opportunities:
        ticker = opp.get("ticker")
        mid = _quote_mid(opp)
        if not ticker or mid is None:
            continue
        series = history.setdefault(ticker, [])
        if series and series[-1].get("timestamp") == timestamp:
            series[-1] = {"timestamp": timestamp, "mid": mid}
        else:
            series.append({"timestamp": timestamp, "mid": mid})
        del series[:-MARKET_HISTORY_LIMIT]


def build_market_visuals(active_trades: List[dict]) -> dict:
    opportunities = state.get("opportunities", [])
    quote_index = _current_quote_index()
    history = state.get("market_history", {})
    by_symbol = {}
    for opp in opportunities:
        symbol = _asset_symbol(opp)
        if symbol not in TRACKED_MARKET_SYMBOLS:
            continue
        current = by_symbol.get(symbol)
        if current is None or _safe_float(opp.get("volume")) > _safe_float(current.get("volume")):
            by_symbol[symbol] = opp

    market_strip = []
    for symbol in TRACKED_MARKET_SYMBOLS:
        opp = by_symbol.get(symbol)
        if not opp:
            continue
        ticker = opp.get("ticker")
        series = history.get(ticker, [])
        current_mid = _quote_mid(opp)
        previous_mid = series[-2]["mid"] if len(series) >= 2 else None
        change = (
            round(current_mid - previous_mid, 4)
            if current_mid is not None and previous_mid is not None else None
        )
        market_strip.append({
            "symbol": symbol,
            "ticker": ticker,
            "market_mid": current_mid,
            "change": change,
            "sparkline": [point["mid"] for point in series[-18:]],
            "action": opp.get("action"),
            "volume": opp.get("volume"),
            "spread": _quote_spread(opp),
            "last_update": series[-1]["timestamp"] if series else None,
            "history_points": len(series),
        })

    selected_trade = active_trades[0] if active_trades else None
    selected_quote = quote_index.get(selected_trade.get("ticker")) if selected_trade else None
    selected_ticker = selected_trade.get("ticker") if selected_trade else (market_strip[0]["ticker"] if market_strip else None)
    if not selected_quote and selected_ticker:
        selected_quote = quote_index.get(selected_ticker)
    selected_history = history.get(selected_ticker, []) if selected_ticker else []

    selected_panel = None
    if selected_ticker:
        selected_mid = _quote_mid(selected_quote or {})
        bet_reward_truth = build_bet_reward_truth(selected_trade, selected_mid) if selected_trade else None
        selected_panel = {
            "ticker": selected_ticker,
            "entry_price": selected_trade.get("entry_price") if selected_trade else None,
            "action": selected_trade.get("action") if selected_trade else selected_quote.get("action") if selected_quote else None,
            "market_mid": selected_mid,
            "yes_bid": (selected_quote or {}).get("yes_bid"),
            "yes_ask": (selected_quote or {}).get("yes_ask"),
            "no_bid": (selected_quote or {}).get("no_bid"),
            "no_ask": (selected_quote or {}).get("no_ask"),
            "spread": _quote_spread(selected_quote or {}),
            "close_time": (selected_trade or {}).get("close_time") or (selected_quote or {}).get("close_time"),
            "result_time": (selected_trade or {}).get("result_time") or (selected_quote or {}).get("result_time"),
            "history": [point["mid"] for point in selected_history[-36:]],
            "history_points": len(selected_history),
            "bet_reward_truth": bet_reward_truth,
        }

    quote_pressure = []
    for rec in active_trades:
        quote = quote_index.get(rec.get("ticker"), {})
        yes_bid = _safe_float(quote.get("yes_bid"), None)
        yes_ask = _safe_float(quote.get("yes_ask"), None)
        no_bid = _safe_float(quote.get("no_bid"), None)
        no_ask = _safe_float(quote.get("no_ask"), None)
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid is not None and yes_ask is not None else None
        no_mid = (no_bid + no_ask) / 2 if no_bid is not None and no_ask is not None else None
        imbalance = (
            round(yes_mid - no_mid, 4)
            if yes_mid is not None and no_mid is not None else None
        )
        quote_pressure.append({
            "ticker": rec.get("ticker"),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "spread": _quote_spread(quote),
            "imbalance": imbalance,
        })

    return {
        "market_strip": market_strip,
        "selected_market": selected_panel,
        "quote_pressure": quote_pressure,
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
    time_exits = [r for r in all_records if _is_time_exit_record(r)]

    wins = [r for r in clean_settled if get_pnl(r) > 0]
    losses = [r for r in clean_settled if get_pnl(r) < 0]
    pushes = [r for r in clean_settled if get_pnl(r) == 0]
    total_pnl = sum(get_pnl(r) for r in clean_settled)
    total_wagered = sum(get_size(r) for r in clean_settled)
    conf_vals = [_safe_float(r.get("confidence")) for r in clean_settled if r.get("confidence") is not None]
    edge_vals = [_safe_float(r.get("edge")) for r in clean_settled if r.get("edge") is not None]
    clv_vals = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    time_exit_pnl = sum(get_pnl(r) for r in time_exits)
    time_exit_wagered = sum(get_size(r) for r in time_exits)
    time_exit_clv_vals = [v for v in (get_clv(r) for r in time_exits) if v is not None]
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
        quote = quote_index.get(rec.get("ticker"), {})
        if mark["unrealized_pnl"] is not None:
            unrealized_vals.append(mark["unrealized_pnl"])
        active_trade_cards.append({
            "timestamp": rec.get("timestamp"),
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
            "current_market_mid": _quote_mid(quote),
            "yes_bid": quote.get("yes_bid"),
            "yes_ask": quote.get("yes_ask"),
            "no_bid": quote.get("no_bid"),
            "no_ask": quote.get("no_ask"),
            "spread": _quote_spread(quote),
            "close_time": rec.get("close_time"),
            "result_time": rec.get("result_time"),
            "overdue_minutes": _overdue_minutes(rec.get("result_time")),
            "bet_reward_truth": build_bet_reward_truth(rec, mark["mark_price"]),
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

    proof_checklist = build_proof_checklist(clean_settled, time_exits)
    try:
        edge_profile = json.loads((ROOT / "data" / "edge_profile.json").read_text())
        edge_health = edge_profile.get("edge_profile_health") or {}
    except Exception:
        edge_profile = {}
        edge_health = {}

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
        "time_exit": {
            "count": len(time_exits),
            "pnl": round(time_exit_pnl, 2),
            "total_wagered": round(time_exit_wagered, 2),
            "roi": round(time_exit_pnl / time_exit_wagered, 4) if time_exit_wagered else 0.0,
            "avg_clv": round(sum(time_exit_clv_vals) / len(time_exit_clv_vals), 4)
            if time_exit_clv_vals else None,
            "proof_note": "FORCED_CLOSE/TIME_EXIT rows are mid-price exits, not event-outcome proof.",
        },
        "blended": {
            "settled_plus_time_exit_pnl": round(total_pnl + time_exit_pnl, 2),
            "warning": "Blended P&L mixes outcome-known SETTLED rows with TIME_EXIT marks.",
        },
        "sizing_mode": {
            "global_forced_learning_mode": GLOBAL_FORCED_LEARNING_MODE,
            "kelly_sizing_used": not GLOBAL_FORCED_LEARNING_MODE,
            "data_collection_mode": DATA_COLLECTION_MODE,
            "message": (
                "Kelly is calculated/logged for audit only; actual entries are forced to learning size."
                if GLOBAL_FORCED_LEARNING_MODE
                else "Kelly-derived sizing path is enabled."
            ),
        },
        "system_truth_summary": {
            "system_mode": "RESEARCH_ONLY",
            "proof_verdict": proof_checklist.get("scale_verdict", "UNKNOWN"),
            "proof_reason": proof_checklist.get("scale_verdict_reason", ""),
            "scale_allowed": False,
            "real_money_allowed": False,
            "edge_profile_trusted": edge_health.get("edge_profile_trusted", False),
            "edge_profile_reason": edge_health.get("reason"),
            "data_collection_mode": DATA_COLLECTION_MODE,
            "global_forced_learning_mode": GLOBAL_FORCED_LEARNING_MODE,
            "kelly_sizing_used": not GLOBAL_FORCED_LEARNING_MODE,
            "clean_settled_count": len(clean_settled),
            "modern_full_count": proof_checklist.get("modern_evaluated_rows", 0),
            "normal_modern_count": proof_checklist.get("normal_trade_count", 0),
            "data_collection_override_count": proof_checklist.get("data_collection_count", 0),
            "bootstrap_provisional_count": proof_checklist.get("bootstrap_provisional_count", 0),
            "time_exit_excluded_count": len(time_exits),
            "warning": "Research-only. Current samples are not proof; no scaling or real money.",
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
        "proof_checklist": proof_checklist,
        "market_visuals": build_market_visuals(active_trade_cards),
        "truth_state": compute_dashboard_truth_state(all_records),
        "generated_at": datetime.now(timezone.utc).isoformat(),
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

    snapshot_stats = state.get("btc15m_snapshot_stats") or {}
    if snapshot_stats:
        btc_ok = snapshot_stats.get("btc_price_present")
        events.append(_live_event(
            "snapshot",
            "SUCCESS" if btc_ok else "WARN",
            (
                f"BTC15M snapshots seen={snapshot_stats.get('seen', 0)} "
                f"matched={snapshot_stats.get('matched', 0)} "
                f"written={snapshot_stats.get('written', 0)} "
                f"btc_sync={'active' if btc_ok else 'missing'} "
                f"latency={snapshot_stats.get('btc_price_latency_ms', 'n/a')}ms"
            ),
            snapshot_stats.get("last_updated"),
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
    "market_history": {},
    "market_visuals": {},
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
                update_market_history(opportunities, now)
                if SCANNER_OPPORTUNITY_LOGGER_OK:
                    scanner_log_stats = log_scanner_opportunities(
                        opportunities,
                        scan_id=f"dashboard_scan_{state['total_scans']}",
                        run_id=DASHBOARD_RUN_ID,
                        source="dashboard",
                    )
                    state["scanner_opportunity_stats"] = {
                        **scanner_log_stats,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }
                    if scanner_log_stats.get("written") or scanner_log_stats.get("errors"):
                        print(
                            "[SCANNER_OPPORTUNITY_LOG] "
                            f"seen={scanner_log_stats.get('seen', 0)} "
                            f"written={scanner_log_stats.get('written', 0)} "
                            f"errors={scanner_log_stats.get('errors', 0)} "
                            f"actions={scanner_log_stats.get('action_counts', {})}"
                        )
                if PAYOFF_AWARE_SHADOW_LOGGER_OK:
                    payoff_shadow_stats = log_payoff_aware_shadow_ranking(
                        opportunities,
                        scan_id=f"dashboard_scan_{state['total_scans']}",
                        run_id=DASHBOARD_RUN_ID,
                    )
                    state["payoff_aware_shadow_ranking_stats"] = {
                        **payoff_shadow_stats,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }
                    if payoff_shadow_stats.get("written") or payoff_shadow_stats.get("errors"):
                        print(
                            "[PAYOFF_AWARE_SHADOW] "
                            f"written={payoff_shadow_stats.get('written', 0)} "
                            f"errors={payoff_shadow_stats.get('errors', 0)} "
                            f"candidates={payoff_shadow_stats.get('candidate_count', 0)} "
                            f"strict_starved={payoff_shadow_stats.get('strict_starvation_count', 0)}"
                        )
                if SIDE_COVERAGE_QUEUE_OK:
                    open_count_for_shadow = len(paper_trader.open_trades) if paper_trader else None
                    shadow_row = select_shadow_candidate(
                        opportunities,
                        scan_id=f"dashboard_scan_{state['total_scans']}",
                        run_id=DASHBOARD_RUN_ID,
                        open_count=open_count_for_shadow,
                        max_open_trades=MAX_CONCURRENT_OPEN_TRADES,
                    )
                    if shadow_row.get("final_reason") != "SIDE_COVERAGE_DISABLED":
                        side_coverage_stats = log_shadow_diagnostic(shadow_row)
                    else:
                        side_coverage_stats = {"written": 0, "errors": 0}
                    state["side_coverage_shadow_stats"] = {
                        **side_coverage_stats,
                        "final_reason": shadow_row.get("final_reason"),
                        "ticker": shadow_row.get("ticker"),
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }
                    if side_coverage_stats.get("written") or side_coverage_stats.get("errors"):
                        print(
                            "[SIDE_COVERAGE_SHADOW] "
                            f"written={side_coverage_stats.get('written', 0)} "
                            f"errors={side_coverage_stats.get('errors', 0)} "
                            f"reason={shadow_row.get('final_reason')} "
                            f"ticker={shadow_row.get('ticker')}"
                        )
                if BTC15M_SNAPSHOT_LOGGER_OK:
                    btc_price_snapshot = None
                    if BTC_PRICE_CLIENT_OK:
                        try:
                            btc_price_snapshot = fetch_btc_usd_price()
                        except Exception as e:
                            btc_price_snapshot = {
                                "symbol": "BTC-USD",
                                "price": None,
                                "source": "coinbase_exchange_public_ticker",
                                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                "fetch_latency_ms": None,
                                "error": e.__class__.__name__,
                            }
                    snapshot_stats = log_btc15m_snapshots(
                        opportunities,
                        scan_id=f"dashboard_scan_{state['total_scans']}",
                        btc_price_snapshot=btc_price_snapshot,
                    )
                    state["btc15m_snapshot_stats"] = {
                        **snapshot_stats,
                        "btc_price_present": bool((btc_price_snapshot or {}).get("price")),
                        "btc_price_latency_ms": (btc_price_snapshot or {}).get("fetch_latency_ms"),
                        "btc_price_error": (btc_price_snapshot or {}).get("error"),
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }
                    if snapshot_stats.get("written") or snapshot_stats.get("errors"):
                        print(
                            "[BTC15M_SNAPSHOT] "
                            f"seen={snapshot_stats.get('seen', 0)} "
                            f"matched={snapshot_stats.get('matched', 0)} "
                            f"written={snapshot_stats.get('written', 0)} "
                            f"errors={snapshot_stats.get('errors', 0)}"
                        )

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
                    rank_context_by_id = {}
                    first_bet_yes_rank_in_scan = None
                    first_bet_no_rank_in_scan = None
                    scan_non_pass_rank = 0
                    for opportunity_rank, ranked_opp in enumerate(opportunities, start=1):
                        scanner_action = str(ranked_opp.get("action") or "").upper()
                        if scanner_action == "PASS":
                            continue
                        scan_non_pass_rank += 1
                        if scanner_action == "BET_YES" and first_bet_yes_rank_in_scan is None:
                            first_bet_yes_rank_in_scan = scan_non_pass_rank
                        if scanner_action == "BET_NO" and first_bet_no_rank_in_scan is None:
                            first_bet_no_rank_in_scan = scan_non_pass_rank
                        rank_context_by_id[id(ranked_opp)] = {
                            "opportunity_rank": opportunity_rank,
                            "scan_non_pass_rank": scan_non_pass_rank,
                        }

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
                            open_count_before = len(paper_trader.open_trades)
                            open_slots_before = max(0, MAX_CONCURRENT_OPEN_TRADES - open_count_before)
                            funnel["entered_paper_trader"] += 1
                            trade, trace_text = call_paper_trader_with_trace(
                                market_data=market_data,
                                estimated_prob=estimated_prob,
                                strategy=strategy_label,
                                intended_action=opp.get("action"),
                            )
                            trace_counts = classify_execution_trace(trace_text, trade)
                            for key, value in trace_counts.items():
                                funnel[key] += value
                            if EXECUTION_FUNNEL_LOGGER_OK:
                                rank_context = {
                                    **rank_context_by_id.get(id(opp), {}),
                                    "open_slots_before": open_slots_before,
                                    "open_count_before": open_count_before,
                                    "max_open_trades": MAX_CONCURRENT_OPEN_TRADES,
                                    "cap_already_full": open_count_before >= MAX_CONCURRENT_OPEN_TRADES,
                                    "first_bet_no_rank_in_scan": first_bet_no_rank_in_scan,
                                    "first_bet_yes_rank_in_scan": first_bet_yes_rank_in_scan,
                                }
                                funnel_log_stats = log_execution_funnel(
                                    opportunity=opp,
                                    scan_id=f"dashboard_scan_{state['total_scans']}",
                                    run_id=DASHBOARD_RUN_ID,
                                    trace_text=trace_text,
                                    trade=trade,
                                    trace_counts=trace_counts,
                                    rank_context=rank_context,
                                )
                                state["execution_funnel_log_stats"] = {
                                    **funnel_log_stats,
                                    "last_updated": datetime.now(timezone.utc).isoformat(),
                                }
                    
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
                update_market_history(state["opportunities"], now)
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
  display: grid;
  grid-template-columns: 62px 1fr;
  gap: 7px;
  align-items: start;
}
.event-row.info { border-left: 2px solid var(--green-dim); }
.event-row.success { border-left: 2px solid var(--green2); background: rgba(0,255,65,0.025); }
.event-row.warn { border-left: 2px solid var(--yellow); background: rgba(255,214,0,0.025); }
.event-row.error { border-left: 2px solid var(--red); background: rgba(255,23,68,0.04); }
.event-top {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 3px;
  color: var(--muted);
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.event-source { color: var(--cyan); }
.event-time { color: var(--green-dim); }
.event-level.info { color: var(--muted); }
.event-level.success { color: var(--green2); }
.event-level.warn { color: var(--yellow); }
.event-level.error { color: var(--red); }
.event-msg { color: var(--text); }

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
  position: relative;
  overflow: hidden;
}
.stat-cell::after {
  content: '';
  position: absolute;
  inset: auto 10px 0 10px;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(0,255,65,0.45), transparent);
  opacity: 0.45;
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
.metric-flash {
  animation: metricFlash 0.7s ease-out;
}
@keyframes metricFlash {
  0%   { background: rgba(0,255,65,0.16); box-shadow: inset 0 0 18px rgba(0,255,65,0.18); }
  100% { background: var(--panel); box-shadow: none; }
}
.metric-flash-down {
  animation: metricFlashDown 0.7s ease-out;
}
@keyframes metricFlashDown {
  0%   { background: rgba(255,23,68,0.14); box-shadow: inset 0 0 18px rgba(255,23,68,0.18); }
  100% { background: var(--panel); box-shadow: none; }
}
.metric-section-label {
  padding: 7px 10px;
  font-size: 8px;
  color: var(--cyan);
  letter-spacing: 2px;
  text-transform: uppercase;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid rgba(13,40,13,0.6);
  background: rgba(0,229,255,0.025);
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.metric-section-label span:last-child { color: var(--muted); text-align: right; }

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
.verdict-icon.watch { color: var(--yellow); }
.verdict-value { color: var(--muted); font-size: 9px; margin-left: auto; text-align: right; }

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
.market-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  background: var(--border);
  min-height: 106px;
}
.market-tile {
  background: var(--panel);
  padding: 9px 9px;
  min-width: 0;
  position: relative;
  overflow: hidden;
}
.market-tile.live-fresh::before {
  content: '';
  position: absolute;
  top: 6px;
  right: 7px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--green2);
  box-shadow: 0 0 10px var(--green2);
}
.market-tile.changed { animation: tilePulse 0.75s ease-out; }
@keyframes tilePulse {
  0% { box-shadow: inset 0 0 22px rgba(0,229,255,0.22); }
  100% { box-shadow: inset 0 0 0 rgba(0,229,255,0); }
}
.market-tile-top {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  align-items: center;
}
.market-symbol { color: var(--cyan); font-weight: 700; font-size: 10px; }
.market-price {
  color: var(--green2);
  font-size: 14px;
  font-family: 'Orbitron', monospace;
  text-shadow: 0 0 10px rgba(0,255,65,0.4);
}
.market-change.good { color: var(--green2); }
.market-change.bad { color: var(--red); }
.market-meta-line {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  color: var(--muted);
  font-size: 8px;
  margin-top: 4px;
}
.sparkline {
  height: 44px;
  display: flex;
  align-items: end;
  gap: 2px;
  margin-top: 5px;
  border-top: 1px solid rgba(13,40,13,0.6);
}
.sparkbar {
  flex: 1;
  min-width: 2px;
  background: var(--green-dim);
  box-shadow: 0 0 4px rgba(0,255,65,0.18);
}
.line-chart {
  height: 132px;
  display: flex;
  align-items: end;
  gap: 3px;
  padding: 10px 0 5px;
  border-bottom: 1px solid rgba(13,40,13,0.55);
  background:
    linear-gradient(180deg, rgba(0,255,65,0.035), transparent),
    repeating-linear-gradient(0deg, transparent, transparent 21px, rgba(13,40,13,0.65) 22px);
}
.chartbar {
  flex: 1;
  min-width: 3px;
  background: var(--cyan);
  opacity: 0.78;
  box-shadow: 0 0 5px rgba(0,229,255,0.25);
}
.entry-line-note {
  color: var(--yellow);
  font-size: 8px;
  letter-spacing: 1px;
  margin-top: 4px;
}
.selected-market-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: rgba(13,40,13,0.65);
  margin-bottom: 8px;
}
.selected-metric {
  background: var(--panel);
  padding: 7px 8px;
}
.selected-metric-label {
  color: var(--muted);
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.selected-metric-value {
  color: var(--green2);
  font-family: 'Orbitron', monospace;
  font-size: 13px;
  margin-top: 3px;
}
.bet-truth-panel {
  border: 1px solid var(--border);
  background: rgba(0,0,0,0.18);
  margin: 7px 0;
  padding: 7px 8px;
}
.bet-truth-title {
  color: var(--cyan);
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-bottom: 5px;
}
.bet-truth-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: rgba(13,40,13,0.55);
}
.bet-truth-cell {
  background: var(--panel);
  padding: 6px 7px;
  min-width: 0;
}
.bet-truth-label {
  color: var(--muted);
  font-size: 7px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.bet-truth-value {
  color: var(--text);
  font-family: 'Orbitron', monospace;
  font-size: 10px;
  margin-top: 2px;
  overflow-wrap: anywhere;
}
.bet-truth-warning {
  color: var(--yellow);
  font-size: 8px;
  line-height: 1.4;
  margin-top: 6px;
  border-top: 1px solid rgba(255,214,0,0.25);
  padding-top: 5px;
}
.time-progress-wrap {
  height: 6px;
  background: var(--dim);
  margin: 7px 0 3px;
  overflow: hidden;
}
.time-progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--green-dim), var(--yellow));
  box-shadow: 0 0 8px rgba(255,214,0,0.28);
}
.pressure-bar-wrap {
  height: 5px;
  background: var(--dim);
  margin-top: 4px;
  overflow: hidden;
}
.pressure-bar {
  height: 100%;
  background: var(--green2);
}
.pressure-bar.bad { background: var(--red); }

/* ── CONTROL ROOM SECTIONS ── */
.cr-section {
  border-top: 1px solid var(--border);
  padding: 7px 10px;
}
.cr-section-title {
  font-family: 'Orbitron', monospace;
  font-size: 7px;
  letter-spacing: 2px;
  color: var(--cyan);
  text-transform: uppercase;
  margin-bottom: 5px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.cr-section-title::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}
.cr-locked { background: rgba(255,23,68,0.02); }
.cr-warn-bg { background: rgba(255,214,0,0.02); }
.cr-bottleneck { background: rgba(255,214,0,0.025); }

/* ── EDGE PROFILE FRESHNESS PANEL (Phase 9D) ── */
.fp-fresh  { color: var(--green2); font-weight: 600; }
.fp-watch  { color: var(--yellow); font-weight: 600; }
.fp-danger { color: var(--red);    font-weight: 600; }
.fp-cell-row {
  display: grid;
  grid-template-columns: 110px 30px 46px 52px 1fr;
  gap: 2px;
  padding: 2px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  align-items: center;
  font-size: 7px;
}
.fp-cell-hdr { color: var(--dim); font-size: 6px; padding-bottom: 2px; }
.fp-warn-box {
  background: rgba(255,23,68,0.06);
  border: 1px solid rgba(255,23,68,0.25);
  padding: 3px 6px;
  font-size: 7px;
  color: var(--red);
  margin-top: 4px;
  word-break: break-word;
}
.fp-ok-box {
  background: rgba(0,255,65,0.05);
  border: 1px solid rgba(0,255,65,0.2);
  padding: 3px 6px;
  font-size: 7px;
  color: var(--green2);
  margin-top: 4px;
}

/* Verdict pills */
.verdict-pills { display: flex; flex-wrap: wrap; gap: 3px; padding: 3px 0; }
.verdict-pill {
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  padding: 4px 7px 3px;
  border-radius: 3px;
  font-size: 8px;
  font-family: 'Orbitron', monospace;
  font-weight: 700;
  letter-spacing: 0.5px;
  min-width: 54px;
  text-align: center;
}
.vp-val { font-size: 9px; line-height: 1.2; }
.vp-key { font-size: 6px; opacity: 0.6; letter-spacing: 1px; margin-top: 1px; }
.vp-locked { background: rgba(255,23,68,0.14); border: 1px solid rgba(255,23,68,0.5); color: var(--red); }
.vp-ok     { background: rgba(0,255,65,0.09);  border: 1px solid rgba(0,255,65,0.4);  color: var(--green2); }
.vp-warn   { background: rgba(255,214,0,0.1);  border: 1px solid rgba(255,214,0,0.4); color: var(--yellow); }
.vp-info   { background: rgba(0,229,255,0.07); border: 1px solid rgba(0,229,255,0.3); color: var(--cyan); }

/* Machine map */
.machine-map {
  display: flex;
  align-items: center;
  overflow-x: auto;
  padding: 3px 0;
  gap: 0;
}
.machine-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 48px;
  padding: 4px 2px;
}
.mn-icon { font-size: 13px; line-height: 1; }
.mn-label {
  font-size: 6px;
  color: var(--muted);
  letter-spacing: 1px;
  margin-top: 2px;
  text-transform: uppercase;
  font-family: 'Orbitron', monospace;
}
.mn-detail { font-size: 7px; margin-top: 1px; }
.mn-active { color: var(--green2); }
.mn-locked { color: var(--red); }
.mn-warn   { color: var(--yellow); }
.mn-off    { color: var(--muted); }
.machine-arrow { color: var(--border); font-size: 10px; margin: 0 1px; padding-bottom: 10px; flex-shrink: 0; }

/* Gate progress bars */
.gate-row { padding: 3px 0; }
.gate-meta {
  display: flex;
  justify-content: space-between;
  font-size: 8px;
  margin-bottom: 2px;
}
.gate-name { color: var(--muted); }
.gate-val  { color: var(--text); }
.gate-track {
  height: 4px;
  background: var(--dim);
  border-radius: 2px;
  overflow: hidden;
}
.gate-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--green2);
  transition: width 0.6s ease;
}
.gate-fill.yellow { background: var(--yellow); }
.gate-fill.red    { background: var(--red); }

/* Bootstrap status */
.bs-badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 2px;
  font-family: 'Orbitron', monospace;
  font-size: 8px;
  font-weight: 700;
  letter-spacing: 1px;
}
.bs-alive    { background: rgba(0,255,65,0.1);  color: var(--green2); border: 1px solid var(--green2); }
.bs-deadlock { background: rgba(255,23,68,0.13); color: var(--red);    border: 1px solid var(--red); }
.bs-disabled { background: rgba(13,40,13,0.5);  color: var(--muted);  border: 1px solid var(--border); }
.bs-pending  { background: rgba(255,214,0,0.1); color: var(--yellow); border: 1px solid var(--yellow); }
.bs-detail   { font-size: 8px; color: var(--muted); margin-top: 3px; line-height: 1.5; }
.bs-action {
  font-size: 9px;
  color: var(--text);
  margin-top: 4px;
  padding: 4px 6px;
  background: rgba(255,214,0,0.04);
  border-left: 2px solid var(--yellow);
  line-height: 1.5;
}

/* Trade truth feed */
.tth { display: grid; grid-template-columns: 88px 68px 50px 48px; gap: 0; padding: 2px 0; }
.tth-head { font-size: 7px; color: var(--muted); letter-spacing: 1px; text-transform: uppercase; border-bottom: 1px solid var(--border); margin-bottom: 2px; }
.ttr { border-bottom: 1px solid rgba(13,40,13,0.35); }
.ttr-ticker { color: var(--cyan); font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ttr-bucket { font-family: 'Orbitron', monospace; font-size: 7px; }
.bkt-LEGACY      { color: var(--red); }
.bkt-DC_OVERRIDE { color: var(--yellow); }
.bkt-PROVISIONAL { color: var(--yellow); }
.bkt-ERA_ALLOW   { color: var(--green2); }
.bkt-NORMAL      { color: var(--green2); }
.bkt-PARTIAL     { color: var(--muted); }
.bkt-UNKNOWN     { color: var(--muted); }
.ttr-status { color: var(--muted); font-size: 8px; }
.ttr-pnl    { font-size: 9px; text-align: right; }

/* Profitability */
.prof-gate { display: flex; align-items: center; gap: 7px; padding: 3px 0; font-size: 9px; border-bottom: 1px solid rgba(13,40,13,0.3); }
.pg-icon   { width: 12px; text-align: center; font-size: 10px; }
.pg-icon.pass { color: var(--green2); }
.pg-icon.fail { color: var(--red); }
.pg-label  { color: var(--muted); flex: 1; font-size: 8px; }
.pg-val    { color: var(--text); text-align: right; font-size: 9px; }
.prof-verdict {
  margin-top: 5px;
  padding: 5px 6px;
  font-size: 9px;
  line-height: 1.5;
  border-left: 2px solid var(--red);
  background: rgba(255,23,68,0.03);
}
.prof-verdict.ok { border-left-color: var(--green2); background: rgba(0,255,65,0.03); }
.prof-verdict.warn { border-left-color: var(--yellow); background: rgba(255,214,0,0.03); }

/* Asymmetry equation */
.asym-eq { font-size: 9px; padding: 4px 0; line-height: 2; }
.asym-pos { color: var(--green2); }
.asym-neg { color: var(--red); }
.asym-dim { color: var(--muted); font-size: 8px; }
.asym-gap { color: var(--yellow); font-weight: 700; }

/* Lockdown */
.lock-header {
  font-family: 'Orbitron', monospace;
  font-size: 13px;
  font-weight: 900;
  color: var(--red);
  text-shadow: 0 0 12px rgba(255,23,68,0.45);
  text-align: center;
  padding: 5px 0 3px;
  letter-spacing: 2px;
}
.lock-reason {
  font-size: 8px;
  color: var(--muted);
  padding: 3px 0;
  border-bottom: 1px solid rgba(255,23,68,0.12);
  display: flex;
  gap: 6px;
  align-items: baseline;
}
.lock-reason::before { content: '⛔'; font-size: 8px; color: var(--red); flex-shrink: 0; }

/* Scale readiness */
.scale-no {
  font-family: 'Orbitron', monospace;
  font-size: 12px;
  font-weight: 900;
  color: var(--red);
  padding: 2px 0 4px;
  letter-spacing: 1px;
}
.scale-blocker {
  font-size: 8px;
  color: var(--muted);
  padding: 2px 0;
  border-bottom: 1px solid rgba(13,40,13,0.4);
}
.scale-blocker::before { content: '▸ '; color: var(--red); }

/* Bottleneck card */
.bn-card {
  padding: 7px 8px;
  border: 1px solid rgba(255,214,0,0.4);
  background: rgba(255,214,0,0.035);
  border-radius: 2px;
}
.bn-priority {
  font-family: 'Orbitron', monospace;
  font-size: 7px;
  color: var(--yellow);
  letter-spacing: 2px;
}
.bn-action { font-size: 10px; color: var(--text); font-weight: 700; margin: 3px 0 2px; }
.bn-reason { font-size: 8px; color: var(--muted); }
.bn-command { font-size: 8px; color: var(--cyan); margin-top: 4px; padding-top: 4px; border-top: 1px solid var(--border); }
.bn-impact  { font-size: 8px; color: var(--green2); margin-top: 2px; }
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
  <div class="col" style="display:grid;grid-template-rows:auto 0.7fr auto auto auto auto auto 0.9fr auto 1.1fr">

    <!-- ARB OPPORTUNITIES -->
    <div class="ph">
      <span class="ph-title">🔥 ARB Opportunities</span>
      <span class="ph-sub" id="arb-label">YES+NO &lt; $1</span>
    </div>
    <div class="pb" id="arb-feed">
      <div class="empty">No arb found yet. Scanning...</div>
    </div>

    <!-- LIVE MARKET STRIP -->
    <div class="ph" style="margin-top:1px">
      <span class="ph-title">Live Market Strip</span>
      <span class="ph-sub" id="market-strip-label">real scanner quotes</span>
    </div>
    <div id="market-strip" class="market-strip">
      <div class="empty">Waiting for scanner quotes...</div>
    </div>

    <!-- SELECTED MARKET CHART -->
    <div class="ph" style="margin-top:1px">
      <span class="ph-title">Selected Market / Open Position</span>
      <span class="ph-sub" id="selected-market-label">--</span>
    </div>
    <div class="mini-section" id="selected-market-panel">
      <div class="empty">No selected market yet.</div>
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

  <!-- COL 3: TRADING RESEARCH CONTROL ROOM -->
  <div class="col" id="control-room">
    <div class="ph">
      <span class="ph-title">🔬 RESEARCH CONTROL ROOM</span>
      <span class="ph-sub" id="cr-verdict-label" style="color:var(--yellow)">COLLECTING</span>
    </div>

    <div class="pb" style="padding:0;min-height:0">

      <!-- 1. SYSTEM VERDICT HEADER -->
      <div class="cr-section">
        <div class="cr-section-title">System Verdict</div>
        <div class="verdict-pills" id="cr-verdict-pills">
          <span class="verdict-pill vp-locked"><span class="vp-val">🔒 LOCKED</span><span class="vp-key">REAL $</span></span>
          <span class="verdict-pill vp-locked"><span class="vp-val">❌ 0/30</span><span class="vp-key">SCALE</span></span>
          <span class="verdict-pill vp-locked"><span class="vp-val">❌ 0/3</span><span class="vp-key">PROFIT</span></span>
          <span class="verdict-pill vp-warn"><span class="vp-val">⚠️ PENDING</span><span class="vp-key">BOOTSTRAP</span></span>
          <span class="verdict-pill vp-info"><span class="vp-val">📊 n=0</span><span class="vp-key">PROOF</span></span>
        </div>
      </div>

      <!-- 2. 5-LEVEL MACHINE MAP -->
      <div class="cr-section">
        <div class="cr-section-title">5-Level Machine Map</div>
        <div class="machine-map" id="cr-machine-map">
          <div class="machine-node"><div class="mn-icon">📡</div><div class="mn-label">SCANNER</div><div class="mn-detail mn-active">--</div></div>
          <div class="machine-arrow">→</div>
          <div class="machine-node"><div class="mn-icon">⚖️</div><div class="mn-label">COUNCIL</div><div class="mn-detail mn-active">--</div></div>
          <div class="machine-arrow">→</div>
          <div class="machine-node"><div class="mn-icon">📝</div><div class="mn-label">TRADER</div><div class="mn-detail mn-active">--</div></div>
          <div class="machine-arrow">→</div>
          <div class="machine-node"><div class="mn-icon">🔬</div><div class="mn-label">PROOF</div><div class="mn-detail mn-active">--</div></div>
          <div class="machine-arrow">→</div>
          <div class="machine-node"><div class="mn-icon">🔒</div><div class="mn-label">READY</div><div class="mn-detail mn-locked">LOCKED</div></div>
        </div>
      </div>

      <!-- 3. PROOF PATH PANEL -->
      <div class="cr-section">
        <div class="cr-section-title">Proof Path</div>
        <div id="cr-proof-path">
          <div class="gate-row">
            <div class="gate-meta"><span class="gate-name">clean_settled</span><span class="gate-val" id="pp-settled">0 / 100</span></div>
            <div class="gate-track"><div class="gate-fill" id="pp-settled-bar" style="width:0%"></div></div>
          </div>
          <div class="gate-row">
            <div class="gate-meta"><span class="gate-name">modern_full_metadata</span><span class="gate-val" id="pp-modern">0 / 100</span></div>
            <div class="gate-track"><div class="gate-fill yellow" id="pp-modern-bar" style="width:0%"></div></div>
          </div>
          <div class="gate-row">
            <div class="gate-meta"><span class="gate-name">normal_modern (proof base)</span><span class="gate-val" id="pp-normal">0 / 30</span></div>
            <div class="gate-track"><div class="gate-fill red" id="pp-normal-bar" style="width:0%"></div></div>
          </div>
          <div style="margin-top:5px;font-size:8px;color:var(--muted)" id="pp-bucket-detail">
            dc_override=? | provisional=? | era_allow=? | legacy=? [excluded]
          </div>
        </div>
      </div>

      <!-- 4. BOOTSTRAP PATH MONITOR -->
      <div class="cr-section" id="cr-bootstrap-section">
        <div class="cr-section-title">Bootstrap Path Monitor</div>
        <div id="cr-bootstrap">
          <span class="bs-badge bs-pending">LOADING...</span>
          <div class="bs-detail">Checking bootstrap path state...</div>
        </div>
      </div>

      <!-- 5. LIVE TRADE TRUTH FEED -->
      <div class="cr-section">
        <div class="cr-section-title">Live Trade Truth Feed <span style="color:var(--muted);font-size:7px" id="cr-feed-count"></span></div>
        <div class="tth tth-head">
          <span>TICKER</span><span>BUCKET</span><span>STATUS</span><span style="text-align:right">P&amp;L</span>
        </div>
        <div id="cr-trade-feed">
          <div class="empty">No trades loaded yet.</div>
        </div>
      </div>

      <!-- 6. PROFITABILITY REALITY -->
      <div class="cr-section">
        <div class="cr-section-title">Profitability Reality</div>
        <div id="cr-profitability">
          <div class="prof-gate">
            <span class="pg-icon fail" id="pg-roi-icon">✗</span>
            <span class="pg-label">ROI &gt; 0%</span>
            <span class="pg-val" id="pg-roi-val">--</span>
          </div>
          <div class="prof-gate">
            <span class="pg-icon fail" id="pg-clv-icon">✗</span>
            <span class="pg-label">avg_CLV &gt; 0</span>
            <span class="pg-val" id="pg-clv-val">--</span>
          </div>
          <div class="prof-gate">
            <span class="pg-icon fail" id="pg-pf-icon">✗</span>
            <span class="pg-label">payoff_ratio &gt; 1.0</span>
            <span class="pg-val" id="pg-pf-val">--</span>
          </div>
          <div class="prof-verdict" id="prof-verdict-box">
            Waiting for settled trades...
          </div>
        </div>
      </div>

      <!-- 7. ASYMMETRY PANEL -->
      <div class="cr-section">
        <div class="cr-section-title">Payoff Asymmetry</div>
        <div class="asym-eq" id="cr-asymmetry">
          <div><span class="asym-dim">Expected win  = </span><span class="asym-pos" id="asym-win-eq">WR × avg_win = ?</span></div>
          <div><span class="asym-dim">Expected loss = </span><span class="asym-neg" id="asym-loss-eq">LR × avg_loss = ?</span></div>
          <div style="border-top:1px solid var(--border);margin-top:3px;padding-top:3px">
            <span class="asym-dim">WR needed to break even: </span><span class="asym-gap" id="asym-be-wr">?</span>
            <span class="asym-dim"> | actual WR: </span><span id="asym-actual-wr">?</span>
          </div>
          <div><span class="asym-dim">gap (actual − breakeven): </span><span class="asym-gap" id="asym-gap">?</span></div>
        </div>
      </div>

      <!-- 8. REAL MONEY LOCKDOWN -->
      <div class="cr-section cr-locked">
        <div class="cr-section-title">Real Money Lockdown</div>
        <div class="lock-header">🔒 LOCKED</div>
        <div id="cr-lockdown">
          <div class="lock-reason">Initializing...</div>
        </div>
      </div>

      <!-- 9. SCALE READINESS -->
      <div class="cr-section">
        <div class="cr-section-title">Scale Readiness</div>
        <div class="scale-no">❌ NOT READY</div>
        <div id="cr-scale">
          <div class="scale-blocker">Loading blockers...</div>
        </div>
      </div>

      <!-- 10. NEXT BOTTLENECK -->
      <div class="cr-section cr-bottleneck">
        <div class="cr-section-title">⚡ Next Bottleneck</div>
        <div id="cr-bottleneck">
          <div class="bn-card">
            <div class="bn-priority">LOADING...</div>
            <div class="bn-action">Analyzing system state...</div>
          </div>
        </div>
      </div>

      <!-- EXECUTION FUNNEL (compact) -->
      <div class="cr-section">
        <div class="cr-section-title">Execution Funnel</div>
        <div id="funnel-box"></div>
      </div>

      <!-- CLEAN VS RAW (compact) -->
      <div class="cr-section">
        <div class="cr-section-title">Record Counts</div>
        <div class="mini-row"><span class="mini-key">Total Records</span><span id="p-trades" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Clean Settled</span><span id="p-settled" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Raw Settled</span><span id="m-raw-settled" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Conflicted</span><span id="m-conflicted" class="mini-val warn">--</span></div>
        <div class="mini-row"><span class="mini-key">Stale Open</span><span id="m-stale-open" class="mini-val warn">--</span></div>
        <div class="mini-row"><span class="mini-key">Realized P&amp;L</span><span id="live-realized-pnl" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Unrealized P&amp;L</span><span id="live-unrealized-pnl" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Win Rate</span><span id="p-winrate" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">Avg CLV</span><span id="p-clv" class="mini-val">--</span></div>
        <div class="mini-row"><span class="mini-key">last report</span><span id="settled-metrics-updated" class="mini-val">--</span></div>
      </div>

      <!-- BLOCKED REASONS -->
      <div class="cr-section">
        <div class="cr-section-title">Recent Blocked Reasons</div>
        <div id="blocked-reasons-box"></div>
      </div>

      <!-- 11. EDGE PROFILE FRESHNESS (Phase 9D) -->
      <div class="cr-section" id="fp-section">
        <div class="cr-section-title">Edge Profile Freshness</div>
        <div id="fp-status-box">
          <div class="mini-row"><span class="mini-key">status</span><span class="mini-val" id="fp-level">--</span></div>
          <div class="mini-row"><span class="mini-key">age</span><span class="mini-val" id="fp-age">--</span></div>
          <div class="mini-row"><span class="mini-key">stale in</span><span class="mini-val" id="fp-stale-in">--</span></div>
          <div class="mini-row"><span class="mini-key">trusted</span><span class="mini-val" id="fp-trusted">--</span></div>
          <div class="mini-row"><span class="mini-key">new nm trades</span><span class="mini-val" id="fp-new-nm">--</span></div>
          <div class="mini-row"><span class="mini-key">new sweet-spot</span><span class="mini-val" id="fp-new-sw">--</span></div>
          <div class="mini-row"><span class="mini-key">2D evidence</span><span class="mini-val" id="fp-2d-ok">--</span></div>
          <div class="mini-row"><span class="mini-key">bootstrap risk</span><span class="mini-val" id="fp-bs-risk">--</span></div>
          <div class="mini-row"><span class="mini-key">proof status</span><span class="mini-val fp-watch">WATCHLIST / RESEARCH ONLY</span></div>
        </div>
        <div style="margin-top:6px;">
          <div class="fp-cell-hdr fp-cell-row"><span>cell</span><span>n</span><span>WR</span><span>PnL</span><span>verdict</span></div>
          <div id="fp-cells-box"></div>
        </div>
        <div id="fp-warning-box"></div>
      </div>

    </div>

    <!-- RISK STATUS — always visible at bottom -->
    <div id="risk-status-box">
      <div class="risk-ph">
        <span class="risk-ph-title">🛡 Risk Status</span>
        <span id="r-status-pill" class="status-pill sp-normal">🟢 NORMAL</span>
      </div>
      <div class="risk-body">
        <div class="risk-row"><span class="risk-label">CAN OPEN NEW TRADES</span><span id="r-can-trade" class="can-trade-yes">YES</span></div>
        <div class="risk-row"><span class="risk-label">Daily P&amp;L</span><span id="r-daily-pnl" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Weekly P&amp;L</span><span id="r-weekly-pnl" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Open Positions</span><span id="r-open-pos" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Open Slots</span><span id="r-open-slots" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Full Exposure</span><span id="r-exposure" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Weighted Exposure</span><span id="r-weighted-exposure" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Effective Daily Risk</span><span id="r-eff-risk" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Daily Loss Limit</span><span id="r-loss-limit" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Remaining Room</span><span id="r-room" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Risk Used</span><span id="r-risk-pct" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Loss Streak</span><span id="r-streak" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Kill Switch</span><span id="r-kill" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Cooldown</span><span id="r-cooldown" class="risk-val">--</span></div>
        <div class="risk-row"><span class="risk-label">Last Trade</span><span id="r-last-trade" class="risk-val">--</span></div>
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
const prevMetricValues = {};
const prevMarketValues = {};

function setTextTracked(id, value, rawValue) {
  const el = document.getElementById(id);
  if (!el) return;
  const next = String(value);
  const prev = prevMetricValues[id];
  if (prev !== undefined && prev !== next) {
    const cell = el.closest('[data-watch]') || el.parentElement;
    if (cell) {
      const down = rawValue != null ? rawValue < 0 : next.startsWith('-');
      cell.classList.remove('metric-flash', 'metric-flash-down');
      void cell.offsetWidth;
      cell.classList.add(down ? 'metric-flash-down' : 'metric-flash');
    }
  }
  prevMetricValues[id] = next;
  el.textContent = next;
}

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
      <div class="bet-val">${o.bet_size > 0 ? 'Notional $'+o.bet_size.toFixed(0) : '—'}${arbFlag}</div>
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
        <span class="event-source">${source}</span>
        <span class="event-time">${timestamp}</span>
        <span class="event-level ${safeLevel}">${escapeHtml(ev.level || 'INFO')}</span>
      </div>
      <div class="event-msg">${message}</div>
    </div>`;
  }).join('');
}

function fmtNum(v, digits=4) {
  if (v == null || Number.isNaN(Number(v))) return '--';
  return Number(v).toFixed(digits);
}

function fmtPct(v) {
  if (v == null || Number.isNaN(Number(v))) return '--';
  return (Number(v) * 100).toFixed(1) + '%';
}

function fmtAge(ts) {
  if (!ts) return '--';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '--';
  const seconds = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (seconds < 90) return seconds + 's';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 90) return minutes + 'm';
  return Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
}

function fmtFreshness(ts) {
  if (!ts) return 'freshness --';
  const d = new Date(ts);
  if (!Number.isNaN(d.getTime())) return 'updated ' + fmtAge(ts) + ' ago';
  return 'scan ' + String(ts).slice(0, 8);
}

function trendArrow(v) {
  if (v == null || Number.isNaN(Number(v)) || Number(v) === 0) return '→';
  return Number(v) > 0 ? '↗' : '↘';
}

function fmtCountdown(ts) {
  if (!ts) return '--';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '--';
  const seconds = Math.floor((d.getTime() - Date.now()) / 1000);
  const sign = seconds < 0 ? '-' : '';
  const abs = Math.abs(seconds);
  if (abs < 90) return sign + abs + 's';
  const minutes = Math.floor(abs / 60);
  if (minutes < 90) return sign + minutes + 'm';
  return sign + Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
}

function renderSparkline(values, cls='sparkbar') {
  const nums = (values || []).map(Number).filter(v => !Number.isNaN(v));
  if (!nums.length) return '<div class="mini-val">no history</div>';
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = Math.max(0.0001, max - min);
  return nums.map(v => {
    const pct = 18 + ((v - min) / span) * 78;
    return `<span class="${cls}" style="height:${pct}%"></span>`;
  }).join('');
}

function fmtDollar(v) {
  if (v == null || Number.isNaN(Number(v))) return '--';
  return '$' + Number(v).toFixed(2);
}

function renderBetRewardTruth(truth) {
  if (!truth) {
    return `<div class="bet-truth-panel">
      <div class="bet-truth-title">Bet / Reward Truth</div>
      <div class="bet-truth-warning">No open trade economics available for this market.</div>
    </div>`;
  }
  const rows = [
    ['Payout Notional', fmtDollar(truth.payout_notional)],
    ['Capital at Risk', fmtDollar(truth.capital_at_risk)],
    ['Max Profit', fmtDollar(truth.max_profit_if_win)],
    ['Max Loss', fmtDollar(truth.max_loss_if_loss)],
    ['Reward/Risk', truth.reward_risk == null ? '--' : Number(truth.reward_risk).toFixed(3)],
    [truth.breakeven_label || 'Breakeven WR', truth.breakeven_wr == null ? '--' : fmtPct(truth.breakeven_wr)],
    ['Accounting Version', escapeHtml(truth.accounting_version || '--')],
    ['Economic PnL', truth.economic_pnl == null ? '--' : fmtMoney(truth.economic_pnl)],
    ['Recorded PnL', truth.recorded_pnl == null ? '--' : fmtMoney(truth.recorded_pnl)],
    ['Open Exposure', fmtDollar(truth.open_exposure)],
  ];
  return `<div class="bet-truth-panel">
    <div class="bet-truth-title">Bet / Reward Truth</div>
    <div class="bet-truth-grid">
      ${rows.map(([label, value]) => `<div class="bet-truth-cell">
        <div class="bet-truth-label">${escapeHtml(label)}</div>
        <div class="bet-truth-value">${value}</div>
      </div>`).join('')}
    </div>
    ${truth.warning ? `<div class="bet-truth-warning">${escapeHtml(truth.warning)}</div>` : ''}
  </div>`;
}

function renderMarketVisuals(visuals) {
  visuals = visuals || {};
  const strip = visuals.market_strip || [];
  const stripEl = document.getElementById('market-strip');
  const stripLabel = document.getElementById('market-strip-label');
  if (stripLabel) stripLabel.textContent = strip.length ? strip.length + ' tracked markets' : 'real scanner quotes';
  if (stripEl) {
    stripEl.innerHTML = strip.length ? strip.map(m => {
      const change = m.change;
      const changeCls = change > 0 ? 'good' : change < 0 ? 'bad' : '';
      const changeText = change == null ? '--' : `${trendArrow(change)} ${(change > 0 ? '+' : '')}${Number(change).toFixed(4)}`;
      const key = m.symbol || m.ticker || 'UNKNOWN';
      const priceText = fmtNum(m.market_mid);
      const changed = prevMarketValues[key] !== undefined && prevMarketValues[key] !== priceText;
      prevMarketValues[key] = priceText;
      return `<div class="market-tile live-fresh ${changed ? 'changed' : ''}" title="${escapeHtml(m.ticker || '')}">
        <div class="market-tile-top">
          <span class="market-symbol">${escapeHtml(m.symbol || '--')}</span>
          <span class="market-price">${priceText}</span>
        </div>
        <div class="market-tile-top" style="margin-top:3px">
          <span class="mini-key">scanner: ${escapeHtml(m.action || '--')}</span>
          <span class="market-change ${changeCls}">${changeText}</span>
        </div>
        <div class="market-meta-line">
          <span>spread ${fmtNum(m.spread)}</span>
          <span>${fmtFreshness(m.last_update)}</span>
        </div>
        <div class="sparkline">${renderSparkline(m.sparkline || [])}</div>
      </div>`;
    }).join('') : '<div class="empty">Waiting for scanner quote history.</div>';
  }

  const selected = visuals.selected_market || null;
  const selectedEl = document.getElementById('selected-market-panel');
  const selectedLabel = document.getElementById('selected-market-label');
  if (selectedLabel) selectedLabel.textContent = selected ? selected.ticker : '--';
  if (selectedEl) {
    if (!selected) {
      selectedEl.innerHTML = '<div class="empty">No selected market yet.</div>';
    } else {
      const noSelectedQuote = selected.market_mid == null && selected.yes_bid == null && selected.yes_ask == null;
      const historyPoints = selected.history_points || (selected.history || []).length;
      const closeLabel = fmtCountdown(selected.close_time);
      const progressWidth = (() => {
        const d = new Date(selected.close_time);
        if (Number.isNaN(d.getTime())) return 0;
        const seconds = Math.max(0, Math.floor((d.getTime() - Date.now()) / 1000));
        return Math.max(0, Math.min(100, (300 - Math.min(300, seconds)) / 300 * 100));
      })();
      selectedEl.innerHTML = `
        <div class="selected-market-grid">
          <div class="selected-metric"><div class="selected-metric-label">Ticker / Side</div><div class="selected-metric-value">${escapeHtml(selected.ticker || '--')} ${escapeHtml(selected.action || '')}</div></div>
          <div class="selected-metric"><div class="selected-metric-label">Entry / Current Mid</div><div class="selected-metric-value">${fmtNum(selected.entry_price)} / ${fmtNum(selected.market_mid)}</div></div>
          <div class="selected-metric"><div class="selected-metric-label">Spread</div><div class="selected-metric-value">${fmtNum(selected.spread)}</div></div>
          <div class="selected-metric"><div class="selected-metric-label">History Points</div><div class="selected-metric-value">${historyPoints}</div></div>
        </div>
        ${renderBetRewardTruth(selected.bet_reward_truth)}
        ${noSelectedQuote
          ? '<div class="mini-row"><span class="mini-key" style="color:var(--yellow,#f5c518);font-weight:700">QUOTE UNAVAILABLE</span><span class="mini-val" style="color:var(--yellow,#f5c518)">market not in scanner</span></div>'
          : `<div class="mini-row"><span class="mini-key">YES bid/ask</span><span class="mini-val">${fmtNum(selected.yes_bid)} / ${fmtNum(selected.yes_ask)}</span></div>
        <div class="mini-row"><span class="mini-key">NO bid/ask</span><span class="mini-val">${fmtNum(selected.no_bid)} / ${fmtNum(selected.no_ask)}</span></div>
        <div class="mini-row"><span class="mini-key">Spread</span><span class="mini-val">${fmtNum(selected.spread)}</span></div>`
        }
        <div class="mini-row"><span class="mini-key">Close countdown</span><span class="mini-val">${closeLabel}</span></div>
        <div class="time-progress-wrap"><div class="time-progress-bar" style="width:${progressWidth}%"></div></div>
        <div class="line-chart">${renderSparkline(selected.history || [], 'chartbar')}</div>
        <div class="entry-line-note">entry=${fmtNum(selected.entry_price)} | close=${fmtCountdown(selected.close_time)} | result=${fmtCountdown(selected.result_time)}</div>
      `;
    }
  }

  const pressureBox = document.getElementById('quote-pressure-box');
  const pressure = visuals.quote_pressure || [];
  if (pressureBox) {
    pressureBox.innerHTML = pressure.length ? pressure.map(q => {
      const imbalance = q.imbalance;
      const noQuote = q.yes_bid == null && q.yes_ask == null && q.no_bid == null && q.no_ask == null;
      const width = imbalance == null ? 50 : Math.max(4, Math.min(96, 50 + imbalance * 100));
      const cls = imbalance != null && imbalance < 0 ? 'bad' : '';
      return `<div class="active-card">
        <div class="active-card-top"><span>${escapeHtml(q.ticker || 'UNKNOWN')}</span><span>spread=${fmtNum(q.spread)}</span></div>
        <div class="active-card-meta">
          ${noQuote
            ? '<span style="color:var(--yellow,#f5c518);font-weight:700">QUOTE UNAVAILABLE — market no longer in scanner</span>'
            : `YES ${fmtNum(q.yes_bid)} / ${fmtNum(q.yes_ask)} | NO ${fmtNum(q.no_bid)} / ${fmtNum(q.no_ask)} | imbalance=${fmtNum(imbalance)}<div class="pressure-bar-wrap"><div class="pressure-bar ${cls}" style="width:${width}%"></div></div>`
          }
        </div>
      </div>`;
    }).join('') : '<div class="mini-row"><span class="mini-key">No active quote pressure</span><span class="mini-val">--</span></div>';
  }
}

function fmtMoney(v) {
  if (v == null) return '--';
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
}

function moneyClass(v) {
  return 'mini-val ' + (v > 0 ? 'good' : v < 0 ? 'bad' : '');
}

function proofIcon(state) {
  if (state === 'PASS') return ['✓', 'pass'];
  if (state === 'FAIL') return ['✗', 'fail'];
  return ['…', 'watch'];
}

function renderProofChecklist(proof) {
  proof = proof || {};
  const sampleEl = document.getElementById('v-sample-progress');
  const qualityEl = document.getElementById('v-data-quality');
  const modernRows = proof.modern_evaluated_rows || 0;
  const targetMin = proof.target_minimum || 30;
  const targetProof = proof.target_proof || 100;
  if (sampleEl) {
    sampleEl.textContent = `Modern sample: ${modernRows} / ${targetMin} minimum | ${modernRows} / ${targetProof} proof`;
    sampleEl.className = 'mini-val ' + (modernRows >= targetMin ? 'good' : 'warn');
  }
  if (qualityEl) {
    const quality = proof.data_quality || 'UNKNOWN';
    const timeExitExcluded = proof.time_exit_excluded_count || 0;
    qualityEl.textContent = quality
      + (proof.legacy_evaluated_rows ? ` | legacy rows ${proof.legacy_evaluated_rows}` : '')
      + ` | proof scope ${proof.proof_scope || 'OUTCOME_KNOWN_SETTLED_ONLY'}`
      + (timeExitExcluded ? ` | time exits excluded ${timeExitExcluded}` : '');
    qualityEl.className = 'mini-val ' + (quality === 'MODERN_ONLY' ? 'good' : 'warn');
  }
  const dcEl = document.getElementById('v-data-collection');
  if (dcEl) {
    const dc = proof.data_collection_count != null ? proof.data_collection_count : 0;
    const bp = proof.bootstrap_provisional_count != null ? proof.bootstrap_provisional_count : 0;
    const norm = proof.normal_trade_count != null ? proof.normal_trade_count : 0;
    const total = dc + bp + norm;
    if (total === 0) {
      dcEl.textContent = 'no modern trades yet';
      dcEl.className = 'mini-val';
    } else if (norm === 0) {
      dcEl.textContent = `${dc} data_collection | ${bp} bootstrap | 0 normal`;
      dcEl.className = 'mini-val bad';
    } else {
      dcEl.textContent = `${dc} data_collection | ${bp} bootstrap | ${norm} normal`;
      dcEl.className = 'mini-val ' + (norm > 0 ? 'good' : 'warn');
    }
  }

  const byKey = {};
  (proof.items || []).forEach(item => { byKey[item.key] = item; });
  const bindings = [
    ['profitability', 'v-profitable', 'v-profitable-text', 'v-profitable-value'],
    ['edge_validity', 'v-edge', 'v-edge-text', 'v-edge-value'],
    ['clv', 'v-clv', 'v-clv-text', 'v-clv-value'],
  ];
  bindings.forEach(([key, iconId, textId, valueId]) => {
    const item = byKey[key] || {state:'WATCH', label:key, value:'--', explanation:'Insufficient data'};
    const [symbol, cls] = proofIcon(item.state);
    const icon = document.getElementById(iconId);
    if (icon) {
      icon.textContent = symbol;
      icon.className = 'verdict-icon ' + cls;
    }
    const text = document.getElementById(textId);
    if (text) text.textContent = `${item.state}: ${item.label}`;
    const val = document.getElementById(valueId);
    if (val) val.textContent = item.value || '--';
  });

  const msg = document.getElementById('verdict-msg');
  if (msg) {
    const sv  = proof.scale_verdict || 'NOT_PROVEN';
    const svr = proof.scale_verdict_reason || '';
    const svColors = {
      'NOT_PROVEN':            'var(--red)',
      'DATA_COLLECTION_ONLY':  'var(--red)',
      'WATCHLIST':             'var(--yellow)',
      'PAPER_VALIDATION_READY':'var(--yellow)',
      'SCALE_ELIGIBLE':        'var(--green2)',
    };
    const c = svColors[sv] || 'var(--muted)';
    msg.innerHTML = `<span style="color:${c};font-weight:700">${sv}</span><br><span style="color:var(--muted);font-size:8px">${svr}</span>`;
  }
}

function _setEl(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
function _setElCls(id, text, cls) {
  const el = document.getElementById(id);
  if (el) { el.textContent = text; el.className = cls; }
}

function renderPaperStats(stats) {
  if (!stats || Object.keys(stats).length === 0) {
    setTextTracked('p-trades', '0');
    setTextTracked('p-settled', '0');
    setTextTracked('p-winrate', '--');
    setTextTracked('p-clv', '--');
    _setEl('live-realized-pnl', '--');
    _setEl('live-unrealized-pnl', '--');
    _setEl('settled-metrics-updated', 'last report --');
    _setEl('m-raw-settled', '--');
    _setEl('m-conflicted', '--');
    _setEl('m-stale-open', '--');
    renderProofChecklist({});
    renderMarketVisuals({});
    return;
  }

  const clean = stats.clean || stats;
  const raw = stats.raw || {};
  const total = raw.total_records || stats.total_trades || 0;
  const settled = clean.settled_trades || 0;
  const winRate = clean.win_rate || 0;
  const clv = clean.avg_clv;

  setTextTracked('p-trades', total);
  setTextTracked('p-settled', settled);
  setTextTracked('p-winrate', settled > 0 ? (winRate * 100).toFixed(1) + '%' : '--');
  setTextTracked('p-clv', (settled > 0 && clv != null) ? (clv > 0 ? '+' : '') + clv.toFixed(3) : '--');

  _setEl('settled-metrics-updated', 'last report ' + fmtFreshness(stats.generated_at).replace('updated ', ''));
  _setEl('m-raw-settled', raw.settled_rows != null ? raw.settled_rows : '--');
  _setEl('m-conflicted', clean.conflicted_settled != null ? clean.conflicted_settled : '--');
  _setEl('m-stale-open', clean.stale_open != null ? clean.stale_open : '--');

  const live = stats.live_pnl || {};
  const realizedEl = document.getElementById('live-realized-pnl');
  const unrealizedEl = document.getElementById('live-unrealized-pnl');
  if (realizedEl)  { realizedEl.textContent  = fmtMoney(live.realized_pnl);  realizedEl.className  = moneyClass(live.realized_pnl  || 0); }
  if (unrealizedEl){ unrealizedEl.textContent = fmtMoney(live.unrealized_pnl); unrealizedEl.className = moneyClass(live.unrealized_pnl || 0); }

  renderProofChecklist(stats.proof_checklist || {});
  renderMarketVisuals(stats.market_visuals || {});
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

// ── CONTROL ROOM RENDER FUNCTIONS ─────────────────────────────────────────────

function renderControlRoom(stats) {
  if (!stats) return;
  const truth = stats.truth_state || {};
  if (truth.error) return; // don't blank out the panel on error

  renderVerdictHeader(truth);
  renderMachineMap(truth.machine_map || []);
  renderProofPath(truth.proof_progress || {});
  renderBootstrapMonitor(truth.bootstrap_path || {});
  renderTradeTruthFeed(truth.trade_feed || []);
  renderProfitabilityReality(truth.profitability || {});
  renderAsymmetryPanel(truth.profitability || {});
  renderLockdownPanel(truth.lockdown_reasons || []);
  renderScaleReadiness(truth.proof_progress || {}, stats.proof_checklist || {});
  renderNextBottleneck(truth.next_bottleneck || {});

  const lbl = document.getElementById('cr-verdict-label');
  if (lbl) {
    const v = truth.system_verdict || 'UNKNOWN';
    const colors = {
      DEADLOCK: 'var(--red)', COLLECTING: 'var(--yellow)',
      BOOTSTRAP_PENDING: 'var(--yellow)', EARLY_DATA: 'var(--yellow)',
      BUILDING_PROOF: 'var(--cyan)', PROOF_CANDIDATE: 'var(--green2)',
      WATCHLIST: 'var(--yellow)',
    };
    lbl.textContent = v;
    lbl.style.color = colors[v] || 'var(--muted)';
  }
}

function renderVerdictHeader(truth) {
  const el = document.getElementById('cr-verdict-pills');
  if (!el) return;
  const proof = truth.proof_progress || {};
  const bootstrap = truth.bootstrap_path || {};
  const profit = truth.profitability || {};
  const nm = proof.normal_modern || 0;
  const gp = profit.gates_passed || 0;
  const bs = bootstrap.status || 'UNKNOWN';
  const bsCls = bs === 'ALIVE' ? 'vp-ok' : bs === 'DEADLOCK' ? 'vp-locked' : 'vp-warn';
  const bsIcon = bs === 'ALIVE' ? '✅' : bs === 'DEADLOCK' ? '⚠️' : '🔄';
  el.innerHTML = `
    <span class="verdict-pill vp-locked"><span class="vp-val">🔒 LOCKED</span><span class="vp-key">REAL $</span></span>
    <span class="verdict-pill vp-locked"><span class="vp-val">❌ ${nm}/30</span><span class="vp-key">SCALE</span></span>
    <span class="verdict-pill ${gp === 3 ? 'vp-ok' : 'vp-locked'}"><span class="vp-val">${gp === 3 ? '✅' : '❌'} ${gp}/3</span><span class="vp-key">PROFIT</span></span>
    <span class="verdict-pill ${bsCls}"><span class="vp-val">${bsIcon} ${bs}</span><span class="vp-key">BOOT</span></span>
    <span class="verdict-pill ${nm >= 30 ? 'vp-ok' : nm >= 10 ? 'vp-warn' : 'vp-info'}"><span class="vp-val">📊 n=${nm}</span><span class="vp-key">PROOF</span></span>
  `;
}

function renderMachineMap(nodes) {
  const el = document.getElementById('cr-machine-map');
  if (!el || !nodes.length) return;
  const statusMap = {
    scanner:   n => n.active ? ['mn-active', n.detail] : ['mn-off', 'OFFLINE'],
    council:   n => ['mn-active', n.detail],
    trader:    n => n.active ? ['mn-active', n.detail] : ['mn-off', 'OFFLINE'],
    proof:     n => ['mn-active', n.detail],
    readiness: n => ['mn-locked', n.detail],
  };
  el.innerHTML = nodes.map((n, i) => {
    const [cls, detail] = (statusMap[n.key] || (() => ['mn-off', '--']))(n);
    return (i > 0 ? '<div class="machine-arrow">→</div>' : '') +
      `<div class="machine-node">
        <div class="mn-icon">${escapeHtml(n.icon || '?')}</div>
        <div class="mn-label">${escapeHtml(n.label)}</div>
        <div class="mn-detail ${cls}">${escapeHtml(detail || '--')}</div>
      </div>`;
  }).join('');
}

function renderProofPath(proof) {
  const nm = proof.normal_modern || 0;
  const modern = proof.modern_full || 0;
  const settled = proof.clean_settled || 0;
  const target = proof.target_proof || 100;
  const targetScale = proof.target_scale || 30;

  function setBar(valId, barId, cur, tgt, cls) {
    const pct = tgt > 0 ? Math.min(100, cur / tgt * 100) : 0;
    const valEl = document.getElementById(valId);
    const barEl = document.getElementById(barId);
    if (valEl) valEl.textContent = cur + ' / ' + tgt;
    if (barEl) { barEl.style.width = pct + '%'; if (cls) barEl.className = 'gate-fill ' + cls; }
  }
  setBar('pp-settled', 'pp-settled-bar', settled, target, '');
  setBar('pp-modern',  'pp-modern-bar',  modern,  target, 'yellow');
  setBar('pp-normal',  'pp-normal-bar',  nm,      targetScale, nm >= targetScale ? '' : 'red');

  const detail = document.getElementById('pp-bucket-detail');
  if (detail) {
    detail.textContent = (
      `dc_override=${proof.dc_override || 0} | provisional=${proof.provisional || 0} | ` +
      `era_allow=${proof.era_allow || 0} | legacy=${proof.legacy || 0} [excluded]`
    );
  }
}

function renderBootstrapMonitor(bs) {
  const el = document.getElementById('cr-bootstrap');
  if (!el) return;
  const status = bs.status || 'UNKNOWN';
  const clsMap = {
    ALIVE: 'bs-alive', DEADLOCK: 'bs-deadlock',
    DISABLED: 'bs-disabled', NO_TRADES: 'bs-pending', UNKNOWN: 'bs-pending',
  };
  const cls = clsMap[status] || 'bs-pending';
  const deadlockNote = status === 'DEADLOCK'
    ? '<div class="bs-action" style="border-left-color:var(--red)">⚠️ CRITICAL: Dashboard process loaded stale module cache before Phase 6B-2. Restart to fix.</div>'
    : '';
  el.innerHTML = `
    <span class="bs-badge ${cls}">${escapeHtml(status)}</span>
    <div class="bs-detail">
      enabled=${bs.enabled} | era_allow=${bs.era_allow_count || 0} | provisional=${bs.provisional_count || 0}<br>
      ${escapeHtml(bs.detail || '')}
    </div>
    ${deadlockNote}
    <div class="bs-action">${escapeHtml(bs.action || '')}</div>
  `;
  const section = document.getElementById('cr-bootstrap-section');
  if (section) {
    section.style.background = status === 'DEADLOCK' ? 'rgba(255,23,68,0.04)' : '';
  }
}

function renderTradeTruthFeed(feed) {
  const el = document.getElementById('cr-trade-feed');
  const cntEl = document.getElementById('cr-feed-count');
  if (!el) return;
  if (cntEl) cntEl.textContent = feed.length ? feed.length + ' trades' : '';
  if (!feed.length) {
    el.innerHTML = '<div class="empty">No trades yet.</div>';
    return;
  }
  el.innerHTML = feed.map(t => {
    const pnl = t.pnl;
    const pnlStr = pnl == null ? '--' : (pnl >= 0 ? '+$' + Math.abs(pnl).toFixed(2) : '-$' + Math.abs(pnl).toFixed(2));
    const pnlCls = pnl == null ? '' : pnl > 0 ? 'mini-val good' : pnl < 0 ? 'mini-val bad' : '';
    const bucket = escapeHtml(t.bucket || 'UNKNOWN');
    return `<div class="tth ttr">
      <span class="ttr-ticker">${escapeHtml(t.ticker || '?')}</span>
      <span class="ttr-bucket bkt-${bucket}">${bucket}</span>
      <span class="ttr-status">${escapeHtml((t.status || '?').slice(0,8))}</span>
      <span class="ttr-pnl ${pnlCls}">${pnlStr}</span>
    </div>`;
  }).join('');
}

function renderProfitabilityReality(prof) {
  function setGate(iconId, valId, pass, val) {
    const icon = document.getElementById(iconId);
    const valEl = document.getElementById(valId);
    if (icon) { icon.textContent = pass ? '✓' : '✗'; icon.className = 'pg-icon ' + (pass ? 'pass' : 'fail'); }
    if (valEl) valEl.textContent = val;
  }
  const roi = prof.roi;
  const clv = prof.avg_clv;
  const pf  = prof.payoff_ratio;
  const wr  = prof.win_rate;
  setGate('pg-roi-icon', 'pg-roi-val', prof.gate_roi,
    roi != null ? (roi * 100).toFixed(1) + '%' : '--');
  setGate('pg-clv-icon', 'pg-clv-val', prof.gate_clv,
    clv != null ? (clv >= 0 ? '+' : '') + clv.toFixed(4) : '--');
  setGate('pg-pf-icon',  'pg-pf-val',  prof.gate_pf,
    pf  != null ? pf.toFixed(3) : '--');

  const box = document.getElementById('prof-verdict-box');
  if (box) {
    const gp = prof.gates_passed || 0;
    const cls = gp === 3 ? 'ok' : gp >= 1 ? 'warn' : '';
    box.className = 'prof-verdict ' + cls;
    box.innerHTML = `<strong>${escapeHtml(prof.verdict || 'UNKNOWN')}</strong><br>
      <span style="color:var(--muted)">${escapeHtml(prof.plain_english || '')}</span>`;
  }
}

function renderAsymmetryPanel(prof) {
  const wr  = prof.win_rate;
  const lrate = wr != null ? (1 - wr) : null;
  const avgW = prof.avg_win;
  const avgL = prof.avg_loss;
  const be  = prof.breakeven_wr;
  const actual = wr;

  function orDash(v, fmt) { return v != null ? fmt(v) : '?'; }

  const winEqEl  = document.getElementById('asym-win-eq');
  const lossEqEl = document.getElementById('asym-loss-eq');
  const beEl     = document.getElementById('asym-be-wr');
  const actEl    = document.getElementById('asym-actual-wr');
  const gapEl    = document.getElementById('asym-gap');

  if (winEqEl)  winEqEl.textContent  = orDash(wr, v => (v*100).toFixed(0)+'%') + ' × ' + orDash(avgW, v => '$'+v.toFixed(2)) + ' = ' + (wr != null && avgW != null ? '$'+(wr*avgW).toFixed(2) : '?');
  if (lossEqEl) lossEqEl.textContent = orDash(lrate, v => (v*100).toFixed(0)+'%') + ' × ' + orDash(avgL, v => '$'+Math.abs(v).toFixed(2)) + ' = ' + (lrate != null && avgL != null ? '$'+(lrate*Math.abs(avgL)).toFixed(2) : '?');
  if (beEl)     beEl.textContent     = be  != null ? (be*100).toFixed(1)+'%' : '?';
  if (actEl) {
    const wr_str = actual != null ? (actual*100).toFixed(1)+'%' : '?';
    actEl.textContent = wr_str;
    actEl.style.color = (actual != null && be != null && actual >= be) ? 'var(--green2)' : 'var(--red)';
  }
  if (gapEl) {
    if (actual != null && be != null) {
      const gap = actual - be;
      gapEl.textContent = (gap >= 0 ? '+' : '') + (gap*100).toFixed(1) + '%';
      gapEl.style.color = gap >= 0 ? 'var(--green2)' : 'var(--red)';
    } else {
      gapEl.textContent = '?';
    }
  }
}

function renderLockdownPanel(reasons) {
  const el = document.getElementById('cr-lockdown');
  if (!el) return;
  if (!reasons.length) {
    el.innerHTML = '<div class="lock-reason">No reasons loaded.</div>';
    return;
  }
  el.innerHTML = reasons.map(r => `<div class="lock-reason">${escapeHtml(r)}</div>`).join('');
}

function renderScaleReadiness(proof, checklist) {
  const el = document.getElementById('cr-scale');
  if (!el) return;
  const nm = proof.normal_modern || 0;
  const sv = (checklist && checklist.scale_verdict) || 'NOT_PROVEN';
  const svr = (checklist && checklist.scale_verdict_reason) || '';
  const blockers = [];
  if (nm < 30) blockers.push(`normal_modern=${nm}/30 (need 30 council-approved modern trades)`);
  if (sv === 'NOT_PROVEN' || sv === 'DATA_COLLECTION_ONLY') {
    if (svr && !blockers.some(b => b.includes(svr.slice(0,20)))) blockers.push(svr);
  }
  blockers.push('scale_allowed=False (hardcoded — requires explicit human council approval)');
  el.innerHTML = blockers.map(b => `<div class="scale-blocker">${escapeHtml(b)}</div>`).join('');
}

function renderNextBottleneck(bn) {
  const el = document.getElementById('cr-bottleneck');
  if (!el || !bn.action) return;
  const pCls = { CRITICAL: 'var(--red)', HIGH: 'var(--yellow)', MEDIUM: 'var(--cyan)', LOW: 'var(--green2)' };
  const color = pCls[bn.priority] || 'var(--muted)';
  el.innerHTML = `
    <div class="bn-card" style="border-color:${color}30;background:${color}08">
      <div class="bn-priority" style="color:${color}">${escapeHtml(bn.priority || 'INFO')}</div>
      <div class="bn-action">${escapeHtml(bn.action || '')}</div>
      <div class="bn-reason">${escapeHtml(bn.reason || '')}</div>
      <div class="bn-command">$ ${escapeHtml(bn.command || '')}</div>
      <div class="bn-impact">→ ${escapeHtml(bn.impact || '')}</div>
    </div>
  `;
}

function renderProfileFreshness(ep) {
  if (!ep) return;
  const fl = (ep.freshness_level || 'MISSING').toUpperCase();
  const cls = fl === 'FRESH' ? 'fp-fresh' : fl === 'WATCH' ? 'fp-watch' : 'fp-danger';
  const lvlEl = document.getElementById('fp-level');
  if (lvlEl) { lvlEl.textContent = fl; lvlEl.className = 'mini-val ' + cls; }

  const ageEl = document.getElementById('fp-age');
  if (ageEl) ageEl.textContent = ep.age_hours != null ? ep.age_hours + 'h' : '--';

  const siEl = document.getElementById('fp-stale-in');
  if (siEl) {
    if (ep.hours_until_stale != null) {
      siEl.textContent = ep.hours_until_stale + 'h';
      siEl.className = 'mini-val ' + (ep.hours_until_stale < 4 ? 'fp-danger' : ep.hours_until_stale < 12 ? 'fp-watch' : 'fp-fresh');
    } else { siEl.textContent = '--'; }
  }

  const trEl = document.getElementById('fp-trusted');
  if (trEl) {
    trEl.textContent = ep.trusted ? 'YES' : 'NO';
    trEl.className = 'mini-val ' + (ep.trusted ? 'fp-fresh' : 'fp-danger');
  }

  const nmEl = document.getElementById('fp-new-nm');
  if (nmEl) nmEl.textContent = ep.new_normal_modern_since_rebuild != null ? ep.new_normal_modern_since_rebuild : '--';

  const swEl = document.getElementById('fp-new-sw');
  if (swEl) swEl.textContent = ep.new_sweet_spot_since_rebuild != null ? ep.new_sweet_spot_since_rebuild : '--';

  const okEl = document.getElementById('fp-2d-ok');
  if (okEl) {
    okEl.textContent = ep.sweet_spot_qualifies ? 'ACTIVE' : 'INACTIVE';
    okEl.className = 'mini-val ' + (ep.sweet_spot_qualifies ? 'fp-fresh' : 'fp-watch');
  }

  const bsEl = document.getElementById('fp-bs-risk');
  if (bsEl) {
    const br = (ep.bootstrap_risk || '').toUpperCase();
    bsEl.textContent = br || '--';
    bsEl.className = 'mini-val ' + (br === 'LOW' ? 'fp-fresh' : br === 'MEDIUM' ? 'fp-watch' : 'fp-danger');
  }

  // 2D cells table
  const cellsBox = document.getElementById('fp-cells-box');
  if (cellsBox) {
    const cells = ep.cells_2d || [];
    if (cells.length === 0) {
      cellsBox.innerHTML = '<div style="color:var(--dim);font-size:7px;">no 2D cells</div>';
    } else {
      cellsBox.innerHTML = cells.map(c => {
        const vCls = (c.verdict || '').includes('SWEET') ? 'fp-fresh' : (c.verdict || '').includes('POISON') ? 'fp-danger' : 'fp-watch';
        const pnlStr = c.pnl != null ? (c.pnl >= 0 ? '+' : '') + c.pnl.toFixed(2) : '--';
        const wrStr  = c.wr  != null ? (c.wr * 100).toFixed(1) + '%' : '--';
        return `<div class="fp-cell-row">
          <span title="${escapeHtml(c.cell||'')}">${escapeHtml((c.cell||'').slice(0,16))}</span>
          <span>${c.n != null ? c.n : '--'}</span>
          <span>${wrStr}</span>
          <span>${pnlStr}</span>
          <span class="${vCls}">${escapeHtml(c.verdict||'')}</span>
        </div>`;
      }).join('');
    }
  }

  // Warning / OK box
  const warnBox = document.getElementById('fp-warning-box');
  if (warnBox) {
    const w = ep.warning || '';
    if (w && fl !== 'FRESH') {
      warnBox.innerHTML = `<div class="fp-warn-box">${escapeHtml(w)}</div>`;
    } else if (fl === 'FRESH') {
      warnBox.innerHTML = '<div class="fp-ok-box">Profile fresh — research-only status; not proof of profitability or scale readiness.</div>';
    } else {
      warnBox.innerHTML = '';
    }
  }
}

// ── END CONTROL ROOM RENDER FUNCTIONS ─────────────────────────────────────────

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
    renderControlRoom(d.performance_report || {});
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

async function fetchTruthPanels() {
  try {
    const resp = await fetch('/api/truth_panels');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data && data.edge_profile) {
      renderProfileFreshness(data.edge_profile);
    }
  } catch(e) {
    console.warn('fetchTruthPanels error:', e);
  }
}

// Fetch loop
setInterval(fetchState, 5000);
setInterval(fetchTruthPanels, 30000);
fetchState();
fetchTruthPanels();
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
    state["market_visuals"] = state["performance_report"].get("market_visuals", {})
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
# TRUTH PANELS — Phase 6I operator layer
# Read-only extended diagnostics route.
# Adding new imports here so existing imports are not disturbed.
# ─────────────────────────────────────────

try:
    from tools.clean_truth_report import (
        edge_bucket       as _tp_edge_bucket,
        market_horizon    as _tp_market_horizon,
        entry_price_bucket as _tp_entry_bucket,
        calibration_bucket as _tp_conf_bucket,
    )
    _TP_CLASSIFY_OK = True
except ImportError:
    _TP_CLASSIFY_OK = False


def _compute_truth_panels() -> dict:
    """
    Compute additional operator-layer truth panels not in the main state.
    All read-only. Fail-soft: errors return partial data with an error key.
    Never touches trading logic, thresholds, or locks.
    """
    try:
        all_records = load_trades()
    except Exception as exc:
        return {"error": f"load_trades failed: {exc}"}

    # ── 1. Proof velocity ──────────────────────────────────────────────────────
    def _is_modern_r(r):
        return (
            r.get("risk_edge") is not None
            and r.get("model_probability") is not None
            and any(r.get(k) is not None for k in
                    ("yes_bid", "yes_ask", "no_bid", "no_ask", "price_yes", "price_no"))
        )

    try:
        from tools.performance_report import (
            build_terminal_key_sets, classify_open_records, classify_settled_records,
        )
        active_opens, stale_opens = classify_open_records(all_records)
        sk, fk, vk = build_terminal_key_sets(all_records)
        clean_settled, _ = classify_settled_records(all_records, sk, fk, vk)
    except Exception:
        active_opens = [r for r in all_records if r.get("status") == "OPEN"]
        stale_opens  = []
        clean_settled = [r for r in all_records if r.get("status") == "SETTLED"]

    modern_full   = [r for r in clean_settled if _is_modern_r(r)]
    dc_override   = [r for r in modern_full if r.get("data_collection_override")]
    provisional   = [
        r for r in modern_full
        if r.get("bootstrap_provisional") and not r.get("data_collection_override")
    ]
    era_allow     = [r for r in modern_full if r.get("bootstrap_era_council_allow")]
    normal_modern = [
        r for r in modern_full
        if not r.get("data_collection_override") and not r.get("bootstrap_provisional")
    ]

    proof_velocity = {
        "clean_settled":      len(clean_settled),
        "modern_full":        len(modern_full),
        "dc_override":        len(dc_override),
        "provisional":        len(provisional),
        "era_allow":          len(era_allow),
        "normal_modern":      len(normal_modern),
        "active_opens":       len(active_opens),
        "stale_opens":        len(stale_opens),
        "trust_gate_target":  10,
        "scale_gate_target":  30,
        "trust_gate_pct":     round(len(normal_modern) / 10 * 100, 1),
        "scale_gate_pct":     round(len(normal_modern) / 30 * 100, 1),
        "real_money_allowed": False,
        "scale_allowed":      False,
    }

    # ── 2. CLV trend (by time bucket) ─────────────────────────────────────────
    from datetime import timedelta
    clv_trend = {"all_time": None, "last_7d": None, "last_30d": None, "note": ""}
    try:
        now = datetime.now(timezone.utc)
        def _avg_clv(rows):
            vals = [v for v in (get_clv(r) for r in rows) if v is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        def _recent(rows, days):
            cutoff = now - timedelta(days=days)
            return [
                r for r in rows
                if (ts_v := (r.get("timestamp") or r.get("timestamp_utc")))
                and (lambda t: t >= cutoff if t else False)(
                    __import__("datetime").datetime.fromisoformat(
                        str(ts_v).replace("Z", "+00:00")
                    ).replace(tzinfo=timezone.utc)
                    if "+" not in str(ts_v) else
                    __import__("datetime").datetime.fromisoformat(
                        str(ts_v).replace("Z", "+00:00")
                    )
                )
            ]

        # Simpler recency filter
        def _recent_s(rows, days):
            cutoff = now - timedelta(days=days)
            result = []
            for r in rows:
                ts_raw = r.get("timestamp") or r.get("timestamp_utc")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        result.append(r)
                except (ValueError, TypeError):
                    pass
            return result

        clv_trend = {
            "all_time":  _avg_clv(clean_settled),
            "last_30d":  _avg_clv(_recent_s(clean_settled, 30)),
            "last_7d":   _avg_clv(_recent_s(clean_settled, 7)),
            "n_all":     len(clean_settled),
            "n_30d":     len(_recent_s(clean_settled, 30)),
            "n_7d":      len(_recent_s(clean_settled, 7)),
            "note": "Negative CLV = model entering after price moves against it (momentum-chasing signal)",
        }
    except Exception as exc:
        clv_trend["error"] = str(exc)[:120]

    # ── 3. Edge bucket performance summary ────────────────────────────────────
    edge_buckets: dict = {}
    if _TP_CLASSIFY_OK:
        from collections import defaultdict as _dd
        by_eb = _dd(list)
        for r in clean_settled:
            by_eb[_tp_edge_bucket(r)].append(r)
        for bucket, brows in by_eb.items():
            wins_b   = [r for r in brows if get_pnl(r) > 0]
            losses_b = [r for r in brows if get_pnl(r) < 0]
            wagered_b = sum(get_size(r) for r in brows)
            pnl_b    = sum(get_pnl(r) for r in brows)
            clv_b    = [v for v in (get_clv(r) for r in brows) if v is not None]
            edge_buckets[bucket] = {
                "n":      len(brows),
                "wr":     round(len(wins_b) / (len(wins_b) + len(losses_b)), 3) if (wins_b or losses_b) else None,
                "roi":    round(pnl_b / wagered_b, 4) if wagered_b else None,
                "avg_clv": round(sum(clv_b) / len(clv_b), 4) if clv_b else None,
                "sample_warning": len(brows) < 15,
            }

    # ── 4. Market type performance summary ────────────────────────────────────
    market_types: dict = {}
    if _TP_CLASSIFY_OK:
        by_mh = _dd(list)
        for r in clean_settled:
            by_mh[_tp_market_horizon(r)].append(r)
        for mtype, mrows in by_mh.items():
            wins_m   = [r for r in mrows if get_pnl(r) > 0]
            losses_m = [r for r in mrows if get_pnl(r) < 0]
            wagered_m = sum(get_size(r) for r in mrows)
            pnl_m    = sum(get_pnl(r) for r in mrows)
            clv_m    = [v for v in (get_clv(r) for r in mrows) if v is not None]
            market_types[mtype] = {
                "n":      len(mrows),
                "wr":     round(len(wins_m) / (len(wins_m) + len(losses_m)), 3) if (wins_m or losses_m) else None,
                "roi":    round(pnl_m / wagered_m, 4) if wagered_m else None,
                "avg_clv": round(sum(clv_m) / len(clv_m), 4) if clv_m else None,
                "sample_warning": len(mrows) < 15,
            }

    # ── 5. Blocker breakdown (most recent dashboard session) ──────────────────
    blocker_summary: dict = {"error": "funnel log not read"}
    try:
        funnel_path = ROOT / "logs" / "execution_funnel.jsonl"
        if funnel_path.exists():
            funnel_rows = []
            for line in funnel_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        funnel_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            # Most recent dashboard_run session
            run_ids = [r.get("run_id") for r in funnel_rows if r.get("run_id")]
            real_ids = [rid for rid in set(run_ids) if rid and rid.startswith("dashboard_run_")]
            if real_ids:
                latest_id = max(real_ids)
                session_rows = [r for r in funnel_rows if r.get("run_id") == latest_id]
                from collections import Counter as _Counter
                reason_ctr = _Counter(r.get("final_reason", "UNKNOWN") for r in session_rows)
                total = len(session_rows)
                blocker_summary = {
                    "session_id":    latest_id,
                    "total_signals": total,
                    "breakdown":     {k: {"count": v, "pct": round(v / total * 100, 1)} for k, v in reason_ctr.most_common()},
                    "trade_open_rate": round(reason_ctr.get("TRADE_OPENED", 0) / total, 5) if total else 0,
                }
            else:
                blocker_summary = {"error": "no dashboard_run sessions in funnel log"}
        else:
            blocker_summary = {"error": "execution_funnel.jsonl not found"}
    except Exception as exc:
        blocker_summary = {"error": str(exc)[:120]}

    # ── 6. Edge profile freshness (Phase 9D) ─────────────────────────────────
    _EP_REBUILD_THRESH = 24.0   # proactive rebuild threshold (hours)
    ep_status: dict = {"freshness_level": "MISSING", "status": "MISSING", "cells_2d": []}
    try:
        ep_path = ROOT / "data" / "edge_profile.json"
        if ep_path.exists():
            ep_data  = json.loads(ep_path.read_text(encoding="utf-8"))
            raw_ts   = ep_data.get("generated_at") or ep_data.get("timestamp")
            ep_hlth  = ep_data.get("edge_profile_health") or {}
            trusted  = ep_hlth.get("edge_profile_trusted", False)

            try:
                from config.trading_config import EDGE_PROFILE_MAX_AGE_HOURS as _MAX_AGE_H
            except ImportError:
                _MAX_AGE_H = 48

            age_h = None
            if raw_ts:
                _ep_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if _ep_ts.tzinfo is None:
                    _ep_ts = _ep_ts.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - _ep_ts).total_seconds() / 3600

            if age_h is None:
                _fl = "MISSING"
            elif not trusted or age_h >= _MAX_AGE_H:
                _fl = "DANGER"
            elif age_h >= _EP_REBUILD_THRESH:
                _fl = "WATCH"
            else:
                _fl = "FRESH"

            _hours_left = (_MAX_AGE_H - age_h) if age_h is not None else None

            # Count normal_modern non-KXETH trades settled since last rebuild
            # Re-use all_records already loaded at top of function (no double-read)
            _new_nm = 0
            _new_sw = 0
            try:
                if raw_ts:
                    _build_ts = str(raw_ts)[:19]
                    _MK9D = ["council_decision", "bootstrap_provisional",
                              "data_collection_override", "risk_edge",
                              "bootstrap_era_council_allow"]
                    for _r9 in all_records:
                        if _r9.get("status") != "SETTLED":
                            continue
                        if _r9.get("timestamp", "") < _build_ts:
                            continue
                        if any(_r9.get(k) is None for k in _MK9D):
                            continue
                        if _r9.get("data_collection_override") or _r9.get("bootstrap_provisional"):
                            continue
                        if str(_r9.get("ticker", "")).upper().startswith("KXETH"):
                            continue
                        _new_nm += 1
                        try:
                            _ya9 = float(_r9.get("yes_ask") or _r9.get("entry_price") or 0)
                        except (TypeError, ValueError):
                            _ya9 = 0.0
                        if _ya9 >= 0.80:
                            _new_sw += 1
            except Exception:
                pass

            # 2D cell analysis
            try:
                from config.trading_config import (
                    PRICE_CONDITIONED_MIN_N  as _PC_MIN_N,
                    PRICE_CONDITIONED_MIN_WR as _PC_MIN_WR,
                )
            except ImportError:
                _PC_MIN_N, _PC_MIN_WR = 5, 0.80

            _SWEET_KEY9  = "0.05-0.10|0.80-0.90"
            _POISON_KEYS9 = {"0.05-0.10|0.60-0.70", "0.05-0.10|0.70-0.80"}
            _table9  = ep_data.get("profiles", {}).get("by_edge_price_bucket", {})
            _cells9: list = []
            for _ck in sorted(_table9):
                _cc  = _table9[_ck]
                _cn  = int(_cc.get("trades", 0))
                _cwr = float(_cc.get("win_rate", 0))
                _cp  = float(_cc.get("total_pnl", 0))
                if _ck == _SWEET_KEY9:
                    _cv = ("SWEET-SPOT WATCH"
                           if _cn >= _PC_MIN_N and _cwr >= _PC_MIN_WR and _cp > 0
                           else "SWEET-SPOT WEAK")
                elif _ck in _POISON_KEYS9:
                    _cv = "POISON BLOCK" if _cp < 0 else "POISON WARN"
                elif _cn < _PC_MIN_N:
                    _cv = "TOO SMALL"
                else:
                    _cv = "OTHER"
                _cells9.append({"cell": _ck, "n": _cn,
                                "wr": round(_cwr, 3), "pnl": round(_cp, 2), "verdict": _cv})

            _sweet9  = _table9.get(_SWEET_KEY9)
            _sweet_ok = bool(
                _sweet9
                and int(_sweet9.get("trades", 0))    >= _PC_MIN_N
                and float(_sweet9.get("win_rate", 0)) >= _PC_MIN_WR
                and float(_sweet9.get("total_pnl", 0)) > 0
            )

            if _fl == "FRESH" and trusted and _sweet_ok:
                _bs_risk = "LOW"
            elif _fl == "WATCH":
                _bs_risk = "MEDIUM"
            else:
                _bs_risk = "HIGH"

            ep_status = {
                "freshness_level":              _fl,
                "status":                       "STALE" if _fl == "DANGER" else _fl,
                "age_hours":                    round(age_h, 1) if age_h is not None else None,
                "hours_until_stale":            round(_hours_left, 1) if _hours_left is not None else None,
                "generated_at":                 raw_ts,
                "trusted":                      trusted,
                "health_reason":                ep_hlth.get("reason", ""),
                "new_normal_modern_since_rebuild": _new_nm,
                "new_sweet_spot_since_rebuild":  _new_sw,
                "cells_2d":                     _cells9,
                "sweet_spot_qualifies":         _sweet_ok,
                "bootstrap_risk":               _bs_risk,
                "has_2d_table":                 bool(_table9),
                "warning": (
                    "PROFILE STALE — rebuild: cd tools && python3 build_edge_profile.py"
                    if _fl in ("DANGER", "MISSING") else
                    "Profile aging — rebuild soon: cd tools && python3 build_edge_profile.py"
                    if _fl == "WATCH" else ""
                ),
            }
        else:
            ep_status = {
                "freshness_level":  "MISSING",
                "status":           "MISSING",
                "cells_2d":         [],
                "sweet_spot_qualifies": False,
                "bootstrap_risk":   "HIGH",
                "warning":          "edge_profile.json missing — run: cd tools && python3 build_edge_profile.py",
            }
    except Exception as exc:
        ep_status = {
            "freshness_level": "MISSING",
            "status": "ERROR",
            "error": str(exc)[:120],
            "cells_2d": [],
        }

    # ── 7. Lock status ────────────────────────────────────────────────────────
    try:
        from config.trading_config import (
            MIN_EDGE, MIN_CONFIDENCE, GLOBAL_FORCED_LEARNING_MODE,
            EDGE_DANGER_HIGH_EDGE_MIN, BOOTSTRAP_MIN_EDGE,
            BOOTSTRAP_MIN_CONFIDENCE, BOOTSTRAP_ALLOW_ENABLED,
        )
        lock_status = {
            "real_money_allowed":    False,
            "scale_allowed":         False,
            "kelly_execution":       "DISABLED" if GLOBAL_FORCED_LEARNING_MODE else "ENABLED",
            "global_forced_learning": GLOBAL_FORCED_LEARNING_MODE,
            "min_edge":              MIN_EDGE,
            "min_confidence":        MIN_CONFIDENCE,
            "edge_danger_guard_min": EDGE_DANGER_HIGH_EDGE_MIN,
            "bootstrap_allow":       BOOTSTRAP_ALLOW_ENABLED,
            "bootstrap_min_edge":    BOOTSTRAP_MIN_EDGE,
            "bootstrap_min_conf":    BOOTSTRAP_MIN_CONFIDENCE,
            "trading_mode":          "PAPER",
        }
    except Exception as exc:
        lock_status = {"error": str(exc)[:80], "real_money_allowed": False, "scale_allowed": False}

    return {
        "proof_velocity":    proof_velocity,
        "clv_trend":         clv_trend,
        "edge_bucket_perf":  edge_buckets,
        "market_type_perf":  market_types,
        "blocker_summary":   blocker_summary,
        "edge_profile":      ep_status,
        "lock_status":       lock_status,
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "note":              "Read-only operator layer. No trading logic changed.",
    }


@app.route("/api/truth_panels")
def api_truth_panels():
    """
    Extended operator truth panels — read-only diagnostic endpoint.
    Returns proof velocity, CLV trend, edge bucket/market type performance,
    blocker breakdown, edge profile staleness, and lock status.
    No trading logic is affected by this endpoint.
    """
    return jsonify(_compute_truth_panels())


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
