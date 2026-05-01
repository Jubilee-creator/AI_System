"""
brain/paper_trader.py
---------------------
Paper trading system with risk management integration

UPDATED: Phase 4, Step 4
CHANGES: Integrated RiskManager for safety checks
PRESERVED: All existing paper trading logic
"""

import os
import json
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path

# Add parent directory to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import risk manager (PHASE 4 ADDITION)
from brain.risk_manager import RiskManager
from brain.strategy_utils import normalize_strategy

# Import dependencies
from logs.trade_logger import TradeLogger
from engine.edge_calculator import MarketData
from config.trading_config import (
    PAPER_VALIDATION_MODE,
    PAPER_VALIDATION_MAX_BET_SIZE,
    GLOBAL_FORCED_LEARNING_MODE,
    DATA_COLLECTION_MODE,
    DATA_COLLECTION_OVERRIDE_ENABLED,
    MAX_POSITIONS_PER_TICKER,
    MAX_SPREAD,
    MIN_VOLUME,
    MIN_CONFIDENCE as CONFIG_MIN_CONFIDENCE,
    EDGE_DANGER_GUARD_ENABLED,
    EDGE_DANGER_BLOCK_HIGH_EDGE,
    EDGE_DANGER_HIGH_EDGE_MIN,
    EDGE_DANGER_REQUIRE_PAPER_ONLY,
    EDGE_DANGER_REASON,
)

MIN_LEARNING_BET = 5.00
MAX_LEARNING_EXPOSURE = 20.00
MAX_CONCURRENT_OPEN_TRADES = 3


def _compute_clv(entry_price: float, exit_price: float) -> float:
    return round(float(exit_price) - float(entry_price), 4)


def _print_clv(clv: float) -> None:
    if clv > 0:
        label = "favorable"
    elif clv < 0:
        label = "unfavorable"
    else:
        label = "flat"
    print(f"[CLV] value={clv:+.4f} ({label})")


def _market_volume(market_data: MarketData) -> float:
    return float(
        getattr(market_data, "volume_24h", 0)
        or getattr(market_data, "volume", 0)
        or getattr(market_data, "liquidity", 0)
        or 0
    )


def _market_spread(market_data: MarketData, action: str) -> float:
    spread = getattr(market_data, "spread", None)
    if spread is not None:
        return float(spread)

    if action == "BET_NO":
        bid = getattr(market_data, "no_bid", None)
        ask = getattr(market_data, "no_ask", None)
    else:
        bid = getattr(market_data, "yes_bid", None)
        ask = getattr(market_data, "yes_ask", None)

    if bid is None or ask is None:
        return float("inf")
    return max(0.0, float(ask) - float(bid))


def _as_optional_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_midpoint(bid, ask):
    bid = _as_optional_float(bid)
    ask = _as_optional_float(ask)
    if bid is None or ask is None:
        return None
    return round((bid + ask) / 2, 6)


def _paper_trade_metadata(market_data: MarketData, action: str, fee_estimate: float) -> dict:
    yes_price = _as_optional_float(getattr(market_data, "yes_price", None))
    no_price = _as_optional_float(getattr(market_data, "no_price", None))
    yes_bid = _as_optional_float(getattr(market_data, "yes_bid", None))
    yes_ask = _as_optional_float(getattr(market_data, "yes_ask", None))
    no_bid = _as_optional_float(getattr(market_data, "no_bid", None))
    no_ask = _as_optional_float(getattr(market_data, "no_ask", None))
    yes_mid = _market_midpoint(yes_bid, yes_ask)
    no_mid = _market_midpoint(no_bid, no_ask)

    metadata = {
        "price_yes": yes_price,
        "price_no": no_price,
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": yes_mid,
        "no_mid": no_mid,
        "market_mid": no_mid if action == "BET_NO" else yes_mid,
        "spread": _as_optional_float(getattr(market_data, "spread", None)),
        "volume": _as_optional_float(getattr(market_data, "volume_24h", None)),
        "liquidity": _as_optional_float(getattr(market_data, "liquidity", None)),
        "fee_estimate": fee_estimate,
        "source": getattr(market_data, "venue", None),
        "scanner_source": getattr(market_data, "scanner_source", None),
        "market_id": getattr(market_data, "market_id", None),
        "event_id": getattr(market_data, "event_id", None),
        "horizon": getattr(market_data, "horizon", None),
        "title": getattr(market_data, "title", None),
        "question": getattr(market_data, "question", None),
        "reasoning": getattr(market_data, "reasoning", None),
        "decision_reason": getattr(market_data, "decision_reason", None),
        "close_time": getattr(market_data, "close_time", None),
        "result_time": getattr(market_data, "result_time", None),
        "time_to_expiry": _as_optional_float(getattr(market_data, "time_to_expiry", None)),
    }
    return {k: v for k, v in metadata.items() if v is not None}


# ═══════════════════════════════════════════════════════════════
# PAPER TRADER
# ═══════════════════════════════════════════════════════════════

class PaperTrader:
    """
    Paper trading system with full risk management.
    
    PHASE 4 UPDATE: Now integrates RiskManager for safety checks.
    All trades must pass risk checks before execution.
    
    Usage:
        trader = PaperTrader(bankroll=500.0)
        trader.enable()
        
        # Process signal
        result = trader.process_signal(
            market_data=market,
            estimated_prob=0.68,
            strategy="SIGNAL"
        )
    """
    
    def __init__(
        self,
        bankroll: float = 500.0,
        min_edge: float = 0.03,
        min_confidence: float = CONFIG_MIN_CONFIDENCE,
        max_bet_size: float = 50.0,
        kelly_fraction: float = 0.25
    ):
        """
        Initialize paper trader.
        
        Args:
            bankroll: Starting capital
            min_edge: Minimum edge required (after fees)
            min_confidence: Minimum probability required
            max_bet_size: Maximum bet size in dollars
            kelly_fraction: Kelly multiplier (0.25 = quarter Kelly)
        """
        self.bankroll = bankroll
        
        # PHASE 4: Initialize risk manager
        self.risk_manager = RiskManager(bankroll=bankroll)
        
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.max_bet_size = max_bet_size
        self.kelly_fraction = kelly_fraction
        
        # State
        self.enabled = False
        self.total_trades = 0
        self.settled_trades = 0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        
        # Trade tracking
        self.open_trades = []
        self.trade_history = []
        
        # Logger
        self.logger = TradeLogger()

        # Rebuild counters and open trades from persisted log
        self._load_state_from_trade_log()

        print(f"[PAPER] Initialized PaperTrader")
        print(f"  Bankroll: ${self.bankroll:.2f}")
        print(f"  Min edge: {self.min_edge*100:.1f}%")
        print(f"  Min confidence: {self.min_confidence*100:.1f}%")
        print(f"  Max bet: ${self.max_bet_size:.2f}")
        print(f"  Kelly fraction: {self.kelly_fraction}")
    
    
    def _load_state_from_trade_log(self) -> None:
        """
        Rebuild in-memory state from logs/paper_trades.jsonl on startup.

        The log may contain two entries per trade (OPEN then SETTLED).
        Deduplication key: (ticker, timestamp) — last line seen wins, so
        a SETTLED entry always supersedes its corresponding OPEN entry.

        Read-only: never writes to the log during this call.
        Safe to call on missing or malformed log files.
        """
        log_path = self.logger.log_file

        if not os.path.exists(log_path):
            print("[PAPER] No trade log found — starting clean")
            return

        trades_by_key: dict = {}
        lines_skipped = 0

        try:
            with open(log_path, "r") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        trade = json.loads(raw)
                        key = (trade.get("ticker", ""), trade.get("timestamp", ""))
                        trades_by_key[key] = trade
                    except Exception:
                        lines_skipped += 1
                        continue
        except Exception as exc:
            print(f"[PAPER] Warning: could not read trade log: {exc}")
            return

        if lines_skipped:
            print(f"[PAPER] Warning: skipped {lines_skipped} malformed line(s) in trade log")

        for trade in trades_by_key.values():
            status = trade.get("status", "")
            result = trade.get("result")
            pnl    = float(trade.get("pnl") or 0.0)

            if status == "OPEN":
                self.open_trades.append(trade)
                self.total_trades += 1
            elif status == "SETTLED":
                self.trade_history.append(trade)
                self.total_trades  += 1
                self.settled_trades += 1
                self.total_pnl     += pnl
                if result == "WIN":
                    self.wins += 1
                elif result == "LOSS":
                    self.losses += 1

        if self.total_trades > 0:
            print(
                f"[PAPER] State rebuilt: {self.total_trades} total | "
                f"{self.settled_trades} settled | "
                f"{len(self.open_trades)} open | "
                f"P&L=${self.total_pnl:.2f}"
            )


    def enable(self) -> None:
        """Enable paper trading - will log all signals."""
        self.enabled = True
        print(f"[PAPER] ENABLED - Will log all signals to paper_trades.jsonl")
    
    
    def disable(self) -> None:
        """Disable paper trading."""
        self.enabled = False
        print(f"[PAPER] DISABLED")
    
    
    def _calculate_kelly_size(
        self,
        prob: float,
        price: float
    ) -> float:
        """
        Calculate Kelly bet size.
        
        Args:
            prob: Win probability
            price: Entry price
        
        Returns:
            Bet size in dollars
        """
        if prob < self.min_confidence:
            return 0.0
        
        # Payout multiplier (profit per dollar risked)
        b = (1.0 - price) / price
        
        # Kelly formula: f* = (bp - q) / b
        q = 1.0 - prob
        f_star = (b * prob - q) / b
        f_star = max(0.0, f_star)
        
        # Apply Kelly fraction (conservative)
        f = self.kelly_fraction * f_star
        
        # Convert to dollar amount
        bet_size = f * self.bankroll
        
        # Apply caps
        bet_size = min(bet_size, self.max_bet_size)
        bet_size = min(bet_size, self.bankroll * 0.10)  # Never >10% of bankroll
        bet_size = max(bet_size, 0.0)
        
        return round(bet_size, 2)
    
    
    def process_signal(
        self,
        market_data: MarketData,
        estimated_prob: float,
        strategy: str = "SIGNAL",
        intended_action: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Process trading signal through risk manager, then execute if approved.
        
        PHASE 4 UPDATE: Now checks risk manager before execution.
        DEBUG: Added debug prints to trace execution flow.
        
        Args:
            market_data: Market data object
            estimated_prob: Scanner confidence. When intended_action is
                BET_YES or BET_NO, this is treated as side-confidence for that
                intended side. Without a valid intended_action, legacy
                YES-probability fallback behavior is preserved.
            strategy: Strategy label (SIGNAL, ARB, TREND)
            intended_action: Scanner-selected action for execution handoff and
                audit logging when it is BET_YES or BET_NO.
        
        Returns:
            Trade dict if executed, None if skipped/blocked
        """
        raw_strategy = strategy
        strategy = normalize_strategy(strategy)
        scanner_action = (
            str(intended_action).strip().upper()
            if intended_action is not None and str(intended_action).strip()
            else None
        )
        raw_strategy_text = str(raw_strategy or "").strip().upper()
        market_type = ""
        if "_" in raw_strategy_text:
            market_type = raw_strategy_text.rsplit("_", 1)[-1]
        print(
            "[TRACE] entering paper_trader "
            f"ticker={market_data.ticker} "
            f"raw_strategy={raw_strategy_text or strategy} "
            f"strategy={strategy} "
            f"confidence_in={estimated_prob:.3f}"
        )
        
        # DEBUG 1: Check if enabled
        if not self.enabled:
            print("[TRACE] stop: trader disabled")
            print(f"[PAPER_DEBUG] blocked: trader disabled")
            return None
        
        # DEBUG 2: Check minimum confidence
        if estimated_prob < self.min_confidence and strategy != "ARB":
            if estimated_prob < 0.50:
                print(
                    "[TRACE] stop: pre-council confidence below learning floor "
                    f"confidence={estimated_prob:.3f}"
                )
                print(f"[PAPER_DEBUG] blocked: below learning confidence floor | estimated_prob={estimated_prob:.3f} strategy={strategy}")
                return None
            print(
                "[TRACE] allowed mid-confidence to pass pre-filter "
                f"confidence={estimated_prob:.3f} "
                f"min_confidence={self.min_confidence:.3f}"
            )

        # Duplicate-ticker guard: enforce MAX_POSITIONS_PER_TICKER
        if len(self.open_trades) >= MAX_CONCURRENT_OPEN_TRADES:
            print("[FLOW_CONTROL] max_open_trades_reached")
            print(
                "[TRACE] stop: global open-trades cap "
                f"open_trades={len(self.open_trades)} cap={MAX_CONCURRENT_OPEN_TRADES}"
            )
            return None

        ticker_open_count = sum(
            1 for t in self.open_trades if t.get("ticker") == market_data.ticker
        )
        if ticker_open_count >= MAX_POSITIONS_PER_TICKER:
            print(
                "[TRACE] stop: duplicate ticker guard "
                f"ticker={market_data.ticker} open_count={ticker_open_count}"
            )
            print(f"[RISK_BLOCK] Duplicate ticker — skipping {market_data.ticker} "
                  f"({ticker_open_count} open position(s) already)")
            return None

        # Determine action and price.
        #
        # Scanner confidence is side-confidence for scanner BET_YES/BET_NO
        # opportunities. Preserve that side when Dashboard supplies a valid
        # intended_action. Fall back to legacy YES-probability inference only
        # for old callers that do not provide a valid side.
        if scanner_action in ("BET_YES", "BET_NO"):
            action = scanner_action
            price = market_data.no_price if action == "BET_NO" else market_data.yes_price
        elif estimated_prob >= 0.5:
            action = "BET_YES"
            price = market_data.yes_price
        else:
            action = "BET_NO"
            price = market_data.no_price
            estimated_prob = 1.0 - estimated_prob  # Legacy YES-probability fallback
        
        # ARB override
        if strategy == "ARB":
            action = "ARB"
            # For ARB, estimated_prob is near 1.0
            estimated_prob = 0.99

        executed_action = action
        handoff_action_mismatch = (
            scanner_action is not None
            and scanner_action != executed_action
        )

        spread = _market_spread(market_data, action)
        volume = _market_volume(market_data)
        if spread > MAX_SPREAD or volume < MIN_VOLUME:
            print(
                "[TRACE] stop: market quality filter "
                f"spread={spread:.4f} volume={volume:.0f}"
            )
            print(
                "[FILTER] skipped: high spread or low liquidity "
                f"(spread={spread:.4f} max={MAX_SPREAD:.4f}, "
                f"volume={volume:.0f} min={MIN_VOLUME})"
            )
            return None
        
        # Calculate edge (after fees)
        original_confidence = estimated_prob
        fee_estimate = 0.01  # 1¢ fee estimate used by existing edge calculation
        edge = estimated_prob - price - fee_estimate
        original_edge = edge
        council_confidence = original_confidence
        adjusted_edge = original_edge

        # ── Execution-path audit defaults ─────────────────────────────────────
        # Populated progressively below; all safe to read at record-creation time.
        council_decision = "UNKNOWN"
        council_reason = ""
        edge_profile_health = {}
        net_confidence_adjustment = 0.0
        original_kelly_size = 0.0
        size_override_used = False
        final_bet_size_reason = "NORMAL_KELLY"
        risk_manager_approved_initially = False
        risk_manager_block_reason = None
        risk_override_used = False
        risk_override_reason = None
        market_quality_passed = True   # only reachable here if filter already passed
        market_spread_val = spread if spread != float("inf") else None
        market_volume_val = volume
        bootstrap_provisional_trade = False
        bootstrap_provisional_reason = None
        bootstrap_era_allow_trade = False

        # DEBUG 3: Show calculated values
        print(f"[PAPER_DEBUG] action={action} price={price:.3f} estimated_prob={estimated_prob:.3f} edge={edge:.4f} min_edge={self.min_edge:.4f}")

        # M-46: high-edge danger guard.
        # Current audits show edge >= 0.08 is inverted/untrusted in this
        # system.  Block before Council/data-collection/bootstrap can treat
        # fake edge as authority.  This affects new paper entries only.
        edge_danger_guard_active = (
            EDGE_DANGER_GUARD_ENABLED
            and EDGE_DANGER_BLOCK_HIGH_EDGE
            and (not EDGE_DANGER_REQUIRE_PAPER_ONLY or PAPER_VALIDATION_MODE)
        )
        if edge_danger_guard_active and original_edge >= EDGE_DANGER_HIGH_EDGE_MIN:
            print(
                "[EDGE_DANGER_GUARD_HIGH_EDGE] blocked: "
                f"original_edge={original_edge:.4f} "
                f"threshold={EDGE_DANGER_HIGH_EDGE_MIN:.4f} "
                f"reason={EDGE_DANGER_REASON}"
            )
            print(
                "[TRACE] stop: edge danger guard "
                f"ticker={market_data.ticker} action={action} "
                f"original_edge={original_edge:.4f}"
            )
            return None
        
        # DEBUG 4: Check minimum edge
        if edge < self.min_edge and strategy != "ARB":
            print(
                "[TRACE] stop: min edge block "
                f"edge={edge:.4f} min_edge={self.min_edge:.4f}"
            )
            print(f"[PAPER_DEBUG] blocked: below min edge | edge={edge:.4f} min_edge={self.min_edge:.4f} strategy={strategy}")
            return None

        force_data_collection_learning = False

        # Decision Council gate.  Fail-open by design so scanner runtime
        # errors cannot crash or halt the paper trading loop in paper mode.
        try:
            from brain.decision_council import decide_signal

            old_prob = estimated_prob
            council_result = decide_signal({
                "confidence": estimated_prob,
                "edge": edge,
                "ticker": market_data.ticker,
                "strategy": strategy,
                "market_type": market_type,
                "action": action,
            })
            council_decision = council_result.get("final_decision", "ALLOW")
            council_reason = council_result.get("reason", "")
            edge_profile_health = council_result.get("edge_profile_health") or {}
            if edge_profile_health and not edge_profile_health.get("edge_profile_trusted", False):
                print(
                    "[EDGE_PROFILE] UNTRUSTED: "
                    f"{edge_profile_health.get('reason', 'unknown')}"
                )
            if council_decision == "BLOCK":
                if DATA_COLLECTION_OVERRIDE_ENABLED:
                    force_data_collection_learning = True
                    print(
                        "[COUNCIL] DATA_COLLECTION_OVERRIDE allowing blocked "
                        "signal as $5 learning trade"
                    )
                    print(f"[COUNCIL] BLOCKED reason: {council_reason}")
                else:
                    print(
                        "[TRACE] stop: council block "
                        f"confidence_before={old_prob:.3f} reason={council_reason}"
                    )
                    print(f"[COUNCIL] BLOCKED: {council_reason}")
                    return None
            elif council_decision == "PROVISIONAL":
                bootstrap_provisional_trade = True
                bootstrap_provisional_reason = council_reason
                print(
                    "[COUNCIL] BOOTSTRAP_PROVISIONAL: bootstrap candidate "
                    "allowed as $5 learning trade"
                )
                print(f"[COUNCIL] PROVISIONAL reason: {council_reason}")
            elif council_decision == "ALLOW" and council_result.get("bootstrap_era_allow"):
                bootstrap_era_allow_trade = True
                print(
                    "[COUNCIL] BOOTSTRAP_ERA_ALLOW: bootstrap-quality signal allowed "
                    "as normal council-approved trade (counts toward proof accumulation)"
                )
                print(f"[COUNCIL] ALLOW reason: {council_reason}")

            estimated_prob = float(council_result.get("final_confidence", estimated_prob))
            estimated_prob = min(max(estimated_prob, 0.0), 1.0)
            edge = estimated_prob - price - fee_estimate
            council_confidence = estimated_prob
            adjusted_edge = edge
            net_confidence_adjustment = float(council_result.get("net_confidence_adjustment") or 0.0)
            if council_decision == "PROVISIONAL":
                print(f"[COUNCIL] PROVISIONAL: {council_reason}")
            else:
                print(f"[COUNCIL] ALLOW: {council_reason}")
            print(f"[COUNCIL] confidence {old_prob:.3f} -> {estimated_prob:.3f}")
            print(f"[COUNCIL] net_adjustment: {net_confidence_adjustment:+.4f}")
        except Exception as exc:
            print(f"[COUNCIL] ERROR: {exc}")
            council_decision = "ERROR"
            council_reason = str(exc)
            edge_profile_health = {}
            council_confidence = estimated_prob
            adjusted_edge = edge
        
        # Calculate bet size
        is_learning_trade = False
        bet_size = self._calculate_kelly_size(estimated_prob, price)
        original_kelly_size = bet_size  # audit: true Kelly size before any override

        # Validation-mode cap: limit trade size while collecting edge data.
        # Daily loss limit, exposure check, min_edge, and min_confidence are unchanged.
        if PAPER_VALIDATION_MODE and bet_size > PAPER_VALIDATION_MAX_BET_SIZE:
            print(f"[PAPER_DEBUG] validation cap applied: original_size={bet_size:.2f} capped_size={PAPER_VALIDATION_MAX_BET_SIZE:.2f}")
            bet_size = PAPER_VALIDATION_MAX_BET_SIZE
            size_override_used = True
            final_bet_size_reason = "VALIDATION_CAP"

        # DEBUG 5: Show bet size calculation
        print(f"[PAPER_DEBUG] bet_size={bet_size:.2f} bankroll={self.bankroll:.2f} max_bet_size={self.max_bet_size:.2f}")
        print(
            "[TRACE] after Kelly calculation "
            f"confidence={estimated_prob:.3f} "
            f"kelly_size=${bet_size:.2f}"
        )

        if force_data_collection_learning:
            bet_size = MIN_LEARNING_BET
            is_learning_trade = True
            size_override_used = True
            final_bet_size_reason = "DATA_COLLECTION_OVERRIDE"
        elif bootstrap_provisional_trade:
            bet_size = MIN_LEARNING_BET
            is_learning_trade = True
            size_override_used = True
            final_bet_size_reason = "BOOTSTRAP_PROVISIONAL_LEARNING"
            print(
                "[BOOTSTRAP] provisional learning trade "
                f"orig_edge={original_edge:.4f} "
                f"size=${MIN_LEARNING_BET:.2f}"
            )
        elif 0.50 <= estimated_prob < self.min_confidence:
            bet_size = MIN_LEARNING_BET
            is_learning_trade = True
            size_override_used = True
            final_bet_size_reason = "MID_CONFIDENCE_LEARNING_OVERRIDE"
            print(
                "[LEARNING] MID_CONFIDENCE_LEARNING_OVERRIDE "
                f"original_confidence={estimated_prob:.3f} "
                f"original_kelly_size=${original_kelly_size:.2f} "
                f"final_size=${MIN_LEARNING_BET:.2f}"
            )
        elif estimated_prob < 0.50:
            print(
                "[TRACE] stop: below learning confidence floor "
                f"confidence={estimated_prob:.3f}"
            )
            print(
                "[PAPER_DEBUG] blocked: confidence below learning floor | "
                f"confidence={estimated_prob:.3f}"
            )
            return None
        print(
            "[TRACE] after learning override "
            f"final_size=${bet_size:.2f} "
            f"learning_trade={is_learning_trade}"
        )

        # Global forced learning mode: Kelly is audit-only while edge is unproven.
        # This intentionally forces every paper entry to MIN_LEARNING_BET before
        # risk checks.  Set GLOBAL_FORCED_LEARNING_MODE=False only after proof
        # gates and human review justify testing actual Kelly-derived sizing.
        if GLOBAL_FORCED_LEARNING_MODE:
            if bet_size != MIN_LEARNING_BET or not is_learning_trade:
                print(
                    "[SIZE_OVERRIDE] GLOBAL_FORCED_LEARNING_MODE "
                    f"kelly_audit_size=${original_kelly_size:.2f} "
                    f"final_size=${MIN_LEARNING_BET:.2f}"
                )
                if not size_override_used:
                    size_override_used = True
                    final_bet_size_reason = "GLOBAL_FORCED_LEARNING_MODE"
            bet_size = MIN_LEARNING_BET
            is_learning_trade = True
        
        # DEBUG 6: Check bet size is positive.  If the Council allowed but
        # Kelly sizes to zero, keep collecting paper data with a tiny bet.
        if bet_size <= 0:
            if edge >= self.min_edge or strategy == "ARB":
                bet_size = MIN_LEARNING_BET
                is_learning_trade = True
                if not size_override_used:
                    size_override_used = True
                    final_bet_size_reason = "KELLY_ZERO_MIN_LEARNING"
                print(
                    "[LEARNING] Kelly size was 0; using minimum learning bet: "
                    f"${MIN_LEARNING_BET:.2f}"
                )
            else:
                print("[TRACE] stop: bet size <= 0 after learning checks")
                print(f"[PAPER_DEBUG] blocked: bet size <= 0")
                return None

        risk_edge = (
            original_edge
            if (force_data_collection_learning or bootstrap_provisional_trade)
            else edge
        )
        if force_data_collection_learning:
            print(
                "[COUNCIL] DATA_COLLECTION_OVERRIDE preserving original edge "
                f"for risk check original_edge={original_edge:.4f} "
                f"council_edge={edge:.4f}"
            )
        
        # DEBUG 7: About to call risk manager
        print(
            "[TRACE] sending to risk_manager "
            f"confidence={estimated_prob:.3f} "
            f"edge={risk_edge:.4f} "
            f"final_size=${bet_size:.2f} "
            f"learning_trade={is_learning_trade}"
        )
        print(f"[PAPER_DEBUG] sending to risk manager")
        
        # ═══════════════════════════════════════════════════════
        # PHASE 4: RISK MANAGER CHECK
        # ═══════════════════════════════════════════════════════
        
        approved, block_reason = self.risk_manager.check_trade(
            ticker=market_data.ticker,
            action=action,
            size=bet_size,
            confidence=estimated_prob,
            edge=risk_edge,
            strategy=strategy,
            learning_trade=is_learning_trade
        )
        print(
            "[TRACE] risk decision="
            f"{'ALLOW' if approved else 'BLOCK'} reason={block_reason}"
        )
        risk_manager_approved_initially = approved
        risk_manager_block_reason = block_reason if not approved else None

        if not approved:
            can_override_learning_risk = (
                is_learning_trade
                and "Effective daily risk includes open exposure" in block_reason
                and self.risk_manager.total_exposure < MAX_LEARNING_EXPOSURE
            )
            if can_override_learning_risk:
                approved = True
                risk_override_used = True
                risk_override_reason = (
                    f"LEARNING_EXPOSURE_WITHIN_LIMIT "
                    f"(exposure=${self.risk_manager.total_exposure:.2f} "
                    f"< max=${MAX_LEARNING_EXPOSURE:.2f}) "
                    f"original_block={block_reason}"
                )
                print("[LEARNING] Risk override: allowing small learning trade")
                print("[TRACE] risk decision=ALLOW reason=learning risk override")
            elif is_learning_trade:
                print(f"[LEARNING] Risk override denied: {block_reason}")

        if not approved:
            print(f"[PAPER] ❌ Trade blocked by risk manager: {block_reason}")
            print(f"  Ticker: {market_data.ticker}")
            print(f"  Action: {action}")
            print(f"  Size: ${bet_size:.2f}")
            return None
        
        # ═══════════════════════════════════════════════════════
        # TRADE APPROVED - EXECUTE
        # ═══════════════════════════════════════════════════════
        print("[TRACE] executing trade")
        
        # Create trade record
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": market_data.ticker,
            "action": action,
            "scanner_action": scanner_action,
            "intended_action": scanner_action,
            "executed_action": executed_action,
            "handoff_action_mismatch": handoff_action_mismatch,
            "strategy": strategy,
            "entry_price": price,
            "size": bet_size,
            "confidence": estimated_prob,
            "edge": edge,
            "original_confidence": original_confidence,
            "council_confidence": council_confidence,
            "original_edge": original_edge,
            "adjusted_edge": adjusted_edge,
            "risk_edge": risk_edge,
            "model_probability": original_confidence,
            "data_collection_mode": DATA_COLLECTION_MODE,
            "data_collection_override_enabled": DATA_COLLECTION_OVERRIDE_ENABLED,
            "data_collection_override": force_data_collection_learning,
            "learning_trade": is_learning_trade,
            "raw_strategy": raw_strategy_text or strategy,
            "status": "OPEN",
            "result": None,
            "pnl": 0.0,
            "exit_price": None,
            "settled_at": None,
            # ── Execution-path audit fields ──────────────────────
            "council_decision": council_decision,
            "council_reason": council_reason,
            "edge_profile_trusted": edge_profile_health.get("edge_profile_trusted"),
            "edge_profile_reason": edge_profile_health.get("reason"),
            "edge_profile_age_hours": edge_profile_health.get("edge_profile_age_hours"),
            "edge_profile_clean_settled": edge_profile_health.get("clean_settled_trades"),
            "edge_profile_modern_full": edge_profile_health.get("modern_full_metadata_trades"),
            "edge_profile_normal_modern": edge_profile_health.get("normal_council_approved_modern_trades"),
            "net_confidence_adjustment": net_confidence_adjustment,
            "original_kelly_size": original_kelly_size,
            "kelly_sizing_used": not GLOBAL_FORCED_LEARNING_MODE,
            "global_forced_learning_mode": GLOBAL_FORCED_LEARNING_MODE,
            "size_override_used": size_override_used,
            "final_bet_size_reason": final_bet_size_reason,
            "risk_manager_approved_initially": risk_manager_approved_initially,
            "risk_manager_block_reason": risk_manager_block_reason,
            "risk_override_used": risk_override_used,
            "risk_override_reason": risk_override_reason,
            "market_spread": market_spread_val,
            "market_volume": market_volume_val,
            "market_quality_passed": market_quality_passed,
            "bootstrap_provisional": bootstrap_provisional_trade,
            "bootstrap_provisional_reason": bootstrap_provisional_reason,
            "bootstrap_era_council_allow": bootstrap_era_allow_trade,
        }
        trade.update(_paper_trade_metadata(market_data, action, fee_estimate))
        
        # Log to file
        self.logger.log_trade(trade)
        
        # Track internally
        self.open_trades.append(trade)
        self.total_trades += 1
        
        # PHASE 4: Track position with risk manager
        self.risk_manager.add_position(bet_size)
        
        # DEBUG 9: Trade created successfully
        print(f"[PAPER_DEBUG] trade created successfully")
        
        print(f"[PAPER] ✅ Trade opened: {action} {market_data.ticker} ${bet_size:.2f} @ {price:.2f}")
        
        return trade
    
    
    def settle_trade(
        self,
        ticker: str,
        outcome: str  # "YES" or "NO"
    ) -> Optional[Dict[str, Any]]:
        """
        Settle a trade when outcome is known.
        
        PHASE 4 UPDATE: Now records result with risk manager.
        
        Args:
            ticker: Market ticker
            outcome: Final outcome ("YES" or "NO")
        
        Returns:
            Settled trade dict or None
        """
        
        # Find open trade
        trade = None
        for t in self.open_trades:
            if t["ticker"] == ticker and t["status"] == "OPEN":
                trade = t
                break
        
        if not trade:
            print(f"[PAPER] No open trade found for {ticker}")
            return None
        
        # Determine win/loss
        won = False
        if trade["action"] == "BET_YES" and outcome == "YES":
            won = True
        elif trade["action"] == "BET_NO" and outcome == "NO":
            won = True
        elif trade["action"] == "ARB":
            won = True  # ARB always wins (bought both sides)
        
        # Calculate P&L
        if won:
            # Profit = (1 - entry_price) * size
            pnl = (1.0 - trade["entry_price"]) * trade["size"]
            result = "WIN"
            self.wins += 1
        else:
            # Loss = -size
            pnl = -trade["size"]
            result = "LOSS"
            self.losses += 1
        
        # Update trade record
        trade["status"] = "SETTLED"
        trade["result"] = result
        trade["pnl"] = round(pnl, 2)
        trade["exit_price"] = 1.0 if won else 0.0
        trade["clv"] = _compute_clv(trade["entry_price"], trade["exit_price"])
        trade["settled_at"] = datetime.now(timezone.utc).isoformat()
        
        # Update totals
        self.total_pnl += pnl
        self.settled_trades += 1
        
        # ═══════════════════════════════════════════════════════
        # PHASE 4: UPDATE RISK MANAGER
        # ═══════════════════════════════════════════════════════
        
        self.risk_manager.close_position(trade["size"])
        self.risk_manager.record_result(
            pnl=pnl,
            result=result,
            size=trade["size"],
            ticker=trade["ticker"]
        )
        
        # Move to history
        self.open_trades.remove(trade)
        self.trade_history.append(trade)
        
        # Update log
        self.logger.log_trade(trade)
        
        print(f"[PAPER] 💰 Trade settled: {ticker} → {result} (${pnl:+.2f})")
        _print_clv(trade["clv"])
        
        return trade
    
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get paper trading statistics.
        
        Returns:
            Dict with all stats
        """
        
        # Win rate
        win_rate = self.wins / self.settled_trades if self.settled_trades > 0 else 0.0
        
        # Average edge (from settled trades)
        edges = [t["edge"] for t in self.trade_history if "edge" in t]
        avg_edge = sum(edges) / len(edges) if edges else 0.0
        
        # Average EV
        avg_ev = self.total_pnl / self.settled_trades if self.settled_trades > 0 else 0.0
        
        # CLV (closing line value - simplified)
        avg_clv = avg_edge  # Simplified - real CLV needs closing prices
        
        # Sharpe ratio (simplified)
        if self.settled_trades > 1:
            pnls = [t["pnl"] for t in self.trade_history]
            import statistics
            mean_pnl = statistics.mean(pnls)
            std_pnl = statistics.stdev(pnls)
            sharpe = (mean_pnl / std_pnl) if std_pnl > 0 else 0.0
        else:
            sharpe = 0.0
        
        # Verdicts
        is_profitable = self.total_pnl > 0
        has_edge = avg_edge > 0
        beats_closing = avg_clv > 0
        
        return {
            "total_trades": self.total_trades,
            "settled_trades": self.settled_trades,
            "open_trades": len(self.open_trades),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(self.total_pnl, 2),
            "avg_edge": round(avg_edge, 4),
            "avg_ev": round(avg_ev, 3),
            "avg_clv": round(avg_clv, 3),
            "sharpe": round(sharpe, 2),
            "is_profitable": is_profitable,
            "has_edge": has_edge,
            "beats_closing": beats_closing,
            "bankroll": self.bankroll,
            "current_balance": round(self.bankroll + self.total_pnl, 2)
        }
    
    
    def get_risk_status(self) -> Dict[str, Any]:
        """
        Get risk manager status.
        
        PHASE 4 ADDITION: Expose risk manager state.
        
        Returns:
            Risk manager status dict
        """
        return self.risk_manager.get_status()
    
    
    def print_stats(self) -> None:
        """Print statistics summary."""
        stats = self.get_stats()
        
        print("\n" + "="*60)
        print("PAPER TRADING STATS")
        print("="*60)
        print(f"Trades: {stats['settled_trades']} settled, {stats['open_trades']} open")
        print(f"Win rate: {stats['win_rate']:.1%}")
        print(f"P&L: ${stats['total_pnl']:+.2f}")
        print(f"Avg edge: {stats['avg_edge']:.3f}")
        print(f"Avg EV: ${stats['avg_ev']:+.3f}")
        print(f"Sharpe: {stats['sharpe']:.2f}")
        print(f"Balance: ${stats['current_balance']:.2f}")
        print("="*60 + "\n")


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*60)
    print("PAPER TRADER TEST (with Risk Manager)")
    print("="*60)
    
    # Initialize
    trader = PaperTrader(bankroll=500.0)
    trader.enable()
    
    # Create test market
    market = MarketData(
        ticker="TEST-001",
        yes_price=0.45,
        no_price=0.55,
        yes_bid=0.44,
        yes_ask=0.46,
        no_bid=0.54,
        no_ask=0.56,
        volume_24h=1000,
        spread=0.02,
        liquidity=500,
        fee_rate=0.01,
        time_to_expiry=24.0,
        venue="kalshi"
    )
    
    print("\n[TEST 1] Process valid signal (should pass risk check)")
    trade1 = trader.process_signal(market, estimated_prob=0.68, strategy="SIGNAL")
    
    print("\n[TEST 2] Settle trade as WIN")
    if trade1:
        trader.settle_trade("TEST-001", outcome="YES")
    
    print("\n[TEST 3] Trigger loss streak")
    for i in range(3):
        market_loss = MarketData(
            ticker=f"TEST-LOSS-{i}",
            yes_price=0.45, no_price=0.55,
            yes_bid=0.44, yes_ask=0.46,
            no_bid=0.54, no_ask=0.56,
            volume_24h=1000, spread=0.02,
            liquidity=500, fee_rate=0.01,
            time_to_expiry=24.0, venue="kalshi"
        )
        trade_loss = trader.process_signal(market_loss, estimated_prob=0.65, strategy="SIGNAL")
        if trade_loss:
            trader.settle_trade(f"TEST-LOSS-{i}", outcome="NO")  # Force loss
    
    print("\n[TEST 4] Try to trade during cooldown (should be blocked)")
    market3 = MarketData(
        ticker="TEST-003",
        yes_price=0.45, no_price=0.55,
        yes_bid=0.44, yes_ask=0.46,
        no_bid=0.54, no_ask=0.56,
        volume_24h=1000, spread=0.02,
        liquidity=500, fee_rate=0.01,
        time_to_expiry=24.0, venue="kalshi"
    )
    trade3 = trader.process_signal(market3, estimated_prob=0.70, strategy="SIGNAL")
    
    print("\n[TEST 5] Stats")
    trader.print_stats()
    
    print("\n[TEST 6] Risk manager status")
    print(trader.risk_manager.get_limits_summary())
    
    print("\n" + "="*60)
    print("✅ All tests complete")
    print("="*60 + "\n")
