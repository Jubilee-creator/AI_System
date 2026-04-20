"""
AI_System — Crypto Market Scanner
Targets Kalshi BTC/ETH short-duration "Up or Down" markets.
Feeds raw market data → bayesian_engine → ranked opportunities.
"""

import os
import time
import json
import requests
import jwt
import statistics
from datetime import datetime, timezone
from typing import Optional
from cryptography.hazmat.primitives import serialization
from collections import defaultdict

# Local engine
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brain.bayesian_engine import (
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

# Crypto market series to target
CRYPTO_SERIES = [
    "KXBTCD",   # BTC daily direction
    "KXETHD",   # ETH daily direction
    "KXBTCU",   # BTC up/down short
    "KXETHU",   # ETH up/down short
    "KXBTC",    # BTC price targets
    "KXETH",    # ETH price targets
    "KXSOL",    # SOL
]

# Keywords to match in market titles
CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "crypto", "up or down", "above", "below", "price"
]

BANKROLL = float(os.getenv("BANKROLL", "500"))
MIN_VOLUME = 0          # Minimum $volume to consider
MIN_EDGE = 0.02           # 2¢ minimum net edge
SCAN_INTERVAL = 30        # Seconds between scans


# ─────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────

def _load_private_key():
    try:
        with open(PRIVATE_KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
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
        return None
    try:
        r = requests.get(KALSHI_API_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[API] {path} → {r.status_code}: {r.text[:100]}")
            return None
    except Exception as e:
        print(f"[API ERROR] {e}")
        return None


# ─────────────────────────────────────────
# MARKET FETCHER
# ─────────────────────────────────────────

def fetch_crypto_markets(limit: int = 200) -> list[dict]:
    """Fetch active Kalshi markets and filter for crypto ones."""
    data = kalshi_get(f"/markets?limit={limit}&status=open")
    if not data or "markets" not in data:
        print("[SCANNER] No markets returned")
        return []

    all_markets = data["markets"]
    crypto = []

    for m in all_markets:
        title = (m.get("title", "") + m.get("ticker", "")).lower()
        subtitle = m.get("subtitle", "").lower()
        combined = title + " " + subtitle

        # Check series prefix
        ticker = m.get("ticker", "")
        series_match = any(ticker.upper().startswith(s) for s in CRYPTO_SERIES)

        # Check keyword match
        keyword_match = any(kw in combined for kw in CRYPTO_KEYWORDS)

        if series_match or keyword_match:
            crypto.append(m)

    print(f"[SCANNER] Found {len(crypto)} crypto markets out of {len(all_markets)} total")
    return crypto


def fetch_market_orderbook(ticker: str) -> Optional[dict]:
    """Get order book for a specific market."""
    return kalshi_get(f"/markets/{ticker}/orderbook")


def parse_orderbook_imbalance(orderbook: Optional[dict]) -> float:
    """Calculate order book imbalance from -1 (all bids) to +1 (all asks)."""
    if not orderbook:
        return 0.0
    try:
        yes_book = orderbook.get("orderbook", {})
        asks = sum(e.get("delta", 0) for e in yes_book.get("yes", []))
        bids = sum(e.get("delta", 0) for e in yes_book.get("no", []))
        total = asks + bids
        if total == 0:
            return 0.0
        return (asks - bids) / total
    except Exception:
        return 0.0


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
        yes_ask = market.get("yes_ask", 0) / 100.0   # Kalshi returns cents
        yes_bid = market.get("yes_bid", 0) / 100.0
        no_ask = market.get("no_ask", 0) / 100.0
        no_bid = market.get("no_bid", 0) / 100.0

        # Mid prices
        yes_mid = (yes_ask + yes_bid) / 2 if (yes_ask + yes_bid) > 0 else yes_ask
        no_mid = (no_ask + no_bid) / 2 if (no_ask + no_bid) > 0 else no_ask

        # Skip if no pricing
        if yes_mid == 0 and no_mid == 0:
            return None

        # If only one side priced, infer the other
        if yes_mid == 0:
            yes_mid = 1.0 - no_mid
        if no_mid == 0:
            no_mid = 1.0 - yes_mid

        # Volume
        volume = float(market.get("volume", 0) or market.get("volume_24h", 0) or 0)
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

        # OBI from orderbook (optional, adds latency)
        obi = 0.0

        return MarketSignal(
            ticker=ticker,
            price_yes=yes_mid,
            price_no=no_mid,
            volume=volume * vol_spike,  # Amplify volume spike signal
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
    print(f"\n[SCAN] {datetime.now().strftime('%H:%M:%S')} — Scanning crypto markets...")

    markets = fetch_crypto_markets()
    if not markets:
        return []

    opportunities = []

    for market in markets:
        signal = build_signal(market)
        if not signal:
            continue

        # Skip illiquid
        if signal.volume < MIN_VOLUME and compute_arb_edge(signal.price_yes, signal.price_no) is None:
            continue

        # Find companion for z-score
        companion = find_companion(signal.ticker, markets)

        # Run engine
        decision = analyze_market(
            signal=signal,
            bankroll=bankroll,
            related_signal=companion,
            market_time_remaining=300.0
        )

        # Filter weak signals
        if decision.action == "PASS" and abs(decision.edge) < MIN_EDGE:
            continue

        # Build result
        result = {
            "ticker": signal.ticker,
            "title": market.get("title", ticker)[:50],
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

        opportunities.append(result)

    # Sort: ARB first, then by edge descending
    def sort_key(o):
        action_rank = {"ARB": 0, "BET_YES": 1, "BET_NO": 1, "PASS": 2}
        return (action_rank.get(o["action"], 3), -o["edge"])

    opportunities.sort(key=sort_key)

    print(f"[SCAN] {len(opportunities)} opportunities found")
    for o in opportunities[:5]:
        flag = "🚨 ARB" if o["action"] == "ARB" else ("✅ BET" if "BET" in o["action"] else "⬜ PASS")
        print(f"  {flag} {o['ticker']} | {o['action']} | conf={o['confidence']:.0%} | edge={o['edge']:.4f} | ${o['bet_size']}")

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
            print(f"[SCANNER ERROR] {e}")

        time.sleep(interval)


if __name__ == "__main__":
    # Single scan test
    results = scan_crypto_markets()
    print(f"\nTop opportunities:")
    for r in results[:3]:
        print(json.dumps(r, indent=2))