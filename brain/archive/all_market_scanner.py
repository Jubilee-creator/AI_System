"""
brain/all_market_scanner.py
----------------------------
DIAGNOSTIC SCANNER - NOT FOR PRODUCTION

Scans ALL open Kalshi markets (not just crypto) to diagnose:
- Can the engine generate valid signals from any markets?
- Are there opportunities in the broader feed?
- Is the crypto filter too restrictive?

This is temporary diagnostic code.
Production scanner: brain/market_scanner.py
"""

import os
import time
import json
import requests
import statistics
from datetime import datetime, timezone
from typing import Optional
from cryptography.hazmat.primitives import serialization
from collections import defaultdict

# Local engine
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.decision_engine import (
    MarketSignal, TradeDecision, analyze_market, compute_arb_edge
)


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.getenv(
    "KALSHI_PRIVATE_KEY_PATH",
    "/Users/samuel/Desktop/AI_System/kalshi_private_key.pem"
)

BANKROLL = float(os.getenv("BANKROLL", "500"))
MIN_VOLUME = 0
MIN_EDGE = 0.015
SCAN_INTERVAL = 30

# Pagination settings
MAX_TOTAL_MARKETS = 500       # Fetch up to 500 markets for diagnosis
MARKETS_PER_PAGE = 200


# ─────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────

def _load_private_key():
    try:
        with open(PRIVATE_KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        print(f"[AUTH ERROR] Private key not found at: {PRIVATE_KEY_PATH}")
        return None
    except Exception as e:
        print(f"[AUTH ERROR] Could not load private key: {e}")
        return None


def _build_auth_header(method: str, path: str) -> dict:
    private_key = _load_private_key()
    if not private_key:
        return {}

    timestamp = str(int(time.time() * 1000))
    msg = timestamp + method.upper() + path

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = private_key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())

    import base64
    sig_b64 = base64.b64encode(signature).decode()

    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "Content-Type": "application/json"
    }


def kalshi_get(path: str) -> Optional[dict]:
    headers = _build_auth_header("GET", path)
    if not headers:
        print(f"[API] ❌ No auth headers")
        return None
    try:
        r = requests.get(KALSHI_API_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[API] ❌ {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        print(f"[API ERROR] {e}")
        return None


# ─────────────────────────────────────────
# MARKET FETCHER (ALL MARKETS)
# ─────────────────────────────────────────

def fetch_all_markets() -> list[dict]:
    """
    Fetch ALL active Kalshi markets (no filtering).
    Uses pagination to get comprehensive view.
    """
    print(f"\n[ALL_SCANNER] Fetching ALL open markets from Kalshi API...")
    
    all_markets = []
    total_fetched = 0
    page_num = 0
    cursor = None
    
    while True:
        page_num += 1
        
        if cursor:
            path = f"/markets?limit={MARKETS_PER_PAGE}&status=open&cursor={cursor}"
        else:
            path = f"/markets?limit={MARKETS_PER_PAGE}&status=open"
        
        print(f"[ALL_SCANNER] Fetching page {page_num}...")
        data = kalshi_get(path)
        
        if not data or "markets" not in data:
            print(f"[ALL_SCANNER] ❌ Failed to fetch page {page_num}")
            break
        
        page_markets = data["markets"]
        
        if len(page_markets) == 0:
            print(f"[ALL_SCANNER] Page {page_num} returned 0 markets - end of results")
            break
        
        all_markets.extend(page_markets)
        total_fetched += len(page_markets)
        print(f"[ALL_SCANNER]   Page {page_num}: {len(page_markets)} markets (total: {total_fetched})")
        
        if total_fetched >= MAX_TOTAL_MARKETS:
            print(f"[ALL_SCANNER] Reached max limit ({MAX_TOTAL_MARKETS}) - stopping")
            break
        
        cursor = data.get("cursor")
        if not cursor:
            print(f"[ALL_SCANNER] No more pages")
            break
    
    print(f"\n[ALL_SCANNER] ✓ Fetched {len(all_markets)} total markets across {page_num} pages")
    
    return all_markets


# ─────────────────────────────────────────
# VOLUME TRACKER
# ─────────────────────────────────────────

class VolumeTracker:
    def __init__(self, window: int = 20):
        self.history: dict[str, list] = defaultdict(list)
        self.window = window

    def update(self, ticker: str, volume: float) -> float:
        hist = self.history[ticker]
        hist.append(volume)
        if len(hist) > self.window:
            hist.pop(0)
        if len(hist) < 3:
            return 1.0
        avg = statistics.mean(hist)
        return volume / avg if avg > 0 else 1.0


_volume_tracker = VolumeTracker()


# ─────────────────────────────────────────
# SIGNAL BUILDER
# ─────────────────────────────────────────

_price_history: dict[str, list] = defaultdict(list)


def build_signal(market: dict) -> Optional[MarketSignal]:
    """Convert raw Kalshi market dict → MarketSignal for the engine."""
    try:
        ticker = market.get("ticker", "")
        
        # Extract prices (Kalshi returns in cents, convert to decimal)
        yes_ask = market.get("yes_ask")
        yes_bid = market.get("yes_bid")
        no_ask = market.get("no_ask")
        no_bid = market.get("no_bid")
        
        # Convert to decimal, handling None
        yes_ask = yes_ask / 100.0 if yes_ask is not None else None
        yes_bid = yes_bid / 100.0 if yes_bid is not None else None
        no_ask = no_ask / 100.0 if no_ask is not None else None
        no_bid = no_bid / 100.0 if no_bid is not None else None

        # Calculate mid prices
        yes_mid = None
        no_mid = None
        
        if yes_ask is not None and yes_bid is not None:
            yes_mid = (yes_ask + yes_bid) / 2
        elif yes_ask is not None:
            yes_mid = yes_ask
        elif yes_bid is not None:
            yes_mid = yes_bid
            
        if no_ask is not None and no_bid is not None:
            no_mid = (no_ask + no_bid) / 2
        elif no_ask is not None:
            no_mid = no_ask
        elif no_bid is not None:
            no_mid = no_bid

        # Skip if no pricing data
        if yes_mid is None and no_mid is None:
            return None

        # If only one side priced, infer the other
        if yes_mid is None and no_mid is not None:
            yes_mid = 1.0 - no_mid
        if no_mid is None and yes_mid is not None:
            no_mid = 1.0 - yes_mid

        # Final check
        if yes_mid is None or no_mid is None:
            return None

        # Volume
        volume = market.get("volume") or market.get("volume_24h") or market.get("open_interest") or 0
        volume = float(volume)
        vol_spike = _volume_tracker.update(ticker, volume)

        # Price change (from history)
        price_hist = _price_history[ticker]
        price_hist.append(yes_mid)
        if len(price_hist) > 20:
            price_hist.pop(0)

        price_change = 0.0
        if len(price_hist) >= 3:
            price_change = price_hist[-1] - price_hist[-3]

        # Volatility
        volatility = 0.03
        if len(price_hist) >= 5:
            try:
                volatility = statistics.stdev(price_hist[-5:])
            except Exception:
                volatility = 0.03

        return MarketSignal(
            ticker=ticker,
            price_yes=yes_mid,
            price_no=no_mid,
            volume=volume * vol_spike,
            price_change=price_change,
            volatility=max(0.001, volatility),
            order_book_imbalance=0.0
        )

    except Exception as e:
        print(f"[BUILD_SIGNAL] Error on {market.get('ticker', '?')}: {e}")
        return None


# ─────────────────────────────────────────
# COMPANION FINDER
# ─────────────────────────────────────────

def find_companion(ticker: str, all_markets: list[dict]) -> Optional[MarketSignal]:
    """Find related market for cross-market z-score."""
    base = ticker.split("_")[0] if "_" in ticker else ticker[:6]

    for m in all_markets:
        t = m.get("ticker", "")
        if t != ticker and base.lower() in t.lower():
            return build_signal(m)
    return None


# ─────────────────────────────────────────
# MAIN DIAGNOSTIC SCANNER
# ─────────────────────────────────────────

def scan_all_markets(bankroll: float = BANKROLL) -> list[dict]:
    """
    DIAGNOSTIC SCAN: Process ALL open markets.
    Returns sorted list of opportunities.
    """
    print(f"\n{'='*70}")
    print(f"[ALL_SCAN] {datetime.now().strftime('%H:%M:%S')} — DIAGNOSTIC: Scanning ALL markets")
    print(f"[ALL_SCAN] This is NOT the production crypto scanner")
    print(f"{'='*70}")

    skip_reasons = {
        "no_signal_data": 0,
        "low_volume_no_arb": 0,
        "pass_low_edge": 0,
    }
    
    skip_examples = {
        "no_signal_data": [],
        "pass_low_edge": [],
    }
    
    markets = fetch_all_markets()
    if not markets:
        print(f"[ALL_SCAN] ❌ No markets fetched")
        return []

    opportunities = []
    signals_processed = 0

    for market in markets:
        ticker = market.get("ticker", "UNKNOWN")
        signal = build_signal(market)
        
        if not signal:
            skip_reasons["no_signal_data"] += 1
            if len(skip_examples["no_signal_data"]) < 5:
                yes_ask = market.get("yes_ask")
                yes_bid = market.get("yes_bid")
                no_ask = market.get("no_ask")
                no_bid = market.get("no_bid")
                skip_examples["no_signal_data"].append(
                    f"{ticker}: yes_ask={yes_ask} yes_bid={yes_bid} no_ask={no_ask} no_bid={no_bid}"
                )
            continue
        
        signals_processed += 1

        # Check for ARB
        arb_edge = compute_arb_edge(signal.price_yes, signal.price_no)
        
        if signal.volume < MIN_VOLUME and arb_edge is None:
            skip_reasons["low_volume_no_arb"] += 1
            continue

        # Find companion
        companion = find_companion(signal.ticker, markets)

        # Run decision engine (same signature as crypto scanner)
        decision = analyze_market(
            signal=signal,
            bankroll=bankroll,
            related_signal=companion,
            inventory_imbalance=0.0,
            market_time_remaining=300.0
        )

        # Build result (same format as crypto scanner)
        result = {
            "ticker": signal.ticker,
            "title": market.get("title", signal.ticker)[:50],
            "action": decision.action,
            "confidence": round(decision.confidence, 3),
            "edge": round(decision.edge, 4),
            "kelly_frac": round(decision.kelly_fraction, 4),
            "bet_size": round(decision.dollar_size, 2),
            "price_yes": round(signal.price_yes, 3),
            "price_no": round(signal.price_no, 3),
            "yes_plus_no": round(signal.price_yes + signal.price_no, 3),
            "arb_edge": decision.arb_edge,
            "z_score": round(decision.z_score, 2) if decision.z_score else None,
            "volume": int(signal.volume),
            "reasoning": decision.reasoning,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "close_time": market.get("close_time", ""),
            "result_time": market.get("expected_expiration_time", "")
        }

        if result["action"] == "PASS":
            skip_reasons["pass_low_edge"] += 1
            if len(skip_examples["pass_low_edge"]) < 5:
                skip_examples["pass_low_edge"].append(
                    f"{ticker}: edge={decision.edge:.4f} (min={MIN_EDGE:.4f})"
                )

        opportunities.append(result)

    # Sort (same as crypto scanner)
    def sort_key(o):
        action_rank = {"ARB": 0, "BET_YES": 1, "BET_NO": 1, "PASS": 2}
        return (action_rank.get(o["action"], 3), -o["edge"])

    opportunities.sort(key=sort_key)

    # Count actionable
    actionable = [o for o in opportunities if o["action"] != "PASS"]
    arb_count = len([o for o in opportunities if o["action"] == "ARB"])
    bet_count = len([o for o in opportunities if "BET" in o["action"]])
    
    # Print summary
    print(f"\n[ALL_SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[ALL_SCAN] DIAGNOSTIC RESULTS:")
    print(f"[ALL_SCAN]   Total markets fetched: {len(markets)}")
    print(f"[ALL_SCAN]   Markets with usable price data: {signals_processed}")
    print(f"[ALL_SCAN]   Markets missing price data: {skip_reasons['no_signal_data']}")
    print(f"[ALL_SCAN]   Total opportunities generated: {len(opportunities)}")
    print(f"[ALL_SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[ALL_SCAN] SKIP REASONS:")
    print(f"[ALL_SCAN]   No signal data: {skip_reasons['no_signal_data']}")
    for ex in skip_examples["no_signal_data"]:
        print(f"[ALL_SCAN]     • {ex}")
    print(f"[ALL_SCAN]   Low volume (no ARB): {skip_reasons['low_volume_no_arb']}")
    print(f"[ALL_SCAN]   PASS (edge < {MIN_EDGE:.3f}): {skip_reasons['pass_low_edge']}")
    for ex in skip_examples["pass_low_edge"]:
        print(f"[ALL_SCAN]     • {ex}")
    print(f"[ALL_SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[ALL_SCAN] ACTIONABLE OPPORTUNITIES:")
    print(f"[ALL_SCAN]   ARB: {arb_count}")
    print(f"[ALL_SCAN]   BET: {bet_count}")
    print(f"[ALL_SCAN]   Total actionable: {len(actionable)}")
    print(f"[ALL_SCAN] ──────────────────────────────────────────────────────────────────")
    
    if actionable:
        print(f"\n[ALL_SCAN] 🎯 TOP {min(10, len(actionable))} ACTIONABLE OPPORTUNITIES:")
        for i, o in enumerate(actionable[:10], 1):
            flag = "🚨 ARB" if o["action"] == "ARB" else "✅ BET"
            print(f"[ALL_SCAN]   {i}. {flag} {o['ticker']:30} | {o['action']:7} | conf={o['confidence']:.1%} | edge={o['edge']:+.4f} | ${o['bet_size']:5.0f}")
            print(f"[ALL_SCAN]      {o['title']}")
    else:
        print(f"\n[ALL_SCAN] ⚠️  NO ACTIONABLE OPPORTUNITIES FOUND")
        print(f"[ALL_SCAN] This indicates:")
        if skip_reasons['pass_low_edge'] > 0:
            print(f"[ALL_SCAN]   - Engine is working (generated {skip_reasons['pass_low_edge']} PASS decisions)")
            print(f"[ALL_SCAN]   - But all edges are < {MIN_EDGE:.3f} threshold")
        if signals_processed == 0:
            print(f"[ALL_SCAN]   - No markets have usable price data")
            print(f"[ALL_SCAN]   - May be outside trading hours")

    print(f"{'='*70}\n")
    
    # Save to diagnostic output file
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "diagnostic_all_markets.json"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "diagnostic": True,
            "total_markets": len(markets),
            "valid_signals": signals_processed,
            "total_opportunities": len(opportunities),
            "actionable_opportunities": len(actionable),
            "bankroll": bankroll,
            "opportunities": opportunities
        }, f, indent=2)
    
    print(f"[ALL_SCAN] 💾 Diagnostic results saved to: {output_path}")
    
    return opportunities


if __name__ == "__main__":
    print("\n" + "="*70)
    print("DIAGNOSTIC ALL-MARKET SCANNER")
    print("="*70)
    
    results = scan_all_markets()
    
    print(f"\n" + "="*70)
    print(f"DIAGNOSTIC COMPLETE")
    print(f"Total opportunities: {len(results)}")
    print(f"Actionable: {len([r for r in results if r['action'] != 'PASS'])}")
    print(f"="*70 + "\n")