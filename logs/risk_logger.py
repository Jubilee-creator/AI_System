"""
logs/risk_logger.py
-------------------
Risk event logger for safety system

PHASE: 4, Step 2
PURPOSE: Log all risk management events (violations, blocks, kills)
OUTPUT: logs/risk_events.jsonl

Event types:
- LOSS_LIMIT_HIT: Daily/weekly loss limit reached
- POSITION_SIZE_BLOCKED: Trade exceeded max position size
- COOLDOWN_TRIGGERED: Loss streak triggered cooldown
- COOLDOWN_ACTIVE: Trade blocked during cooldown period
- KILL_SWITCH_ACTIVATED: Manual emergency stop
- KILL_SWITCH_DEACTIVATED: Trading resumed after kill switch
- TRADE_BLOCKED: General trade rejection
- MAX_POSITIONS_REACHED: Too many open positions
- OUTSIDE_TRADING_HOURS: Trade attempted outside allowed hours
- INSUFFICIENT_EDGE: Trade below minimum edge threshold
- INSUFFICIENT_CONFIDENCE: Trade below minimum confidence threshold
- CONFIG_CHANGED: Configuration parameter updated
- DAILY_RESET: Daily counters reset at midnight
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

# Default log file path
DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "risk_events.jsonl"
)


# ─────────────────────────────────────────────────────────────
# RISK LOGGER CLASS
# ─────────────────────────────────────────────────────────────

class RiskLogger:
    """
    Logger for risk management events.
    
    All safety system events are logged here for audit trail.
    Separate from trade logs to track violations and blocks.
    """
    
    def __init__(self, log_path: Optional[str] = None):
        """
        Initialize risk logger.
        
        Args:
            log_path: Path to risk events log file (JSONL format)
        """
        self.log_path = log_path or DEFAULT_LOG_PATH
        
        # Ensure logs directory exists
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        
        # Create file if doesn't exist
        if not os.path.exists(self.log_path):
            Path(self.log_path).touch()
            print(f"[RISK_LOGGER] Created: {self.log_path}")
        else:
            print(f"[RISK_LOGGER] Initialized: {self.log_path}")
    
    
    def _write_event(self, event: Dict[str, Any]) -> None:
        """
        Write event to log file.
        
        Args:
            event: Event dict to log
        """
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            print(f"[RISK_LOGGER ERROR] Failed to write event: {e}")
    
    
    def log_event(
        self,
        event_type: str,
        details: Dict[str, Any],
        severity: str = "INFO"
    ) -> None:
        """
        Log a risk event.
        
        Args:
            event_type: Type of risk event (e.g., "LOSS_LIMIT_HIT")
            details: Event details dict
            severity: "INFO", "WARNING", "CRITICAL"
        """
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "severity": severity,
            "details": details
        }
        
        self._write_event(event)
        
        # Print to console for visibility
        severity_prefix = {
            "INFO": "ℹ️ ",
            "WARNING": "⚠️ ",
            "CRITICAL": "🚨"
        }.get(severity, "")
        
        print(f"[RISK] {severity_prefix}{event_type}: {details}")
    
    
    # ─────────────────────────────────────────────────────────
    # SPECIFIC EVENT LOGGERS
    # ─────────────────────────────────────────────────────────
    
    def log_loss_limit_hit(
        self,
        daily_pnl: float,
        loss_limit: float,
        trades_today: int
    ) -> None:
        """Log daily loss limit reached."""
        self.log_event(
            event_type="LOSS_LIMIT_HIT",
            details={
                "daily_pnl": daily_pnl,
                "loss_limit": loss_limit,
                "trades_today": trades_today,
                "message": f"Daily loss limit hit: ${daily_pnl:.2f} / ${loss_limit:.2f}"
            },
            severity="CRITICAL"
        )
    
    
    def log_position_size_blocked(
        self,
        ticker: str,
        requested_size: float,
        max_size: float,
        reason: str = ""
    ) -> None:
        """Log trade blocked due to position size."""
        self.log_event(
            event_type="POSITION_SIZE_BLOCKED",
            details={
                "ticker": ticker,
                "requested_size": requested_size,
                "max_size": max_size,
                "reason": reason or f"Trade size ${requested_size:.2f} exceeds max ${max_size:.2f}"
            },
            severity="WARNING"
        )
    
    
    def log_cooldown_triggered(
        self,
        loss_streak: int,
        trigger_threshold: int,
        cooldown_minutes: int
    ) -> None:
        """Log cooldown triggered by loss streak."""
        self.log_event(
            event_type="COOLDOWN_TRIGGERED",
            details={
                "loss_streak": loss_streak,
                "trigger_threshold": trigger_threshold,
                "cooldown_minutes": cooldown_minutes,
                "message": f"{loss_streak} losses in a row → {cooldown_minutes}min cooldown"
            },
            severity="WARNING"
        )
    
    
    def log_cooldown_active(
        self,
        ticker: str,
        time_remaining_minutes: float
    ) -> None:
        """Log trade blocked during active cooldown."""
        self.log_event(
            event_type="COOLDOWN_ACTIVE",
            details={
                "ticker": ticker,
                "time_remaining_minutes": round(time_remaining_minutes, 1),
                "message": f"Trade blocked - cooldown active ({time_remaining_minutes:.0f}min remaining)"
            },
            severity="INFO"
        )
    
    
    def log_kill_switch_activated(
        self,
        reason: str = "",
        activated_by: str = "manual"
    ) -> None:
        """Log kill switch activation."""
        self.log_event(
            event_type="KILL_SWITCH_ACTIVATED",
            details={
                "reason": reason or "Manual emergency stop",
                "activated_by": activated_by,
                "message": "🚨 KILL SWITCH ACTIVATED - ALL TRADING STOPPED"
            },
            severity="CRITICAL"
        )
    
    
    def log_kill_switch_deactivated(
        self,
        deactivated_by: str = "manual"
    ) -> None:
        """Log kill switch deactivation."""
        self.log_event(
            event_type="KILL_SWITCH_DEACTIVATED",
            details={
                "deactivated_by": deactivated_by,
                "message": "Kill switch deactivated - trading resumed"
            },
            severity="INFO"
        )
    
    
    def log_trade_blocked(
        self,
        ticker: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log general trade block."""
        event_details = {
            "ticker": ticker,
            "reason": reason
        }
        if details:
            event_details.update(details)
        
        self.log_event(
            event_type="TRADE_BLOCKED",
            details=event_details,
            severity="INFO"
        )
    
    
    def log_max_positions_reached(
        self,
        ticker: str,
        current_positions: int,
        max_positions: int
    ) -> None:
        """Log trade blocked due to max positions."""
        self.log_event(
            event_type="MAX_POSITIONS_REACHED",
            details={
                "ticker": ticker,
                "current_positions": current_positions,
                "max_positions": max_positions,
                "message": f"Max positions reached: {current_positions}/{max_positions}"
            },
            severity="WARNING"
        )
    
    
    def log_outside_trading_hours(
        self,
        ticker: str,
        current_time: str
    ) -> None:
        """Log trade blocked outside trading hours."""
        self.log_event(
            event_type="OUTSIDE_TRADING_HOURS",
            details={
                "ticker": ticker,
                "current_time": current_time,
                "message": "Trade blocked - outside trading hours"
            },
            severity="INFO"
        )
    
    
    def log_insufficient_edge(
        self,
        ticker: str,
        edge: float,
        min_edge: float
    ) -> None:
        """Log trade blocked due to insufficient edge."""
        self.log_event(
            event_type="INSUFFICIENT_EDGE",
            details={
                "ticker": ticker,
                "edge": edge,
                "min_edge": min_edge,
                "message": f"Edge {edge:.4f} below minimum {min_edge:.4f}"
            },
            severity="INFO"
        )
    
    
    def log_insufficient_confidence(
        self,
        ticker: str,
        confidence: float,
        min_confidence: float
    ) -> None:
        """Log trade blocked due to insufficient confidence."""
        self.log_event(
            event_type="INSUFFICIENT_CONFIDENCE",
            details={
                "ticker": ticker,
                "confidence": confidence,
                "min_confidence": min_confidence,
                "message": f"Confidence {confidence:.2%} below minimum {min_confidence:.2%}"
            },
            severity="INFO"
        )
    
    
    def log_config_changed(
        self,
        parameter: str,
        old_value: Any,
        new_value: Any,
        changed_by: str = "manual"
    ) -> None:
        """Log configuration parameter change."""
        self.log_event(
            event_type="CONFIG_CHANGED",
            details={
                "parameter": parameter,
                "old_value": old_value,
                "new_value": new_value,
                "changed_by": changed_by,
                "message": f"Config: {parameter} changed from {old_value} to {new_value}"
            },
            severity="WARNING"
        )
    
    
    def log_daily_reset(
        self,
        previous_pnl: float,
        trades_yesterday: int
    ) -> None:
        """Log daily counter reset."""
        self.log_event(
            event_type="DAILY_RESET",
            details={
                "previous_pnl": previous_pnl,
                "trades_yesterday": trades_yesterday,
                "message": f"Daily reset - Yesterday: ${previous_pnl:.2f} P&L, {trades_yesterday} trades"
            },
            severity="INFO"
        )
    
    
    # ─────────────────────────────────────────────────────────
    # QUERY & RETRIEVAL
    # ─────────────────────────────────────────────────────────
    
    def get_recent_events(
        self,
        n: int = 10,
        event_type: Optional[str] = None,
        severity: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get recent risk events.
        
        Args:
            n: Number of events to return
            event_type: Filter by event type (optional)
            severity: Filter by severity (optional)
        
        Returns:
            List of event dicts (most recent first)
        """
        if not os.path.exists(self.log_path):
            return []
        
        events = []
        
        try:
            with open(self.log_path, "r") as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        
                        # Apply filters
                        if event_type and event.get("event_type") != event_type:
                            continue
                        if severity and event.get("severity") != severity:
                            continue
                        
                        events.append(event)
                    
                    except json.JSONDecodeError:
                        continue
        
        except Exception as e:
            print(f"[RISK_LOGGER ERROR] Failed to read events: {e}")
            return []
        
        # Return most recent first
        return events[-n:][::-1]
    
    
    def get_events_since(
        self,
        since: datetime,
        event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get events since a specific timestamp.
        
        Args:
            since: Get events after this timestamp
            event_type: Filter by event type (optional)
        
        Returns:
            List of event dicts
        """
        if not os.path.exists(self.log_path):
            return []
        
        events = []
        
        try:
            with open(self.log_path, "r") as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        
                        # Parse timestamp
                        event_time = datetime.fromisoformat(event["timestamp"])
                        
                        # Filter by time
                        if event_time <= since:
                            continue
                        
                        # Filter by type if specified
                        if event_type and event.get("event_type") != event_type:
                            continue
                        
                        events.append(event)
                    
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        
        except Exception as e:
            print(f"[RISK_LOGGER ERROR] Failed to read events: {e}")
            return []
        
        return events
    
    
    def count_events_today(self, event_type: Optional[str] = None) -> int:
        """
        Count events that occurred today.
        
        Args:
            event_type: Filter by event type (optional)
        
        Returns:
            Number of events
        """
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        
        events = self.get_events_since(today_start, event_type=event_type)
        return len(events)
    
    
    def get_event_summary(self) -> Dict[str, int]:
        """
        Get count of each event type in log.
        
        Returns:
            Dict mapping event_type to count
        """
        if not os.path.exists(self.log_path):
            return {}
        
        counts = {}
        
        try:
            with open(self.log_path, "r") as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        event_type = event.get("event_type", "UNKNOWN")
                        counts[event_type] = counts.get(event_type, 0) + 1
                    except json.JSONDecodeError:
                        continue
        
        except Exception as e:
            print(f"[RISK_LOGGER ERROR] Failed to read events: {e}")
            return {}
        
        return counts


# ─────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ─────────────────────────────────────────────────────────────

# Global risk logger instance
_logger = None


def get_logger(log_path: Optional[str] = None) -> RiskLogger:
    """
    Get global risk logger instance.
    
    Args:
        log_path: Path to log file (uses default if not provided)
    
    Returns:
        RiskLogger instance
    """
    global _logger
    
    if _logger is None:
        _logger = RiskLogger(log_path=log_path)
    
    return _logger


# Convenience functions using global logger
def log_event(event_type: str, details: Dict[str, Any], severity: str = "INFO") -> None:
    """Log risk event using global logger."""
    get_logger().log_event(event_type, details, severity)


def log_loss_limit_hit(daily_pnl: float, loss_limit: float, trades_today: int) -> None:
    """Log loss limit hit using global logger."""
    get_logger().log_loss_limit_hit(daily_pnl, loss_limit, trades_today)


def log_position_size_blocked(ticker: str, requested_size: float, max_size: float, reason: str = "") -> None:
    """Log position size block using global logger."""
    get_logger().log_position_size_blocked(ticker, requested_size, max_size, reason)


def log_cooldown_triggered(loss_streak: int, trigger_threshold: int, cooldown_minutes: int) -> None:
    """Log cooldown trigger using global logger."""
    get_logger().log_cooldown_triggered(loss_streak, trigger_threshold, cooldown_minutes)


def log_trade_blocked(ticker: str, reason: str, details: Optional[Dict] = None) -> None:
    """Log trade block using global logger."""
    get_logger().log_trade_blocked(ticker, reason, details)


def log_kill_switch_activated(reason: str = "", activated_by: str = "manual") -> None:
    """Log kill switch activation using global logger."""
    get_logger().log_kill_switch_activated(reason, activated_by)


# ─────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*60)
    print("RISK LOGGER TEST")
    print("="*60)
    
    # Initialize logger
    logger = RiskLogger()
    
    print("\n[TEST 1] Logging sample events...")
    
    # Test each event type
    logger.log_loss_limit_hit(
        daily_pnl=-52.50,
        loss_limit=-50.0,
        trades_today=15
    )
    
    logger.log_position_size_blocked(
        ticker="KXBTCUSD-24APR14",
        requested_size=75.0,
        max_size=50.0
    )
    
    logger.log_cooldown_triggered(
        loss_streak=3,
        trigger_threshold=3,
        cooldown_minutes=30
    )
    
    logger.log_trade_blocked(
        ticker="KXETHD-24APR15",
        reason="Insufficient edge",
        details={"edge": 0.015, "min_edge": 0.03}
    )
    
    logger.log_kill_switch_activated(
        reason="Testing emergency stop"
    )
    
    logger.log_kill_switch_deactivated()
    
    print("\n[TEST 2] Retrieving recent events...")
    recent = logger.get_recent_events(n=5)
    print(f"   Found {len(recent)} recent events")
    
    print("\n[TEST 3] Event summary...")
    summary = logger.get_event_summary()
    for event_type, count in summary.items():
        print(f"   {event_type}: {count}")
    
    print("\n[TEST 4] Counting events today...")
    today_count = logger.count_events_today()
    print(f"   Events today: {today_count}")
    
    print("\n" + "="*60)
    print(f"✅ All tests passed")
    print(f"✅ Log file: {logger.log_path}")
    print("="*60 + "\n")

