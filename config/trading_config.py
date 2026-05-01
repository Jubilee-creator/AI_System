"""
config/trading_config.py
------------------------
Centralized trading configuration and risk parameters

PHASE: 4, Step 1
PURPOSE: Single source of truth for ALL trading limits
SCOPE: Paper trading + future live trading

All limits enforced by risk/risk_manager.py
Modify these values to adjust safety parameters
"""

from datetime import time as dt_time
from typing import Dict, Any


# ═══════════════════════════════════════════════════════════════
# ACCOUNT & BANKROLL
# ═══════════════════════════════════════════════════════════════

# Starting bankroll (paper trading)
INITIAL_BANKROLL = 500.0

# Mode: "PAPER" or "LIVE"
TRADING_MODE = "PAPER"

# Venue
VENUE = "kalshi"


# ═══════════════════════════════════════════════════════════════
# POSITION SIZING LIMITS
# ═══════════════════════════════════════════════════════════════

# Maximum dollar amount per single trade
MAX_POSITION_SIZE = 50.0

# Maximum percentage of bankroll per trade (10%)
MAX_BANKROLL_PCT = 0.10

# Maximum number of concurrent open positions
MAX_OPEN_POSITIONS = 5

# Kelly fraction (0.25 = quarter Kelly, conservative)
KELLY_FRACTION = 0.25


# ═══════════════════════════════════════════════════════════════
# DAILY LOSS LIMITS (CIRCUIT BREAKER)
# ═══════════════════════════════════════════════════════════════

# Maximum loss allowed per day before FULL STOP
# Example: -50.0 means if down $50 today, STOP ALL TRADING
DAILY_LOSS_LIMIT = -50.0

# Maximum loss allowed per week
WEEKLY_LOSS_LIMIT = -150.0

# Maximum number of trades per day
MAX_TRADES_PER_DAY = 20


# ═══════════════════════════════════════════════════════════════
# LOSS STREAK & COOLDOWN
# ═══════════════════════════════════════════════════════════════

# Number of consecutive losses before triggering cooldown
LOSS_STREAK_TRIGGER = 3

# Cooldown duration in minutes after loss streak
COOLDOWN_MINUTES = 30

# Cooldown duration after daily loss limit hit (in hours)
LOSS_LIMIT_COOLDOWN_HOURS = 24


# ═══════════════════════════════════════════════════════════════
# EDGE & CONFIDENCE REQUIREMENTS
# ═══════════════════════════════════════════════════════════════

# Minimum edge (after fees) required to take a trade
# 0.03 = 3¢ edge minimum
MIN_EDGE = 0.03

# Minimum model confidence to take a trade
# 0.65 = 65% probability minimum
MIN_CONFIDENCE = 0.65

# Minimum edge for ARB trades (can be lower since guaranteed)
MIN_ARB_EDGE = 0.005  # 0.5¢ minimum for arb


# ═══════════════════════════════════════════════════════════════
# TRADING HOURS (US Eastern Time)
# ═══════════════════════════════════════════════════════════════

# Market hours when most liquidity is available
TRADING_HOURS = {
    "enabled": False,  # Set to True to enforce time restrictions
    "start": dt_time(9, 0),   # 9:00 AM ET
    "end": dt_time(16, 0),    # 4:00 PM ET
    "timezone": "US/Eastern"
}

# Allow after-hours trading for specific events (sports, crypto)
ALLOW_AFTER_HOURS_CRYPTO = True
ALLOW_AFTER_HOURS_SPORTS = True


# ═══════════════════════════════════════════════════════════════
# MARKET FILTERS
# ═══════════════════════════════════════════════════════════════

# Minimum market volume to consider (in $)
MIN_VOLUME = 1000
MIN_MARKET_VOLUME = MIN_VOLUME

# Maximum spread (bid-ask) to accept
# 0.05 = 5¢ spread maximum
MAX_SPREAD = 0.05

# Minimum time until expiry (in hours)
MIN_TIME_TO_EXPIRY = 0.5  # 30 minutes minimum

# Maximum time until market close/expiry for trade consideration.
# Validation is focused on short-duration learning trades; long-dated
# contracts such as Jan 2027 markets should not be opened and force-closed
# after two hours as if they were short-term signals.
MAX_TRADE_EXPIRY_HOURS = 24.0


# ═══════════════════════════════════════════════════════════════
# SLIPPAGE & FEES
# ═══════════════════════════════════════════════════════════════

# Expected slippage per trade (conservative estimate)
SLIPPAGE_ESTIMATE = 0.005  # 0.5¢

# Kalshi trading fee (per contract)
KALSHI_FEE = 0.01  # 1¢ per contract

# Total trading cost (fee + slippage)
TOTAL_TRADING_COST = KALSHI_FEE + SLIPPAGE_ESTIMATE


# ═══════════════════════════════════════════════════════════════
# KILL SWITCH & EMERGENCY CONTROLS
# ═══════════════════════════════════════════════════════════════

# Kill switch status (manual emergency stop)
KILL_SWITCH_ACTIVE = False

# Pause all trading (temporary stop without full kill switch)
TRADING_PAUSED = False

# Reason for pause (for logging)
PAUSE_REASON = ""


# ═══════════════════════════════════════════════════════════════
# RISK ALERTS & NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

# Alert when daily loss reaches this threshold
ALERT_AT_LOSS_PCT = 0.60  # Alert at 60% of daily loss limit

# Alert when win rate drops below this
ALERT_AT_WIN_RATE = 0.45  # Alert if win rate < 45%

# Email/SMS alerts (not implemented yet - Phase 5)
ENABLE_EMAIL_ALERTS = False
ENABLE_SMS_ALERTS = False


# ═══════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════════════

# Auto-close positions when profit target hit
AUTO_CLOSE_AT_PROFIT_PCT = None  # Set to e.g. 0.20 for 20% profit close

# Auto-close positions when loss limit hit
AUTO_CLOSE_AT_LOSS_PCT = None  # Set to e.g. 0.15 for 15% loss close

# Maximum position hold time (in hours)
MAX_POSITION_HOLD_TIME = None  # Set to e.g. 48 for 2-day max hold


# ═══════════════════════════════════════════════════════════════
# STRATEGY-SPECIFIC LIMITS
# ═══════════════════════════════════════════════════════════════

STRATEGY_LIMITS = {
    "ARB": {
        "max_position_size": 100.0,  # ARB can be larger (low risk)
        "max_positions": 10,
        "min_edge": MIN_ARB_EDGE,
        "enabled": True
    },
    "SIGNAL": {
        "max_position_size": 50.0,
        "max_positions": 5,
        "min_edge": MIN_EDGE,
        "min_confidence": MIN_CONFIDENCE,
        "enabled": True
    },
    "TREND": {
        "max_position_size": 30.0,
        "max_positions": 3,
        "min_edge": 0.05,  # Higher edge required
        "min_confidence": 0.70,  # Higher confidence required
        "enabled": True
    }
}


# ═══════════════════════════════════════════════════════════════
# PAPER TRADING SPECIFIC
# ═══════════════════════════════════════════════════════════════

# Number of paper trades required to prove edge before going live
PAPER_TRADES_REQUIRED = 100

# Minimum win rate required to go live
MIN_WIN_RATE_TO_GO_LIVE = 0.52

# Minimum average edge required to go live
MIN_AVG_EDGE_TO_GO_LIVE = 0.02

# Minimum CLV (closing line value) to go live
MIN_CLV_TO_GO_LIVE = 0.0

# Validation mode: cap individual trade size to collect more trades
# before daily exposure limit is consumed.  All other risk controls
# (daily loss limit, exposure check, confidence, edge) remain unchanged.
PAPER_VALIDATION_MODE = True
PAPER_VALIDATION_MAX_BET_SIZE = 10.00  # $ per trade while in validation mode

# Phase 1-3 research mode: Kelly is calculated and logged for audit only.
# Actual paper entries are forced to the minimum learning bet until edge,
# calibration, and normal council-approved performance are proven.
GLOBAL_FORCED_LEARNING_MODE = True

# Research data-collection mode: when the Council blocks a signal, paper mode
# can still log a $5 learning trade with data_collection_override=True.
# These rows are explicitly NOT normal council-approved proof.
DATA_COLLECTION_MODE = True

# Maximum concurrent open positions per ticker (1 = no duplicate exposure)
MAX_POSITIONS_PER_TICKER = 1


# ═══════════════════════════════════════════════════════════════
# EDGE PROFILE TRUST GATES
# ═══════════════════════════════════════════════════════════════

# Edge profiles are historical evidence, not proof.  These gates prevent tiny,
# stale, or data-collection-only profiles from being treated as reliable.
EDGE_PROFILE_MAX_AGE_HOURS = 48
EDGE_PROFILE_MIN_CLEAN_SETTLED = 30
EDGE_PROFILE_MIN_MODERN_FULL = 30
EDGE_PROFILE_MIN_NORMAL_MODERN = 10


# ═══════════════════════════════════════════════════════════════
# BOOTSTRAP PROVISIONAL MODE
# ═══════════════════════════════════════════════════════════════

# When the system is in a bootstrap deadlock (normal_modern == 0, edge profile
# untrusted), the Critic cannot normally approve any signal.  Bootstrap
# provisional mode allows the Critic to return a PROVISIONAL decision for
# signals that meet a higher quality bar.  These trades are logged with
# bootstrap_provisional=True and are explicitly excluded from normal proof
# counts, proof gate advancement, and edge profile trust evaluation.
#
# bootstrap_provisional trades:
#   - are allowed only when the system has zero normal-approved modern trades
#   - execute at MIN_LEARNING_BET ($5) — same as data_collection_override
#   - do NOT count as council-approved normal proof
#   - do NOT advance proof gates
#   - do NOT make edge_profile trusted
#   - do NOT unlock scaling or real money
BOOTSTRAP_PROVISIONAL_MODE = True
BOOTSTRAP_MIN_EDGE = 0.05          # original edge floor for provisional candidates
BOOTSTRAP_MIN_CONFIDENCE = 0.65    # confidence floor for provisional candidates


# ═══════════════════════════════════════════════════════════════
# EDGE DANGER GUARD
# ═══════════════════════════════════════════════════════════════

# M-45 showed the current high-edge bucket is not reliable evidence:
# edge >= 0.08 had negative realized ROI/CLV and behaved as an inverted signal.
# This paper-only guard prevents the system from treating high predicted edge as
# authority while calibration is unproven.  It is deliberately narrow and can be
# removed only after modern full-metadata calibration proves edge is monotonic.
EDGE_DANGER_GUARD_ENABLED = True
EDGE_DANGER_BLOCK_HIGH_EDGE = True
EDGE_DANGER_HIGH_EDGE_MIN = 0.08
EDGE_DANGER_REQUIRE_PAPER_ONLY = True
EDGE_DANGER_REASON = "M-45 high-edge bucket negative ROI/CLV; edge not trusted"


# ═══════════════════════════════════════════════════════════════
# LOGGING & PERSISTENCE
# ═══════════════════════════════════════════════════════════════

# Risk events log path
RISK_LOG_PATH = "logs/risk_events.jsonl"

# Trade log path
TRADE_LOG_PATH = "logs/paper_trades.jsonl"

# State persistence (save daily stats, cooldowns, etc.)
STATE_FILE_PATH = "data/risk_state.json"

# Auto-save state every N trades
AUTO_SAVE_INTERVAL = 5


# ═══════════════════════════════════════════════════════════════
# VALIDATION & HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def validate_config() -> tuple[bool, list[str]]:
    """
    Validate configuration for logical consistency.
    
    Returns:
        (is_valid, error_messages)
    """
    errors = []
    
    # Daily loss limit must be negative
    if DAILY_LOSS_LIMIT > 0:
        errors.append("DAILY_LOSS_LIMIT must be negative (e.g., -50.0)")
    
    # Max position size must be positive
    if MAX_POSITION_SIZE <= 0:
        errors.append("MAX_POSITION_SIZE must be positive")
    
    # Max bankroll % must be between 0 and 1
    if not (0 < MAX_BANKROLL_PCT <= 1):
        errors.append("MAX_BANKROLL_PCT must be between 0 and 1")
    
    # Min confidence must be between 0.5 and 1
    if not (0.5 <= MIN_CONFIDENCE <= 1.0):
        errors.append("MIN_CONFIDENCE must be between 0.5 and 1.0")
    
    # Kelly fraction should be conservative (< 0.5)
    if KELLY_FRACTION > 0.5:
        errors.append("KELLY_FRACTION > 0.5 is aggressive (recommended: 0.25)")
    
    # Loss streak trigger must be positive
    if LOSS_STREAK_TRIGGER <= 0:
        errors.append("LOSS_STREAK_TRIGGER must be positive")
    
    # Cooldown minutes must be positive
    if COOLDOWN_MINUTES <= 0:
        errors.append("COOLDOWN_MINUTES must be positive")
    
    is_valid = len(errors) == 0
    return is_valid, errors


def get_config_summary() -> Dict[str, Any]:
    """
    Get summary of current configuration.
    
    Returns:
        Dict with key config values
    """
    return {
        "mode": TRADING_MODE,
        "bankroll": INITIAL_BANKROLL,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "max_position_size": MAX_POSITION_SIZE,
        "max_bankroll_pct": MAX_BANKROLL_PCT,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "loss_streak_trigger": LOSS_STREAK_TRIGGER,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "min_edge": MIN_EDGE,
        "min_confidence": MIN_CONFIDENCE,
        "kelly_fraction": KELLY_FRACTION,
        "global_forced_learning_mode": GLOBAL_FORCED_LEARNING_MODE,
        "data_collection_mode": DATA_COLLECTION_MODE,
        "kill_switch_active": KILL_SWITCH_ACTIVE,
        "trading_paused": TRADING_PAUSED,
    }


def get_safety_limits() -> Dict[str, float]:
    """
    Get all safety limits in one dict.
    
    Returns:
        Dict with all limit values
    """
    return {
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "weekly_loss_limit": WEEKLY_LOSS_LIMIT,
        "max_position_size": MAX_POSITION_SIZE,
        "max_bankroll_pct": MAX_BANKROLL_PCT,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "loss_streak_trigger": LOSS_STREAK_TRIGGER,
        "cooldown_minutes": COOLDOWN_MINUTES,
    }


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*60)
    print("TRADING CONFIG VALIDATION")
    print("="*60)
    
    # Validate config
    is_valid, errors = validate_config()
    
    if is_valid:
        print("\n✅ Configuration is VALID")
    else:
        print("\n❌ Configuration has ERRORS:")
        for error in errors:
            print(f"   - {error}")
    
    # Display config summary
    print("\n" + "-"*60)
    print("CONFIG SUMMARY")
    print("-"*60)
    
    summary = get_config_summary()
    for key, value in summary.items():
        print(f"  {key:.<25} {value}")
    
    # Display safety limits
    print("\n" + "-"*60)
    print("SAFETY LIMITS")
    print("-"*60)
    
    limits = get_safety_limits()
    for key, value in limits.items():
        print(f"  {key:.<25} {value}")
    
    # Display strategy limits
    print("\n" + "-"*60)
    print("STRATEGY LIMITS")
    print("-"*60)
    
    for strategy, config in STRATEGY_LIMITS.items():
        print(f"\n  {strategy}:")
        for key, value in config.items():
            print(f"    {key:.<23} {value}")
    
    print("\n" + "="*60)
    print("✅ Config loaded successfully")
    print("="*60 + "\n")
