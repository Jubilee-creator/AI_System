"""
logs/trade_logger.py
--------------------
Trade logger for paper and live trading

Logs all trades to JSONL file for analysis
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path


DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
    "paper_trades.jsonl"
)


class TradeLogger:
    """
    Trade logger for paper and live trades.
    
    Logs all trades to JSONL file.
    """
    
    def __init__(self, log_file: Optional[str] = None):
        """
        Initialize trade logger.
        
        Args:
            log_file: Path to log file (default: logs/paper_trades.jsonl)
        """
        self.log_file = log_file or DEFAULT_LOG_PATH
        
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        
        if not os.path.exists(self.log_file):
            Path(self.log_file).touch()
            print(f"[LOGGER] Created: {self.log_file}")
        else:
            print(f"[LOGGER] Initialized: {self.log_file}")
    
    
    def log_trade(self, trade) -> None:
        """
        Log a trade to file.
        
        Args:
            trade: Trade as dict or object with attributes
        """
        
        def get_value(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            else:
                return getattr(obj, key, default)
        
        trade_record = {
            "timestamp": get_value(trade, "timestamp", datetime.now(timezone.utc).isoformat()),
            "ticker": get_value(trade, "ticker", "UNKNOWN"),
            "action": get_value(trade, "action", "UNKNOWN"),
            "strategy": get_value(trade, "strategy", "UNKNOWN"),
            "entry_price": get_value(trade, "entry_price", 0.0),
            "size": get_value(trade, "size", 0.0),
            "confidence": get_value(trade, "confidence", 0.0),
            "edge": get_value(trade, "edge", 0.0),
            "status": get_value(trade, "status", "UNKNOWN"),
            "result": get_value(trade, "result"),
            "pnl": get_value(trade, "pnl", 0.0),
            "exit_price": get_value(trade, "exit_price"),
            "settled_at": get_value(trade, "settled_at"),
            "is_paper": get_value(trade, "is_paper", True)
        }
        
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(trade_record) + "\n")
        except Exception as e:
            print(f"[TRADE_LOGGER ERROR] Failed to log trade: {e}")
    
    
    def get_trades(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get all trades from log.
        
        Args:
            limit: Maximum number of trades to return (most recent first)
        
        Returns:
            List of trade dicts
        """
        if not os.path.exists(self.log_file):
            return []
        
        trades = []
        
        try:
            with open(self.log_file, "r") as f:
                for line in f:
                    try:
                        trade = json.loads(line.strip())
                        trades.append(trade)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"[TRADE_LOGGER ERROR] Failed to read trades: {e}")
            return []
        
        if limit:
            return trades[-limit:]
        
        return trades
    
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get trading statistics from log.
        
        Returns:
            Dict with stats
        """
        trades = self.get_trades()
        
        if not trades:
            return {
                "total_trades": 0,
                "settled_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0
            }
        
        settled = [t for t in trades if t.get("status") == "SETTLED"]
        wins = [t for t in settled if t.get("result") == "WIN"]
        losses = [t for t in settled if t.get("result") == "LOSS"]
        
        total_pnl = sum(t.get("pnl", 0.0) for t in settled)
        avg_pnl = total_pnl / len(settled) if settled else 0.0
        win_rate = len(wins) / len(settled) if settled else 0.0
        
        return {
            "total_trades": len(trades),
            "settled_trades": len(settled),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2)
        }


if __name__ == "__main__":
    print("\n" + "="*60)
    print("TRADE LOGGER TEST")
    print("="*60)
    
    logger = TradeLogger()
    
    print("\n[TEST 1] Log dict trade")
    trade_dict = {
        "ticker": "TEST-001",
        "action": "BET_YES",
        "strategy": "SIGNAL",
        "entry_price": 0.45,
        "size": 25.0,
        "confidence": 0.68,
        "edge": 0.035,
        "status": "OPEN"
    }
    logger.log_trade(trade_dict)
    print("  ✓ Dict trade logged")
    
    print("\n[TEST 2] Get trades")
    trades = logger.get_trades(limit=5)
    print(f"  Found {len(trades)} trades")
    
    print("\n[TEST 3] Get stats")
    stats = logger.get_stats()
    print(f"  Total trades: {stats['total_trades']}")
    print(f"  Settled: {stats['settled_trades']}")
    
    print("\n" + "="*60)
    print("✅ All tests complete")
    print("="*60 + "\n")
