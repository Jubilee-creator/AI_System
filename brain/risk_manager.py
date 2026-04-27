"""
brain/risk_manager.py
---------------------
Core risk management system - THE SAFETY LAYER

PHASE: 4, Step 3
PURPOSE: Enforce ALL trading limits before any trade executes
SCOPE: All trades (paper + live) must pass through here

Safety checks:
1. Kill switch (manual emergency stop)
2. Daily loss limit (circuit breaker)
3. Position size limits
4. Loss streak cooldowns
5. Max open positions
6. Edge/confidence thresholds
7. Trading hours (optional)

Integration:
- Imports config from config/trading_config.py
- Logs events to logs/risk_logger.py
- Used by brain/paper_trader.py (and future live trader)

State tracking:
- Daily P&L
- Open positions count
- Loss streak
- Cooldown expiry
- Trade count today
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

# Import config
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.trading_config import (
    DAILY_LOSS_LIMIT,
    WEEKLY_LOSS_LIMIT,
    MAX_POSITION_SIZE,
    MAX_BANKROLL_PCT,
    MAX_OPEN_POSITIONS,
    MAX_TRADES_PER_DAY,
    LOSS_STREAK_TRIGGER,
    COOLDOWN_MINUTES,
    MIN_EDGE,
    MIN_CONFIDENCE,
    MIN_ARB_EDGE,
    KILL_SWITCH_ACTIVE,
    TRADING_PAUSED,
    TRADING_HOURS,
    INITIAL_BANKROLL,
    STRATEGY_LIMITS,
    STATE_FILE_PATH,
)

from logs.risk_logger import RiskLogger


# ═══════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════

class RiskManager:
    """
    Core risk management system.
    
    ALL trades must pass check_trade() before execution.
    Updates state via record_result() after each trade.
    
    Usage:
        rm = RiskManager()
        
        # Before trade
        approved, reason = rm.check_trade(signal)
        if not approved:
            logger.log(f"Trade blocked: {reason}")
            return
        
        # Execute trade...
        
        # After trade
        rm.record_result(trade_result)
    """
    
    def __init__(
        self,
        bankroll: float = INITIAL_BANKROLL,
        state_file: Optional[str] = None
    ):
        """
        Initialize risk manager.
        
        Args:
            bankroll: Starting bankroll
            state_file: Path to state persistence file
        """
        self.bankroll = bankroll
        self.state_file = state_file or STATE_FILE_PATH
        
        # Daily state (resets at midnight)
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.last_reset_date = datetime.now(timezone.utc).date()
        
        # Position tracking
        self.open_positions = 0
        self.total_exposure = 0.0  # $ amount in open positions
        
        # Loss tracking
        self.loss_streak = 0
        self.last_trade_result = None  # "WIN" or "LOSS"
        
        # Cooldown state
        self.cooldown_until = None  # datetime or None
        self.cooldown_reason = ""
        
        # Kill switch
        self.kill_switch_active = KILL_SWITCH_ACTIVE
        self.trading_paused = TRADING_PAUSED
        
        # Weekly tracking
        self.weekly_pnl = 0.0
        self.week_start_date = self._get_week_start()
        
        # Logger
        self.logger = RiskLogger()
        
        # Load persisted state
        self._load_state()
        
        # Check if daily reset needed
        self._check_daily_reset()
        
        print(f"[RISK_MANAGER] Initialized")
        print(f"  Bankroll: ${self.bankroll:.2f}")
        print(f"  Daily P&L: ${self.daily_pnl:.2f}")
        print(f"  Loss limit: ${DAILY_LOSS_LIMIT:.2f}")
        print(f"  Kill switch: {'ACTIVE 🚨' if self.kill_switch_active else 'OFF'}")
    
    
    # ───────────────────────────────────────────────────────────
    # STATE MANAGEMENT
    # ───────────────────────────────────────────────────────────
    
    def _get_week_start(self) -> datetime.date:
        """Get start of current week (Monday)."""
        today = datetime.now(timezone.utc).date()
        return today - timedelta(days=today.weekday())
    
    
    def _load_state(self) -> None:
        """Load persisted state from file."""
        if not os.path.exists(self.state_file):
            return
        
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.weekly_pnl = state.get("weekly_pnl", 0.0)
            self.trades_today = state.get("trades_today", 0)
            self.open_positions = state.get("open_positions", 0)
            self.total_exposure = state.get("total_exposure", 0.0)
            self.loss_streak = state.get("loss_streak", 0)
            self.last_trade_result = state.get("last_trade_result")
            
            # Load dates
            if state.get("last_reset_date"):
                self.last_reset_date = datetime.fromisoformat(
                    state["last_reset_date"]
                ).date()
            
            if state.get("week_start_date"):
                self.week_start_date = datetime.fromisoformat(
                    state["week_start_date"]
                ).date()
            
            # Load cooldown
            if state.get("cooldown_until"):
                self.cooldown_until = datetime.fromisoformat(
                    state["cooldown_until"]
                )
                self.cooldown_reason = state.get("cooldown_reason", "")
            
            # Load kill switch state
            self.kill_switch_active = state.get("kill_switch_active", False)
            
            print(f"[RISK_MANAGER] State loaded from {self.state_file}")
        
        except Exception as e:
            print(f"[RISK_MANAGER] Failed to load state: {e}")
    
    
    def _save_state(self) -> None:
        """Persist state to file."""
        state = {
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "trades_today": self.trades_today,
            "open_positions": self.open_positions,
            "total_exposure": self.total_exposure,
            "loss_streak": self.loss_streak,
            "last_trade_result": self.last_trade_result,
            "last_reset_date": self.last_reset_date.isoformat(),
            "week_start_date": self.week_start_date.isoformat(),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "cooldown_reason": self.cooldown_reason,
            "kill_switch_active": self.kill_switch_active,
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
        
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        
        except Exception as e:
            print(f"[RISK_MANAGER] Failed to save state: {e}")
    
    
    def _check_daily_reset(self) -> None:
        """Check if daily counters need reset (new day)."""
        today = datetime.now(timezone.utc).date()
        
        if today > self.last_reset_date:
            # Log previous day stats
            self.logger.log_daily_reset(
                previous_pnl=self.daily_pnl,
                trades_yesterday=self.trades_today
            )
            
            # Reset daily counters
            previous_pnl = self.daily_pnl
            previous_trades = self.trades_today
            
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.last_reset_date = today
            
            # Check weekly reset
            if today >= self.week_start_date + timedelta(days=7):
                self.weekly_pnl = 0.0
                self.week_start_date = self._get_week_start()
            
            # Clear cooldown if it was from previous day
            if self.cooldown_until and self.cooldown_until.date() < today:
                self.cooldown_until = None
                self.cooldown_reason = ""
            
            self._save_state()
            
            print(f"[RISK_MANAGER] Daily reset")
            print(f"  Yesterday: ${previous_pnl:.2f} P&L, {previous_trades} trades")
            print(f"  Today: Fresh start")
    
    
    # ───────────────────────────────────────────────────────────
    # KILL SWITCH & PAUSE
    # ───────────────────────────────────────────────────────────
    
    def activate_kill_switch(self, reason: str = "Manual emergency stop") -> None:
        """
        Activate kill switch - STOP ALL TRADING.
        
        Args:
            reason: Reason for activation
        """
        self.kill_switch_active = True
        self._save_state()
        
        self.logger.log_kill_switch_activated(
            reason=reason,
            activated_by="manual"
        )
        
        print(f"\n{'='*60}")
        print("🚨 KILL SWITCH ACTIVATED 🚨")
        print(f"Reason: {reason}")
        print("ALL TRADING STOPPED")
        print(f"{'='*60}\n")
    
    
    def deactivate_kill_switch(self) -> None:
        """Deactivate kill switch - resume trading."""
        self.kill_switch_active = False
        self._save_state()
        
        self.logger.log_kill_switch_deactivated(
            deactivated_by="manual"
        )
        
        print(f"\n{'='*60}")
        print("✅ Kill switch deactivated")
        print("Trading resumed")
        print(f"{'='*60}\n")
    
    
    def pause_trading(self, reason: str = "Manual pause") -> None:
        """Temporary pause (not as severe as kill switch)."""
        self.trading_paused = True
        
        self.logger.log_event(
            event_type="TRADING_PAUSED",
            details={"reason": reason},
            severity="WARNING"
        )
        
        print(f"[RISK_MANAGER] Trading paused: {reason}")
    
    
    def resume_trading(self) -> None:
        """Resume after pause."""
        self.trading_paused = False
        
        self.logger.log_event(
            event_type="TRADING_RESUMED",
            details={},
            severity="INFO"
        )
        
        print(f"[RISK_MANAGER] Trading resumed")
    
    
    # ───────────────────────────────────────────────────────────
    # CORE: CHECK TRADE (BEFORE EXECUTION)
    # ───────────────────────────────────────────────────────────
    
    def check_trade(
        self,
        ticker: str,
        action: str,
        size: float,
        confidence: float,
        edge: float,
        strategy: str = "SIGNAL"
    ) -> Tuple[bool, str]:
        """
        Check if trade is allowed.
        
        ALL TRADES must pass this before execution.
        
        Args:
            ticker: Market ticker
            action: "BET_YES", "BET_NO", "ARB", etc.
            size: Dollar amount of trade
            confidence: Model probability
            edge: Net edge after fees
            strategy: "ARB", "SIGNAL", "TREND"
        
        Returns:
            (approved: bool, reason: str)
            - If approved: (True, "")
            - If blocked: (False, "Reason trade was blocked")
        """
        
        # Auto-reset if new day
        self._check_daily_reset()
        
        # ── CHECK 1: Kill switch ──
        if self.kill_switch_active:
            reason = "Kill switch active - all trading stopped"
            self.logger.log_trade_blocked(ticker, reason)
            return False, reason
        
        # ── CHECK 2: Trading paused ──
        if self.trading_paused:
            reason = "Trading paused"
            self.logger.log_trade_blocked(ticker, reason)
            return False, reason
        
        # ── CHECK 3: Daily loss limit (realized) ──
        if self.daily_pnl <= DAILY_LOSS_LIMIT:
            reason = f"Daily loss limit hit: ${self.daily_pnl:.2f} / ${DAILY_LOSS_LIMIT:.2f}"
            self.logger.log_loss_limit_hit(
                daily_pnl=self.daily_pnl,
                loss_limit=DAILY_LOSS_LIMIT,
                trades_today=self.trades_today
            )

            # Auto-activate kill switch on loss limit hit
            if not self.kill_switch_active:
                self.activate_kill_switch(reason="Daily loss limit reached")

            return False, reason

        # ── CHECK 3b: Effective daily risk (realized P&L + open exposure) ──
        # Worst-case: all open positions lose their full size.
        effective_daily_risk = self.daily_pnl - self.total_exposure
        if effective_daily_risk <= DAILY_LOSS_LIMIT:
            reason = (
                f"Effective daily risk breaches limit — "
                f"daily_pnl=${self.daily_pnl:.2f} "
                f"open_exposure=${self.total_exposure:.2f} "
                f"effective=${effective_daily_risk:.2f} "
                f"limit=${DAILY_LOSS_LIMIT:.2f}"
            )
            self.logger.log_event(
                event_type="TRADE_BLOCKED",
                details={
                    "reason": "Effective daily risk includes open exposure",
                    "daily_pnl": self.daily_pnl,
                    "open_exposure": self.total_exposure,
                    "effective_daily_risk": effective_daily_risk,
                    "daily_loss_limit": DAILY_LOSS_LIMIT,
                },
                severity="CRITICAL"
            )
            return False, reason

        # ── CHECK 4: Weekly loss limit ──
        if self.weekly_pnl <= WEEKLY_LOSS_LIMIT:
            reason = f"Weekly loss limit hit: ${self.weekly_pnl:.2f} / ${WEEKLY_LOSS_LIMIT:.2f}"
            self.logger.log_event(
                event_type="WEEKLY_LOSS_LIMIT_HIT",
                details={
                    "weekly_pnl": self.weekly_pnl,
                    "loss_limit": WEEKLY_LOSS_LIMIT
                },
                severity="CRITICAL"
            )
            return False, reason
        
        # ── CHECK 5: Max trades per day ──
        if self.trades_today >= MAX_TRADES_PER_DAY:
            reason = f"Max trades per day reached: {self.trades_today}/{MAX_TRADES_PER_DAY}"
            self.logger.log_trade_blocked(ticker, reason)
            return False, reason
        
        # ── CHECK 6: Cooldown period ──
        if self.cooldown_until:
            now = datetime.now(timezone.utc)
            if now < self.cooldown_until:
                time_remaining = (self.cooldown_until - now).total_seconds() / 60
                reason = f"Cooldown active: {time_remaining:.0f}min remaining ({self.cooldown_reason})"
                self.logger.log_cooldown_active(
                    ticker=ticker,
                    time_remaining_minutes=time_remaining
                )
                return False, reason
            else:
                # Cooldown expired
                self.cooldown_until = None
                self.cooldown_reason = ""
        
        # ── CHECK 7: Position size limits ──
        
        # Get strategy-specific limits
        strategy_config = STRATEGY_LIMITS.get(strategy, STRATEGY_LIMITS["SIGNAL"])
        max_size = strategy_config.get("max_position_size", MAX_POSITION_SIZE)
        
        # Global max position size
        if size > MAX_POSITION_SIZE:
            reason = f"Position size ${size:.2f} exceeds global max ${MAX_POSITION_SIZE:.2f}"
            self.logger.log_position_size_blocked(
                ticker=ticker,
                requested_size=size,
                max_size=MAX_POSITION_SIZE,
                reason=reason
            )
            return False, reason
        
        # Strategy-specific max
        if size > max_size:
            reason = f"Position size ${size:.2f} exceeds {strategy} max ${max_size:.2f}"
            self.logger.log_position_size_blocked(
                ticker=ticker,
                requested_size=size,
                max_size=max_size,
                reason=reason
            )
            return False, reason
        
        # Bankroll % limit
        bankroll_pct = size / self.bankroll
        if bankroll_pct > MAX_BANKROLL_PCT:
            reason = f"Position size {bankroll_pct:.1%} exceeds max {MAX_BANKROLL_PCT:.1%} of bankroll"
            self.logger.log_position_size_blocked(
                ticker=ticker,
                requested_size=size,
                max_size=self.bankroll * MAX_BANKROLL_PCT,
                reason=reason
            )
            return False, reason
        
        # ── CHECK 8: Max open positions ──
        max_positions = strategy_config.get("max_positions", MAX_OPEN_POSITIONS)
        
        if self.open_positions >= max_positions:
            reason = f"Max open positions reached: {self.open_positions}/{max_positions}"
            self.logger.log_max_positions_reached(
                ticker=ticker,
                current_positions=self.open_positions,
                max_positions=max_positions
            )
            return False, reason
        
        # ── CHECK 9: Edge threshold ──
        min_edge_required = strategy_config.get("min_edge", MIN_EDGE)
        
        # ARB trades can have lower edge
        if action == "ARB":
            min_edge_required = MIN_ARB_EDGE
        
        if edge < min_edge_required:
            reason = f"Edge {edge:.4f} below minimum {min_edge_required:.4f}"
            self.logger.log_insufficient_edge(
                ticker=ticker,
                edge=edge,
                min_edge=min_edge_required
            )
            return False, reason
        
        # ── CHECK 10: Confidence threshold ──
        if action != "ARB":  # ARB doesn't need confidence check
            min_conf_required = strategy_config.get("min_confidence", MIN_CONFIDENCE)
            
            if confidence < min_conf_required:
                reason = f"Confidence {confidence:.2%} below minimum {min_conf_required:.2%}"
                self.logger.log_insufficient_confidence(
                    ticker=ticker,
                    confidence=confidence,
                    min_confidence=min_conf_required
                )
                return False, reason
        
        # ── CHECK 11: Trading hours (optional) ──
        if TRADING_HOURS.get("enabled", False):
            now = datetime.now(timezone.utc)
            # This is a simplified check - full implementation would handle timezones
            hour = now.hour
            start_hour = TRADING_HOURS.get("start").hour
            end_hour = TRADING_HOURS.get("end").hour
            
            if not (start_hour <= hour < end_hour):
                reason = f"Outside trading hours ({start_hour}:00 - {end_hour}:00 ET)"
                self.logger.log_outside_trading_hours(
                    ticker=ticker,
                    current_time=now.strftime("%H:%M:%S")
                )
                return False, reason
        
        # ── ALL CHECKS PASSED ──
        return True, ""
    
    
    # ───────────────────────────────────────────────────────────
    # RECORD RESULT (AFTER EXECUTION)
    # ───────────────────────────────────────────────────────────
    
    def record_result(
        self,
        pnl: float,
        result: str,  # "WIN" or "LOSS"
        size: float,
        ticker: str = ""
    ) -> None:
        """
        Update state after trade execution.
        
        Args:
            pnl: Profit/loss from trade
            result: "WIN" or "LOSS"
            size: Trade size
            ticker: Market ticker (optional)
        """
        
        # Update P&L
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        self.trades_today += 1
        
        # Track loss streak
        if result == "LOSS":
            if self.last_trade_result == "LOSS":
                self.loss_streak += 1
            else:
                self.loss_streak = 1
            
            # Check if cooldown should trigger
            if self.loss_streak >= LOSS_STREAK_TRIGGER:
                self._trigger_cooldown()
        
        elif result == "WIN":
            self.loss_streak = 0
        
        self.last_trade_result = result
        
        # Save state
        self._save_state()
        
# Log if approaching loss limit (but haven't hit it yet)
        # Warning threshold: 60% of absolute limit
        # Example: if limit is -50, warn at -30
        warning_threshold = DAILY_LOSS_LIMIT * 0.6
        
        # Only warn if:
        # 1. We've reached the warning zone (daily_pnl <= warning_threshold)
        # 2. We haven't breached the limit yet (daily_pnl > DAILY_LOSS_LIMIT)
        if self.daily_pnl <= warning_threshold and self.daily_pnl > DAILY_LOSS_LIMIT:
            remaining = abs(DAILY_LOSS_LIMIT - self.daily_pnl)
            self.logger.log_event(
                event_type="APPROACHING_LOSS_LIMIT",
                details={
                    "daily_pnl": self.daily_pnl,
                    "loss_limit": DAILY_LOSS_LIMIT,
                    "remaining": remaining,
                    "warning_threshold": warning_threshold,
                    "message": f"⚠️  ${remaining:.2f} from daily loss limit"
                },
                severity="WARNING"
            )    
    
    def _trigger_cooldown(self) -> None:
        """Trigger cooldown period after loss streak."""
        self.cooldown_until = datetime.now(timezone.utc) + timedelta(
            minutes=COOLDOWN_MINUTES
        )
        self.cooldown_reason = f"{self.loss_streak} losses in a row"
        
        self.logger.log_cooldown_triggered(
            loss_streak=self.loss_streak,
            trigger_threshold=LOSS_STREAK_TRIGGER,
            cooldown_minutes=COOLDOWN_MINUTES
        )
        
        print(f"\n⚠️  COOLDOWN TRIGGERED")
        print(f"   Reason: {self.cooldown_reason}")
        print(f"   Duration: {COOLDOWN_MINUTES} minutes")
        print(f"   Until: {self.cooldown_until.strftime('%H:%M:%S')}\n")
        
        self._save_state()
    
    
    # ───────────────────────────────────────────────────────────
    # POSITION TRACKING
    # ───────────────────────────────────────────────────────────
    
    def add_position(self, size: float) -> None:
        """Record new open position."""
        self.open_positions += 1
        self.total_exposure += size
        self._save_state()
    
    
    def close_position(self, size: float) -> None:
        """Record position close."""
        self.open_positions = max(0, self.open_positions - 1)
        self.total_exposure = max(0, self.total_exposure - size)
        self._save_state()
    
    
    # ───────────────────────────────────────────────────────────
    # STATUS & REPORTING
    # ───────────────────────────────────────────────────────────
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get current risk manager status.
        
        Returns:
            Dict with all current state
        """
        now = datetime.now(timezone.utc)
        
        # Calculate limits remaining
        loss_limit_remaining = abs(DAILY_LOSS_LIMIT - self.daily_pnl)
        loss_limit_pct = (self.daily_pnl / DAILY_LOSS_LIMIT) if DAILY_LOSS_LIMIT != 0 else 0
        
        # Cooldown status
        cooldown_active = False
        cooldown_remaining = 0
        if self.cooldown_until and now < self.cooldown_until:
            cooldown_active = True
            cooldown_remaining = (self.cooldown_until - now).total_seconds() / 60
        
        # Trading allowed?
        trading_allowed = (
            not self.kill_switch_active
            and not self.trading_paused
            and self.daily_pnl > DAILY_LOSS_LIMIT
            and not cooldown_active
        )
        
        return {
            # P&L
            "daily_pnl": round(self.daily_pnl, 2),
            "weekly_pnl": round(self.weekly_pnl, 2),
            "bankroll": round(self.bankroll, 2),
            
            # Limits
            "daily_loss_limit": DAILY_LOSS_LIMIT,
            "loss_limit_remaining": round(loss_limit_remaining, 2),
            "loss_limit_pct_used": round(loss_limit_pct, 2),
            
            # Positions
            "open_positions": self.open_positions,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "total_exposure": round(self.total_exposure, 2),
            
            # Trades
            "trades_today": self.trades_today,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            
            # Loss streak
            "loss_streak": self.loss_streak,
            "loss_streak_trigger": LOSS_STREAK_TRIGGER,
            
            # Cooldown
            "cooldown_active": cooldown_active,
            "cooldown_remaining_minutes": round(cooldown_remaining, 1) if cooldown_active else 0,
            "cooldown_reason": self.cooldown_reason if cooldown_active else "",
            
            # Kill switch
            "kill_switch_active": self.kill_switch_active,
            "trading_paused": self.trading_paused,
            "trading_allowed": trading_allowed,
            
            # Dates
            "last_reset_date": self.last_reset_date.isoformat(),
            "week_start_date": self.week_start_date.isoformat(),
        }
    
    
    def is_trading_allowed(self) -> bool:
        """Quick check if trading is globally allowed."""
        self._check_daily_reset()
        
        return (
            not self.kill_switch_active
            and not self.trading_paused
            and self.daily_pnl > DAILY_LOSS_LIMIT
            and (not self.cooldown_until or datetime.now(timezone.utc) >= self.cooldown_until)
        )
    
    
    def get_limits_summary(self) -> str:
        """Get human-readable limits summary."""
        status = self.get_status()
        
        lines = []
        lines.append("="*60)
        lines.append("RISK MANAGER STATUS")
        lines.append("="*60)
        
        # P&L
        pnl_status = "✅" if status["daily_pnl"] >= 0 else "🔴"
        lines.append(f"\nDaily P&L: {pnl_status} ${status['daily_pnl']:.2f}")
        lines.append(f"Loss limit: ${status['daily_loss_limit']:.2f}")
        lines.append(f"Remaining: ${status['loss_limit_remaining']:.2f}")
        
        # Positions
        lines.append(f"\nOpen positions: {status['open_positions']} / {status['max_open_positions']}")
        lines.append(f"Exposure: ${status['total_exposure']:.2f}")
        
        # Trades
        lines.append(f"\nTrades today: {status['trades_today']} / {status['max_trades_per_day']}")
        
        # Loss streak
        if status['loss_streak'] > 0:
            lines.append(f"\n⚠️  Loss streak: {status['loss_streak']} (trigger at {status['loss_streak_trigger']})")
        
        # Cooldown
        if status['cooldown_active']:
            lines.append(f"\n🕐 COOLDOWN ACTIVE")
            lines.append(f"   Time remaining: {status['cooldown_remaining_minutes']:.0f} minutes")
            lines.append(f"   Reason: {status['cooldown_reason']}")
        
        # Kill switch
        if status['kill_switch_active']:
            lines.append(f"\n🚨 KILL SWITCH ACTIVE - ALL TRADING STOPPED")
        elif status['trading_paused']:
            lines.append(f"\n⏸️  TRADING PAUSED")
        elif status['trading_allowed']:
            lines.append(f"\n✅ Trading allowed")
        else:
            lines.append(f"\n🔴 Trading blocked")
        
        lines.append("="*60)
        
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*60)
    print("RISK MANAGER TEST")
    print("="*60)
    
    # Initialize
    rm = RiskManager(bankroll=500.0)
    
    print("\n[TEST 1] Initial status")
    print(rm.get_limits_summary())
    
    print("\n[TEST 2] Check valid trade")
    approved, reason = rm.check_trade(
        ticker="KXBTCUSD-TEST",
        action="BET_YES",
        size=25.0,
        confidence=0.68,
        edge=0.035,
        strategy="SIGNAL"
    )
    print(f"   Trade check: {'✅ APPROVED' if approved else f'❌ BLOCKED: {reason}'}")
    
    print("\n[TEST 3] Check oversized trade")
    approved, reason = rm.check_trade(
        ticker="KXBTCUSD-TEST",
        action="BET_YES",
        size=75.0,  # Exceeds $50 limit
        confidence=0.70,
        edge=0.040,
        strategy="SIGNAL"
    )
    print(f"   Trade check: {'✅ APPROVED' if approved else f'❌ BLOCKED: {reason}'}")
    
    print("\n[TEST 4] Simulate loss streak")
    for i in range(3):
        rm.record_result(pnl=-10.0, result="LOSS", size=25.0)
        print(f"   Loss {i+1}: Loss streak = {rm.loss_streak}")
    
    print("\n[TEST 5] Check trade during cooldown")
    approved, reason = rm.check_trade(
        ticker="KXBTCUSD-TEST",
        action="BET_YES",
        size=25.0,
        confidence=0.68,
        edge=0.035,
        strategy="SIGNAL"
    )
    print(f"   Trade check: {'✅ APPROVED' if approved else f'❌ BLOCKED: {reason}'}")
    
    print("\n[TEST 6] Activate kill switch")
    rm.activate_kill_switch(reason="Testing emergency stop")
    
    print("\n[TEST 7] Check trade with kill switch")
    approved, reason = rm.check_trade(
        ticker="KXBTCUSD-TEST",
        action="ARB",
        size=10.0,
        confidence=0.99,
        edge=0.020,
        strategy="ARB"
    )
    print(f"   Trade check: {'✅ APPROVED' if approved else f'❌ BLOCKED: {reason}'}")
    
    print("\n[TEST 8] Deactivate kill switch")
    rm.deactivate_kill_switch()
    
    print("\n[TEST 9] Final status")
    print(rm.get_limits_summary())
    
    print("\n" + "="*60)
    print("✅ All tests complete")
    print("="*60 + "\n")
