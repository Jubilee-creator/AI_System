"""
brain/market_scanner.py
-----------------------
AI_System — Crypto Market Scanner
Targets Kalshi BTC/ETH/SOL short-duration markets.
Feeds raw market data → decision_engine → ranked opportunities.

UPDATED: Added pagination for comprehensive market search
"""

import os
import time
import json
import requests
import jwt
import statistics
import re
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

# Crypto market series prefixes (exact match required)
CRYPTO_SERIES = [
    "KXBTCD",   # BTC daily direction
    "KXETHD",   # ETH daily direction
    "KXBTCU",   # BTC up/down short
    "KXETHU",   # ETH up/down short
    "KXBTC",    # BTC price targets
    "KXETH",    # ETH price targets
    "KXSOL",    # SOL
]

# Crypto asset names (require word boundaries)
CRYPTO_ASSETS = [
    "bitcoin",
    "btc",
    "ethereum", 
    "eth",
    "solana",
    "sol",
    "dogecoin",
    "doge",
    "cardano",
    "ada",
    "polygon",
    "matic",
    "ripple",
    "xrp",
]

BANKROLL = float(os.getenv("BANKROLL", "500"))
MIN_VOLUME = 0          # Minimum $volume to consider (0 = no filter)
MIN_EDGE = 0.015        # 1.5% minimum net edge
SCAN_INTERVAL = 30      # Seconds between scans

# Pagination settings
MAX_TOTAL_MARKETS = 1000      # Stop fetching after this many total markets
MARKETS_PER_PAGE = 200        # Markets to fetch per API call
MIN_CRYPTO_TARGET = 10        # Stop early if we find this many crypto markets


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
        print(f"[API] ❌ No auth headers - check KEY_ID and private key path")
        return None
    try:
        r = requests.get(KALSHI_API_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 401:
            print(f"[API] ❌ Authentication failed - check credentials")
            return None
        elif r.status_code == 429:
            print(f"[API] ❌ Rate limited - waiting before retry")
            return None
        else:
            print(f"[API] ❌ {path} → {r.status_code}: {r.text[:200]}")
            return None
    except requests.Timeout:
        print(f"[API ERROR] Timeout fetching {path}")
        return None
    except requests.ConnectionError:
        print(f"[API ERROR] Connection error - check internet connection")
        return None
    except Exception as e:
        print(f"[API ERROR] {e}")
        return None


# ─────────────────────────────────────────
# CRYPTO DETECTION
# ─────────────────────────────────────────

def is_crypto_market(market: dict) -> tuple[bool, str]:
    """
    Determine if market is crypto-related.
    
    Returns:
        (is_crypto: bool, reason: str)
    """
    ticker = market.get("ticker", "").upper()
    title = market.get("title", "").lower()
    subtitle = market.get("subtitle", "").lower()
    
    # Check 1: Ticker series prefix (most reliable)
    for series in CRYPTO_SERIES:
        if ticker.startswith(series):
            return True, f"ticker_series:{series}"
    
    # Check 2: Crypto asset with word boundaries in title
    # Use regex word boundaries to avoid matching "eth" in "whether" etc.
    combined_text = f"{title} {subtitle}"
    
    for asset in CRYPTO_ASSETS:
        # Create pattern with word boundaries
        pattern = r'\b' + re.escape(asset) + r'\b'
        if re.search(pattern, combined_text, re.IGNORECASE):
            return True, f"asset_keyword:{asset}"
    
    return False, "no_crypto_match"


def fetch_crypto_markets() -> list[dict]:
    """
    Fetch active Kalshi markets and filter for crypto ones.
    Uses pagination to search through multiple pages.
    """
    print(f"[SCANNER] Fetching markets from Kalshi API with pagination...")
    
    all_crypto = []
    total_fetched = 0
    page_num = 0
    cursor = None
    match_reasons = {}
    
    while True:
        page_num += 1
        
        # Build API path with cursor if available
        if cursor:
            path = f"/markets?limit={MARKETS_PER_PAGE}&status=open&cursor={cursor}"
        else:
            path = f"/markets?limit={MARKETS_PER_PAGE}&status=open"
        
        print(f"[SCANNER] Fetching page {page_num}...")
        data = kalshi_get(path)
        
        if not data:
            print(f"[SCANNER] ❌ API request failed on page {page_num}")
            break
        
        if "markets" not in data:
            print(f"[SCANNER] ❌ Unexpected API response format: {list(data.keys())}")
            break
        
        page_markets = data["markets"]
        # DEBUG: print sample of markets
        print("\n[DEBUG SAMPLE MARKETS]")
        for m in page_markets[:10]:
            print(m.get("ticker"), "|", m.get("title"))
        page_crypto_count = 0
        
        if len(page_markets) == 0:
            print(f"[SCANNER] Page {page_num} returned 0 markets - end of results")
            break
        
        total_fetched += len(page_markets)
        print(f"[SCANNER]   Page {page_num}: {len(page_markets)} markets")
        
        # Filter for crypto markets
        for m in page_markets:
            is_crypto, reason = is_crypto_market(m)
            
            if is_crypto:
                all_crypto.append(m)
                page_crypto_count += 1
                match_reasons[reason] = match_reasons.get(reason, 0) + 1
        
        if page_crypto_count > 0:
            print(f"[SCANNER]   Page {page_num}: Found {page_crypto_count} crypto markets")
        
        # Check stopping conditions
        if total_fetched >= MAX_TOTAL_MARKETS:
            print(f"[SCANNER] Reached max total markets ({MAX_TOTAL_MARKETS}) - stopping")
            break
        
        if len(all_crypto) >= MIN_CRYPTO_TARGET:
            print(f"[SCANNER] Found {len(all_crypto)} crypto markets (target: {MIN_CRYPTO_TARGET}) - stopping early")
            break
        
        # Check for next page
        cursor = data.get("cursor")
        if not cursor:
            print(f"[SCANNER] No more pages available")
            break
    
    print(f"\n[SCANNER] ✓ Pagination complete:")
    print(f"[SCANNER]   Pages fetched: {page_num}")
    print(f"[SCANNER]   Total markets scanned: {total_fetched}")
    print(f"[SCANNER]   Crypto markets found: {len(all_crypto)}")
    
    if match_reasons:
        print(f"[SCANNER] Match breakdown:")
        for reason, count in sorted(match_reasons.items()):
            print(f"  {reason}: {count}")
    
    if len(all_crypto) > 0:
        print(f"[SCANNER] Crypto markets found:")
        for m in all_crypto[:5]:
            ticker = m.get("ticker", "N/A")
            title = m.get("title", "N/A")[:50]
            is_crypto, reason = is_crypto_market(m)
            print(f"  ✓ {ticker}: {title} [{reason}]")
        if len(all_crypto) > 5:
            print(f"  ... and {len(all_crypto) - 5} more")
    else:
        print(f"\n[SCANNER] ⚠️  No crypto markets found across {page_num} pages ({total_fetched} markets)")
        print(f"[SCANNER] This means Kalshi currently has no open crypto markets matching our filters.")
        print(f"[SCANNER] Crypto series: {CRYPTO_SERIES}")
        print(f"[SCANNER] Crypto assets: {CRYPTO_ASSETS}")
        print(f"[SCANNER] This is normal if:")
        print(f"[SCANNER]   - Outside crypto trading hours")
        print(f"[SCANNER]   - Kalshi has temporarily delisted crypto markets")
        print(f"[SCANNER]   - All crypto markets have closed/settled")
    
    return all_crypto


# ─────────────────────────────────────────
# VOLUME TRACKER (detects spikes)
# ─────────────────────────────────────────

class VolumeTracker:
    def __init__(self, window: int = 20):
        self.history: dict[str, list] = defaultdict(list)
        self.window = window

    def update(self, ticker: str, volume: float) -> float:
        """Returns normalized volume (1.0 = average, >2.0 = spike)."""
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

        # Volume - try multiple fields
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

        # Volatility (rolling std of recent prices)
        volatility = 0.03  # default
        if len(price_hist) >= 5:
            try:
                volatility = statistics.stdev(price_hist[-5:])
            except Exception:
                volatility = 0.03

        # OBI (set to 0 for now, can fetch orderbook if needed)
        obi = 0.0

        return MarketSignal(
            ticker=ticker,
            price_yes=yes_mid,
            price_no=no_mid,
            volume=volume * vol_spike,
            price_change=price_change,
            volatility=max(0.001, volatility),
            order_book_imbalance=obi
        )

    except Exception as e:
        print(f"[BUILD_SIGNAL] Error on {market.get('ticker', '?')}: {e}")
        return None


# ─────────────────────────────────────────
# COMPANION MARKET FINDER
# ─────────────────────────────────────────

def find_companion(ticker: str, all_markets: list[dict]) -> Optional[MarketSignal]:
    """
    Find the related market for cross-market z-score.
    E.g. BTC_5M companion is BTC_15M or prior 5M window.
    """
    base = ticker.split("_")[0] if "_" in ticker else ticker[:6]

    for m in all_markets:
        t = m.get("ticker", "")
        if t != ticker and base.lower() in t.lower():
            return build_signal(m)
    return None


# ─────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────

def scan_crypto_markets(bankroll: float = BANKROLL) -> list[dict]:
    """
    Full scan: fetch → build signals → run engine → rank by edge.
    Returns sorted list of opportunities.
    """
    print(f"\n{'='*70}")
    print(f"[SCAN] {datetime.now().strftime('%H:%M:%S')} — Starting crypto market scan")
    print(f"{'='*70}")

    # Debug counters
    skip_reasons = {
        "no_signal_data": 0,
        "low_volume_no_arb": 0,
        "pass_low_edge": 0,
    }
    
    skip_examples = {
        "no_signal_data": [],
        "low_volume_no_arb": [],
        "pass_low_edge": [],
    }
    
    markets = fetch_crypto_markets()
    if not markets:
        print(f"\n[SCAN] ❌ No crypto markets available")
        print(f"[SCAN] Scanner will retry on next cycle ({SCAN_INTERVAL}s)")
        print(f"{'='*70}\n")
        return []

    opportunities = []
    signals_processed = 0

    for market in markets:
        ticker = market.get("ticker", "UNKNOWN")
        signal = build_signal(market)
        
        if not signal:
            skip_reasons["no_signal_data"] += 1
            if len(skip_examples["no_signal_data"]) < 3:
                # Show which price fields are missing
                yes_ask = market.get("yes_ask")
                yes_bid = market.get("yes_bid")
                no_ask = market.get("no_ask")
                no_bid = market.get("no_bid")
                price_info = f"yes_ask={yes_ask} yes_bid={yes_bid} no_ask={no_ask} no_bid={no_bid}"
                skip_examples["no_signal_data"].append(f"{ticker}: {price_info}")
            continue
        
        signals_processed += 1

        # Check for ARB opportunity
        arb_edge = compute_arb_edge(signal.price_yes, signal.price_no)
        
        # Skip illiquid (unless it's an ARB opportunity)
        if signal.volume < MIN_VOLUME and arb_edge is None:
            skip_reasons["low_volume_no_arb"] += 1
            if len(skip_examples["low_volume_no_arb"]) < 3:
                skip_examples["low_volume_no_arb"].append(f"{ticker}: volume={signal.volume:.0f}")
            continue

        # Find companion for z-score
        companion = find_companion(signal.ticker, markets)

        # Run decision engine
        decision = analyze_market(
            signal=signal,
            bankroll=bankroll,
            related_signal=companion,
            inventory_imbalance=0.0,
            market_time_remaining=300.0
        )

        # Build result
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

        # Track PASS actions
        if result["action"] == "PASS":
            skip_reasons["pass_low_edge"] += 1
            if len(skip_examples["pass_low_edge"]) < 3:
                skip_examples["pass_low_edge"].append(
                    f"{ticker}: edge={decision.edge:.4f} (min={MIN_EDGE:.4f})"
                )

        opportunities.append(result)

    # Sort: ARB first, then by edge descending
    def sort_key(o):
        action_rank = {"ARB": 0, "BET_YES": 1, "BET_NO": 1, "PASS": 2}
        return (action_rank.get(o["action"], 3), -o["edge"])

    opportunities.sort(key=sort_key)

    # Count actionable opportunities
    actionable = [o for o in opportunities if o["action"] != "PASS"]
    arb_count = len([o for o in opportunities if o["action"] == "ARB"])
    bet_count = len([o for o in opportunities if "BET" in o["action"]])
    
    # Print detailed summary
    print(f"\n[SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[SCAN] PIPELINE SUMMARY:")
    print(f"[SCAN]   Crypto markets found: {len(markets)}")
    print(f"[SCAN]   Valid signals built: {signals_processed}")
    print(f"[SCAN]   Total opportunities: {len(opportunities)}")
    print(f"[SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[SCAN] SKIP REASONS:")
    print(f"[SCAN]   No signal data (missing prices): {skip_reasons['no_signal_data']}")
    for ex in skip_examples["no_signal_data"]:
        print(f"[SCAN]     • {ex}")
    print(f"[SCAN]   Low volume (no ARB): {skip_reasons['low_volume_no_arb']}")
    for ex in skip_examples["low_volume_no_arb"]:
        print(f"[SCAN]     • {ex}")
    print(f"[SCAN]   PASS (edge < {MIN_EDGE:.3f}): {skip_reasons['pass_low_edge']}")
    for ex in skip_examples["pass_low_edge"]:
        print(f"[SCAN]     • {ex}")
    print(f"[SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[SCAN] ACTIONABLE OPPORTUNITIES:")
    print(f"[SCAN]   ARB opportunities: {arb_count}")
    print(f"[SCAN]   BET opportunities: {bet_count}")
    print(f"[SCAN]   Total actionable: {len(actionable)}")
    print(f"[SCAN] ──────────────────────────────────────────────────────────────────")
    
    if actionable:
        print(f"\n[SCAN] 🎯 TOP {min(5, len(actionable))} OPPORTUNITIES:")
        for o in actionable[:5]:
            flag = "🚨 ARB" if o["action"] == "ARB" else "✅ BET"
            print(f"[SCAN]   {flag} {o['ticker']:30} | {o['action']:7} | conf={o['confidence']:.1%} | edge={o['edge']:+.4f} | ${o['bet_size']:5.0f}")
    else:
        print(f"\n[SCAN] ⚠️  NO ACTIONABLE OPPORTUNITIES FOUND")
        if skip_reasons["pass_low_edge"] > 0:
            print(f"[SCAN]   {skip_reasons['pass_low_edge']} signals had edge < {MIN_EDGE:.3f} (too low)")
        if skip_reasons["no_signal_data"] > 0:
            print(f"[SCAN]   {skip_reasons['no_signal_data']} markets had no valid price data")
        if signals_processed == 0:
            print(f"[SCAN]   No markets could be converted to valid signals")

    print(f"{'='*70}\n")
    return opportunities


def continuous_scan(interval: int = SCAN_INTERVAL, bankroll: float = BANKROLL):
    """Run continuous scan loop. Call from control_center or dashboard."""
    print(f"[SCANNER] Starting crypto scanner | interval={interval}s | bankroll=${bankroll}")
    while True:
        try:
            results = scan_crypto_markets(bankroll)
            # Save latest results for dashboard
            output_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "latest_opportunities.json"
            )
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w") as f:
                json.dump({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "bankroll": bankroll,
                    "opportunities": results
                }, f, indent=2)

        except KeyboardInterrupt:
            print("\n[SCANNER] Stopped.")
            break
        except Exception as e:
            import traceback
            print(f"[SCANNER ERROR] {e}")
            traceback.print_exc()

        time.sleep(interval)


if __name__ == "__main__":
    # Single scan test
    print("\n" + "="*70)
    print("MARKET SCANNER TEST")
    print("="*70)
    
    results = scan_crypto_markets()
    
    print(f"\n" + "="*70)
    print(f"SCAN COMPLETE - Found {len(results)} total opportunities")
    print(f"="*70 + "\n")