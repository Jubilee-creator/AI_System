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
    from brain.market_scanner import scan_crypto_markets
    BRAIN_OK = True
except ImportError as e:
    print(f"[WARN] Brain modules not found: {e}")
    BRAIN_OK = False

# Import paper trader
try:
    from brain.paper_trader import PaperTrader
    from engine.edge_calculator import MarketData
    PAPER_TRADER_OK = True
except ImportError as e:
    print(f"[WARN] Paper trader not found: {e}")
    PAPER_TRADER_OK = False

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

# Pull active thresholds from config (respects TEST_MODE overrides)
try:
    from config.trading_config import MIN_EDGE, MIN_CONFIDENCE
except ImportError:
    MIN_EDGE = 0.03
    MIN_CONFIDENCE = 0.65

app = Flask(__name__)
CORS(app)

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

# Shared state
state = {
    "markets": [],          # raw crypto markets
    "opportunities": [],    # AI-analyzed opportunities
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
    "risk_decisions": [],   # Recent signal decisions (PLACED / BLOCKED)
    "recent_trades": [],    # Latest trade records (open + settled)
    "risk_status": {},      # Live risk manager state
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

                state["opportunities"] = opportunities
                state["last_scan"] = now
                state["total_scans"] += 1

                # Update alert counts
                arbs = [o for o in opportunities if o["action"] == "ARB"]
                bets = [o for o in opportunities if "BET" in o["action"]]
                state["arb_count"] = len(arbs)
                state["bet_count"] = len(bets)

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
                        
                        # Build MarketData object
                        market_data = MarketData(
                            ticker=opp.get("ticker", "UNKNOWN"),
                            yes_price=opp.get("price_yes", 0.5),
                            no_price=opp.get("price_no", 0.5),
                            yes_bid=opp.get("price_yes", 0.5) - 0.01,
                            yes_ask=opp.get("price_yes", 0.5) + 0.01,
                            no_bid=opp.get("price_no", 0.5) - 0.01,
                            no_ask=opp.get("price_no", 0.5) + 0.01,
                            volume_24h=opp.get("volume", 0),
                            spread=0.02,  # Default 2¢ spread
                            liquidity=opp.get("volume", 0) // 10,  # Estimate
                            fee_rate=0.01,  # Kalshi 1% fee
                            time_to_expiry=24.0,  # Default 24h
                            venue="kalshi"
                        )
                        
                        # Process signal through paper trader
                        estimated_prob = opp.get("confidence", 0.5)
                        if estimated_prob > 0:
                            strategy_label = reason_tag  # ARB | SIGNAL | TREND
                            result = paper_trader.process_signal(
                                market_data=market_data,
                                estimated_prob=estimated_prob,
                                strategy=strategy_label
                            )
                            # Capture decision for risk feed
                            if result:
                                if result.get("status") == "BLOCKED":
                                    decision = {
                                        "time": now,
                                        "ticker": opp.get("ticker", "?"),
                                        "action": opp.get("action", "?"),
                                        "outcome": "BLOCKED",
                                        "reason": result.get("reason", "unknown"),
                                    }
                                else:
                                    decision = {
                                        "time": now,
                                        "ticker": result.get("ticker", "?"),
                                        "action": result.get("action", "?"),
                                        "outcome": "PLACED",
                                        "size": result.get("size", 0),
                                        "price": result.get("entry_price", 0),
                                    }
                                state["risk_decisions"].insert(0, decision)
                    state["risk_decisions"] = state["risk_decisions"][:40]

                    # Update paper stats and expose recent trades
                    state["paper_stats"] = paper_trader.get_stats()
                    state["risk_status"] = paper_trader.get_risk_status()
                    recent = list(paper_trader.open_trades) + list(paper_trader.trade_history[-20:])
                    recent.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
                    state["recent_trades"] = recent[:20]

                    # Reset per-scan position count — paper trades have no
                    # settlement trigger so counts must not accumulate across scans
                    paper_trader.risk_manager.open_positions = 0
                    paper_trader.risk_manager.total_exposure = 0.0

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

/* ── LIVE ACTIONS ── */
.action-row {
  padding: 5px 10px;
  border-bottom: 1px solid rgba(13,40,13,0.5);
  display: grid;
  grid-template-columns: 52px 80px 64px 1fr 48px;
  align-items: center;
  gap: 0;
}
.action-row.placed { border-left: 2px solid var(--green2); background: rgba(0,255,65,0.02); }
.action-row.settled-win { border-left: 2px solid var(--cyan); background: rgba(0,229,255,0.02); }
.action-row.settled-loss { border-left: 2px solid var(--red); background: rgba(255,23,68,0.02); }
.action-time { font-size: 9px; color: var(--green-dim); }
.action-ticker { font-size: 10px; color: var(--cyan); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.action-badge2 { font-size: 9px; font-weight: 700; font-family: 'Orbitron', monospace; }
.action-meta { font-size: 9px; color: var(--muted); padding: 0 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.action-pnl { font-size: 10px; text-align: right; font-weight: 700; }
.pnl-win { color: var(--cyan); }
.pnl-loss { color: var(--red); }
.pnl-open { color: var(--muted); }

/* ── RISK FEED ── */
.risk-row {
  padding: 4px 10px;
  border-bottom: 1px solid rgba(13,40,13,0.4);
  display: flex;
  gap: 6px;
  align-items: flex-start;
}
.risk-row.placed { border-left: 2px solid var(--green2); }
.risk-row.blocked { border-left: 2px solid var(--red); opacity: 0.75; }
.risk-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; margin-top: 3px; }
.risk-dot.placed { background: var(--green2); box-shadow: 0 0 4px var(--green2); }
.risk-dot.blocked { background: var(--red); }
.risk-body { flex: 1; min-width: 0; }
.risk-top { display: flex; gap: 6px; align-items: center; }
.risk-ticker { font-size: 10px; color: var(--cyan); font-weight: 500; }
.risk-action { font-size: 9px; color: var(--text); }
.risk-outcome { font-size: 9px; font-weight: 700; font-family: 'Orbitron', monospace; }
.risk-outcome.placed { color: var(--green2); }
.risk-outcome.blocked { color: var(--red); }
.risk-reason { font-size: 9px; color: var(--muted); margin-top: 1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.risk-time { font-size: 8px; color: var(--green-dim); flex-shrink: 0; }

/* ── STATUS GRID ── */
.status-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--border);
}
.status-cell {
  background: var(--panel);
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
}
.status-val { font-family: 'Orbitron', monospace; font-size: 12px; font-weight: 700; color: var(--green2); text-shadow: 0 0 8px var(--green2); line-height: 1.2; }
.status-val.warn { color: var(--yellow); text-shadow: 0 0 8px var(--yellow); }
.status-val.danger { color: var(--red); text-shadow: 0 0 8px var(--red); }
.status-val.ok { color: var(--green2); }
.status-key { font-size: 7px; color: var(--muted); letter-spacing: 1.5px; text-transform: uppercase; margin-top: 2px; }
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

  <!-- COL 2: LIVE ACTIONS + RISK FEED -->
  <div class="col" style="display:grid;grid-template-rows:auto 1fr auto 1fr">

    <!-- RECENT LIVE ACTIONS -->
    <div class="ph">
      <span class="ph-title">🎯 Recent Live Actions</span>
      <span class="ph-sub" id="actions-count">0 trades</span>
    </div>
    <div class="pb" id="live-actions-feed">
      <div class="empty">No trades placed yet. Scanning...</div>
    </div>

    <!-- RISK DECISION FEED -->
    <div class="ph" style="margin-top:1px">
      <span class="ph-title">🛡 Risk Decision Feed</span>
      <span class="ph-sub" id="risk-count">0 decisions</span>
    </div>
    <div class="pb" id="risk-feed">
      <div class="empty">Monitoring signals...</div>
    </div>
  </div>

  <!-- COL 3: STATUS + PAPER STATS -->
  <div class="col">

    <!-- LIVE STATUS SUMMARY -->
    <div class="ph">
      <span class="ph-title">📡 Live Status</span>
      <span class="ph-sub" id="status-time">--</span>
    </div>
    <div style="flex-shrink:0;border-bottom:1px solid var(--border)">
      <div class="status-grid" id="status-grid"></div>
    </div>

    <!-- PAPER TRADE PERFORMANCE -->
    <div class="ph" style="flex-shrink:0">
      <span class="ph-title">📊 Performance</span>
      <span class="ph-sub" id="paper-progress">0 / 100 trades</span>
    </div>
    <div class="pb" style="padding:0">

      <!-- Stats Grid -->
      <div class="stat-grid">
        <div class="stat-cell">
          <div class="stat-val-big" id="p-trades">0</div>
          <div class="stat-label-small">Total Trades</div>
        </div>
        <div class="stat-cell">
          <div class="stat-val-big" id="p-settled">0</div>
          <div class="stat-label-small">Settled</div>
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
          <div class="stat-label-small">Avg EV</div>
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

function renderOpportunities(opps) {
  const el = document.getElementById('opp-feed');
  document.getElementById('opp-count').textContent = opps.length + ' markets';

  if (!opps.length) {
    el.innerHTML = '<div class="empty">No crypto markets found.<br>Markets are most liquid 9am–4pm ET + sports events.</div>';
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
    return;
  }

  const total = stats.total_trades || 0;
  const settled = stats.settled_trades || 0;
  const winRate = stats.win_rate || 0;
  const pnl = stats.total_pnl || 0;
  const edge = stats.avg_edge || 0;
  const ev = stats.avg_ev || 0;
  const clv = stats.avg_clv || 0;
  const sharpe = stats.sharpe || 0;

  document.getElementById('p-trades').textContent = total;
  document.getElementById('p-settled').textContent = settled;
  document.getElementById('p-winrate').textContent = settled > 0 ? (winRate * 100).toFixed(1) + '%' : '--';
  
  const pnlEl = document.getElementById('p-pnl');
  pnlEl.textContent = pnl >= 0 ? '+$' + pnl.toFixed(2) : '-$' + Math.abs(pnl).toFixed(2);
  pnlEl.className = 'stat-val-big ' + (pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral');

  document.getElementById('p-edge').textContent = settled > 0 ? (edge > 0 ? '+' : '') + (edge * 100).toFixed(2) + '%' : '--';
  document.getElementById('p-ev').textContent = settled > 0 ? (ev > 0 ? '+' : '') + ev.toFixed(3) : '--';
  document.getElementById('p-clv').textContent = settled > 0 ? (clv > 0 ? '+' : '') + clv.toFixed(3) : '--';
  document.getElementById('p-sharpe').textContent = settled > 1 ? sharpe.toFixed(2) : '--';
  document.getElementById('paper-progress').textContent = total + ' / 100 trades';

  // Verdicts
  const profitable = stats.is_profitable || false;
  const hasEdge = stats.has_edge || false;
  const beatsClosing = stats.beats_closing || false;

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

function renderLiveActions(trades) {
  const el = document.getElementById('live-actions-feed');
  document.getElementById('actions-count').textContent = trades.length + ' trades';
  if (!trades.length) {
    el.innerHTML = '<div class="empty">No trades placed yet. Scanning...</div>';
    return;
  }
  el.innerHTML = trades.map(t => {
    const ts = (t.timestamp||'').slice(11,19) || '--:--:--';
    const isOpen = t.status === 'OPEN';
    const isWin = t.result === 'WIN';
    const rowCls = isOpen ? 'placed' : (isWin ? 'settled-win' : 'settled-loss');
    const pnlHtml = isOpen
      ? `<span class="action-pnl pnl-open">open</span>`
      : `<span class="action-pnl ${isWin ? 'pnl-win' : 'pnl-loss'}">${t.pnl >= 0 ? '+' : ''}$${(t.pnl||0).toFixed(2)}</span>`;
    const actionColor = t.action === 'BET_YES' ? 'ab-yes' : t.action === 'ARB' ? 'ab-arb' : 'ab-no';
    const meta = `$${(t.size||0).toFixed(0)} @ ${((t.entry_price||0)*100).toFixed(0)}¢`;
    return `<div class="action-row ${rowCls}">
      <span class="action-time">${ts}</span>
      <span class="action-ticker">${t.ticker||'?'}</span>
      <span class="action-badge2 ${actionColor}">${t.action||'?'}</span>
      <span class="action-meta">${meta}</span>
      ${pnlHtml}
    </div>`;
  }).join('');
}

function renderRiskFeed(decisions) {
  const el = document.getElementById('risk-feed');
  document.getElementById('risk-count').textContent = decisions.length + ' decisions';
  if (!decisions.length) {
    el.innerHTML = '<div class="empty">Monitoring signals...</div>';
    return;
  }
  el.innerHTML = decisions.map(d => {
    const isPlaced = d.outcome === 'PLACED';
    const rowCls = isPlaced ? 'placed' : 'blocked';
    const dotCls = isPlaced ? 'placed' : 'blocked';
    const outcomeLabel = isPlaced ? 'PLACED' : 'BLOCKED';
    const sizeStr = isPlaced ? ` $${(d.size||0).toFixed(0)} @ ${((d.price||0)*100).toFixed(0)}¢` : '';
    const reasonHtml = !isPlaced
      ? `<div class="risk-reason">${d.reason||''}</div>` : '';
    return `<div class="risk-row ${rowCls}">
      <div class="risk-dot ${dotCls}"></div>
      <div class="risk-body">
        <div class="risk-top">
          <span class="risk-ticker">${d.ticker||'?'}</span>
          <span class="risk-action">${d.action||''}</span>
          <span class="risk-outcome ${dotCls}">${outcomeLabel}${sizeStr}</span>
        </div>
        ${reasonHtml}
      </div>
      <span class="risk-time">${d.time||''}</span>
    </div>`;
  }).join('');
}

function renderStatusSummary(d) {
  const rs = d.risk_status || {};
  const ps = d.paper_stats || {};
  const now = d.last_scan || '--';
  document.getElementById('status-time').textContent = now;

  const dailyPnl = rs.daily_pnl || 0;
  const bankroll = rs.bankroll || ps.current_balance || d.bankroll || 500;
  const tradesT = rs.trades_today || 0;
  const maxT = rs.max_trades_per_day || 100;
  const streak = rs.loss_streak || 0;
  const streakMax = rs.loss_streak_trigger || 10;
  const openPos = rs.open_positions || 0;
  const killSwitch = rs.kill_switch_active || false;
  const cooldown = rs.cooldown_active || false;
  const opps = (d.opportunities||[]).filter(o => o.action !== 'PASS').length;
  const arbs = (d.opportunities||[]).filter(o => o.action === 'ARB').length;
  const bets = (d.opportunities||[]).filter(o => o.action && o.action.includes('BET')).length;

  const pnlClass = dailyPnl > 0 ? 'ok' : dailyPnl < -25 ? 'danger' : dailyPnl < -10 ? 'warn' : 'ok';
  const systemStatus = killSwitch ? 'KILL SW' : cooldown ? 'COOLDOWN' : 'LIVE';
  const sysClass = killSwitch ? 'danger' : cooldown ? 'warn' : 'ok';

  document.getElementById('status-grid').innerHTML = `
    <div class="status-cell">
      <div class="status-val ${sysClass}">${systemStatus}</div>
      <div class="status-key">System</div>
    </div>
    <div class="status-cell">
      <div class="status-val ${pnlClass}">${dailyPnl >= 0 ? '+' : ''}$${dailyPnl.toFixed(2)}</div>
      <div class="status-key">Daily P&L</div>
    </div>
    <div class="status-cell">
      <div class="status-val">${d.total_scans||0}</div>
      <div class="status-key">Scans</div>
    </div>
    <div class="status-cell">
      <div class="status-val">${opps} <span style="font-size:9px;color:var(--muted)">(${arbs}arb/${bets}bet)</span></div>
      <div class="status-key">Actionable</div>
    </div>
    <div class="status-cell">
      <div class="status-val">${tradesT}<span style="font-size:9px;color:var(--muted)">/${maxT}</span></div>
      <div class="status-key">Trades Today</div>
    </div>
    <div class="status-cell">
      <div class="status-val">${openPos}</div>
      <div class="status-key">Open Pos</div>
    </div>
    <div class="status-cell">
      <div class="status-val ${streak >= streakMax * 0.7 ? 'warn' : 'ok'}">${streak}<span style="font-size:9px;color:var(--muted)">/${streakMax}</span></div>
      <div class="status-key">Loss Streak</div>
    </div>
    <div class="status-cell">
      <div class="status-val">${(bankroll).toFixed(0)}</div>
      <div class="status-key">Balance $</div>
    </div>`;
}

async function fetchState() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();

    // Header
    const arbs = (d.opportunities||[]).filter(o => o.action === 'ARB').length;
    const bets = (d.opportunities||[]).filter(o => o.action && o.action.includes('BET')).length;
    const paperTrades = (d.paper_stats && d.paper_stats.total_trades) || 0;

    document.getElementById('h-opps').textContent = (d.opportunities||[]).length;
    document.getElementById('h-arb').textContent = arbs;
    document.getElementById('h-bet').textContent = bets;
    document.getElementById('h-scans').textContent = d.total_scans;
    document.getElementById('h-paper-trades').textContent = paperTrades;
    document.getElementById('h-last').textContent = 'Last: ' + (d.last_scan||'--');

    renderOpportunities(d.opportunities || []);
    renderLiveActions(d.recent_trades || []);
    renderRiskFeed(d.risk_decisions || []);
    renderStatusSummary(d);
    renderPaperStats(d.paper_stats || {});

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
    if paper_trader:
        return jsonify(paper_trader.get_stats())
    return jsonify({"error": "Paper trader not initialized"})


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