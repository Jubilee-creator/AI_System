"""
brain/market_scanner.py
-----------------------
AI_System — Crypto Market Scanner
Targets Kalshi BTC/ETH/SOL crypto markets.
Feeds raw market data → decision_engine → ranked opportunities.

PRODUCTION VERSION: Series-based discovery with correct quote parsing
"""

import os
import time
import json
import requests
import statistics
import re
from datetime import datetime, timezone
from typing import Optional, List
from cryptography.hazmat.primitives import serialization
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Local engine
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.decision_engine import (
    MarketSignal, TradeDecision, analyze_market, compute_arb_edge
)
from config.trading_config import MAX_SPREAD


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.getenv(
    "KALSHI_PRIVATE_KEY_PATH",
    "/Users/samuel/Desktop/AI_System/kalshi_private_key.pem"
)

CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "crypto", "xrp", "ripple", "cardano", "ada", "dogecoin",
    "doge", "polygon", "matic", "hype", "hyperliquid", "stellar",
    "xlm", "litecoin", "ltc", "chainlink", "link", "avalanche", "avax"
]

BANKROLL = float(os.getenv("BANKROLL", "500"))
MIN_VOLUME = 0
MIN_EDGE = 0.015
SCAN_INTERVAL = 30


# ─────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────

def _load_private_key():
    try:
        with open(PRIVATE_KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
        return None


def _build_auth_header(method: str, path: str) -> dict:
    private_key = _load_private_key()
    if not private_key:
        return {}
    
    timestamp = str(int(time.time() * 1000))
    msg = timestamp + method.upper() + path
    
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    import base64
    
    signature = private_key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode()
    
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "Content-Type": "application/json"
    }


def kalshi_get(path: str, silent: bool = False) -> Optional[dict]:
    headers = _build_auth_header("GET", path)
    if not headers:
        return None
    try:
        r = requests.get(KALSHI_API_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        elif not silent:
            print(f"[API] {path} → {r.status_code}")
        return None
    except Exception as e:
        if not silent:
            print(f"[API ERROR] {e}")
        return None


# ─────────────────────────────────────────
# SERIES DISCOVERY
# ─────────────────────────────────────────

def discover_crypto_series() -> List[str]:
    """Discover crypto series via /series endpoint."""
    print(f"[SCANNER] Discovering crypto series...")
    
    data = kalshi_get("/series?limit=1000")
    if not data or "series" not in data:
        print(f"[SCANNER] ❌ Failed to fetch series")
        return []
    
    all_series = data["series"]
    print(f"[SCANNER] ✓ Fetched {len(all_series)} total series")
    
    crypto_series = []
    
    for s in all_series:
        ticker = s.get("ticker", "").lower()
        title = s.get("title", "").lower()
        combined = f"{ticker} {title}"
        
        for keyword in CRYPTO_KEYWORDS:
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, combined, re.IGNORECASE):
                crypto_series.append(s.get("ticker", ""))
                break
    
    print(f"[SCANNER] ✓ Found {len(crypto_series)} crypto series")
    
    return crypto_series


# ─────────────────────────────────────────
# MARKET FETCHING
# ─────────────────────────────────────────

def fetch_markets_for_series(series_ticker: str) -> List[dict]:
    """Fetch open markets for specific series."""
    path = f"/markets?series_ticker={series_ticker}&status=open&limit=200"
    data = kalshi_get(path, silent=True)
    
    if not data or "markets" not in data:
        return []
    
    return data["markets"]


# ─────────────────────────────────────────
# QUOTE ENRICHMENT
# ─────────────────────────────────────────

def enrich_market_with_quotes(market: dict) -> dict:
    """
    Enrich market with quote data.
    
    PRIMARY: Market detail endpoint provides:
    - yes_bid_dollars (string like "0.2000")
    - yes_ask_dollars (string like "0.2400")
    - no_bid_dollars (string like "0.7600")
    - no_ask_dollars (string like "0.8000")
    Already in probability units (0.00-1.00), NOT cents.
    
    FALLBACK: Orderbook if detail missing quotes:
    - Extract highest bid from yes_dollars and no_dollars arrays
    - Derive asks: yes_ask = 1 - no_bid, no_ask = 1 - yes_bid
    """
    ticker = market.get("ticker", "")
    enriched = dict(market)
    
    # Try market detail (PRIMARY)
    detail = kalshi_get(f"/markets/{ticker}", silent=True)
    if detail and "market" in detail:
        market_data = detail["market"]
        
        # Extract quote fields (already in probability units)
        yes_bid_str = market_data.get("yes_bid_dollars")
        yes_ask_str = market_data.get("yes_ask_dollars")
        no_bid_str = market_data.get("no_bid_dollars")
        no_ask_str = market_data.get("no_ask_dollars")
        
        # Convert to floats if present
        if yes_bid_str is not None:
            enriched["yes_bid"] = float(yes_bid_str)
        if yes_ask_str is not None:
            enriched["yes_ask"] = float(yes_ask_str)
        if no_bid_str is not None:
            enriched["no_bid"] = float(no_bid_str)
        if no_ask_str is not None:
            enriched["no_ask"] = float(no_ask_str)
        
        # Extract volume fields
        volume_fp = market_data.get("volume_fp")
        volume_24h_fp = market_data.get("volume_24h_fp")
        open_interest_fp = market_data.get("open_interest_fp")
        
        if volume_fp is not None:
            enriched["volume"] = float(volume_fp)
        elif volume_24h_fp is not None:
            enriched["volume"] = float(volume_24h_fp)
        elif open_interest_fp is not None:
            enriched["volume"] = float(open_interest_fp)
    
    # Check if we have quotes now
    has_quotes = any(enriched.get(f) is not None for f in ["yes_ask", "yes_bid", "no_ask", "no_bid"])
    
    # FALLBACK: Try orderbook if still missing quotes
    if not has_quotes:
        orderbook = kalshi_get(f"/markets/{ticker}/orderbook", silent=True)
        if orderbook and "orderbook_fp" in orderbook:
            ob_fp = orderbook["orderbook_fp"]
            
            yes_levels = ob_fp.get("yes_dollars", [])
            no_levels = ob_fp.get("no_dollars", [])
            
            yes_bid = None
            no_bid = None
            
            # Extract highest bids (first element in arrays)
            if yes_levels and isinstance(yes_levels, list) and len(yes_levels) > 0:
                if isinstance(yes_levels[0], list) and len(yes_levels[0]) > 0:
                    yes_bid = float(yes_levels[0][0])
            
            if no_levels and isinstance(no_levels, list) and len(no_levels) > 0:
                if isinstance(no_levels[0], list) and len(no_levels[0]) > 0:
                    no_bid = float(no_levels[0][0])
            
            # Derive asks from bids
            if yes_bid is not None and no_bid is not None:
                enriched["yes_bid"] = yes_bid
                enriched["no_bid"] = no_bid
                enriched["yes_ask"] = 1.0 - no_bid
                enriched["no_ask"] = 1.0 - yes_bid
            elif yes_bid is not None:
                enriched["yes_bid"] = yes_bid
                enriched["yes_ask"] = min(1.0, yes_bid + 0.01)
                enriched["no_ask"] = 1.0 - yes_bid
                enriched["no_bid"] = max(0.0, 1.0 - enriched["yes_ask"])
            elif no_bid is not None:
                enriched["no_bid"] = no_bid
                enriched["no_ask"] = min(1.0, no_bid + 0.01)
                enriched["yes_ask"] = 1.0 - no_bid
                enriched["yes_bid"] = max(0.0, 1.0 - enriched["no_ask"])
    
    return enriched


_QUOTE_FIELDS = ("yes_ask", "yes_bid", "no_ask", "no_bid")

# Conservative worker count: enough to keep all TCP connections busy without
# hammering Kalshi's rate limits. Each series fetch is one GET request (~250ms);
# 15 workers turns ~45s of sequential fetches into ~3-4s.
_SERIES_FETCH_WORKERS = 15


def fetch_and_enrich_crypto_markets() -> List[dict]:
    """Main market fetching pipeline with parallel series fetching."""
    t0_total = time.time()

    # ── Step 1: Discover series ───────────────────────────────────
    t0_series = time.time()
    crypto_series = discover_crypto_series()
    t_series = time.time() - t0_series

    if not crypto_series:
        return []

    # ── Step 2: Fetch markets per series — parallel ───────────────
    # All requests still go through kalshi_get() with the same auth and
    # timeout. No rate limiter is bypassed; workers are limited to
    # _SERIES_FETCH_WORKERS concurrent connections.
    print(f"\n[SCANNER] Fetching markets for {len(crypto_series)} crypto series "
          f"(parallel, workers={_SERIES_FETCH_WORKERS})...")

    t0_markets = time.time()
    all_markets: List[dict] = []
    series_success = 0
    series_empty   = 0

    with ThreadPoolExecutor(max_workers=_SERIES_FETCH_WORKERS) as executor:
        futures = {executor.submit(fetch_markets_for_series, s): s
                   for s in crypto_series}
        for fut in as_completed(futures):
            try:
                markets = fut.result()
            except Exception as exc:
                print(f"[SCANNER] WARN: {futures[fut]} fetch error: {exc}")
                markets = []
            if markets:
                all_markets.extend(markets)
                series_success += 1
            else:
                series_empty += 1

    t_markets = time.time() - t0_markets

    print(f"[SCANNER] Market fetch:")
    print(f"[SCANNER]   series_requested: {len(crypto_series)}")
    print(f"[SCANNER]   series_success:   {series_success}")
    print(f"[SCANNER]   series_empty:     {series_empty}")
    print(f"[SCANNER]   markets_found:    {len(all_markets)}")
    print(f"[SCANNER] ✓ Found {len(all_markets)} total crypto markets")

    if not all_markets:
        return []

    # ── Step 3: Enrich with quotes ────────────────────────────────
    print(f"\n[SCANNER] Enriching {len(all_markets)} markets with quotes...")
    t0_enrich = time.time()

    enriched:  List[dict] = []
    enriched_count   = 0
    list_only        = 0   # quotes already in the list-endpoint response
    detail_fetched   = 0   # quotes added by the /markets/{ticker} detail call
    orderbook_fetched = 0  # quotes added by the orderbook fallback
    failed_examples: List[str] = []

    for market in all_markets:
        ticker = market.get("ticker", "")

        # Record which quote fields the list endpoint already provided
        pre_quotes = any(market.get(f) is not None for f in _QUOTE_FIELDS)

        enriched_market = enrich_market_with_quotes(market)

        has_quotes = any(enriched_market.get(f) is not None for f in _QUOTE_FIELDS)

        if has_quotes:
            enriched.append(enriched_market)
            enriched_count += 1
            if pre_quotes:
                list_only += 1
            else:
                detail_fetched += 1   # came from detail or orderbook call
        else:
            if len(failed_examples) < 5:
                failed_examples.append(ticker)

    t_enrich = time.time() - t0_enrich
    t_total  = time.time() - t0_total

    print(f"[SCANNER] ✓ Enriched: {enriched_count}/{len(all_markets)} have quotes")
    print(f"[SCANNER] Quote src: list-only={list_only}, "
          f"detail-fetched={detail_fetched}, orderbook-fetched={orderbook_fetched}")

    if failed_examples:
        print(f"[SCANNER] Missing quotes: {', '.join(failed_examples)}")

    print(f"\n[SCANNER] Timing:")
    print(f"[SCANNER]   series_fetch_time:  {t_series:.1f}s")
    print(f"[SCANNER]   markets_fetch_time: {t_markets:.1f}s")
    print(f"[SCANNER]   enrichment_time:    {t_enrich:.1f}s")
    print(f"[SCANNER]   total_scan_time:    {t_total:.1f}s")

    return enriched


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
_price_history: dict[str, list] = defaultdict(list)


# ─────────────────────────────────────────
# SIGNAL BUILDER
# ─────────────────────────────────────────

def build_signal(market: dict) -> Optional[MarketSignal]:
    """
    Convert enriched market dict → MarketSignal.

    price_yes = yes_ask  (actual entry price for BET_YES — what you pay)
    price_no  = no_ask   (actual entry price for BET_NO  — what you pay)

    Mid prices are retained only for price-history, price_change, and
    volatility tracking so those signals remain centered on the market.
    """
    try:
        ticker = market.get("ticker", "")

        # Extract quotes (already floats in 0-1 range)
        yes_ask = market.get("yes_ask")
        yes_bid = market.get("yes_bid")
        no_ask  = market.get("no_ask")
        no_bid  = market.get("no_bid")

        # ── Mid prices: used ONLY for history / price_change / volatility ──
        yes_mid = None
        no_mid  = None

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

        if yes_mid is None and no_mid is None:
            return None

        # Infer missing mid from complement
        if yes_mid is None:
            yes_mid = 1.0 - no_mid
        if no_mid is None:
            no_mid = 1.0 - yes_mid

        # ── Execution prices: ASK (what you actually pay to enter) ──
        # BET_YES → buy YES contracts at yes_ask
        # BET_NO  → buy NO  contracts at no_ask
        price_yes = yes_ask if yes_ask is not None else yes_mid
        price_no  = no_ask  if no_ask  is not None else no_mid

        # Infer missing ask from complement of the other ask
        if yes_ask is None and no_ask is not None:
            price_yes = 1.0 - no_ask
        if no_ask is None and yes_ask is not None:
            price_no = 1.0 - yes_ask

        # Volume
        volume    = float(market.get("volume", 0.0))
        vol_spike = _volume_tracker.update(ticker, volume)

        # Price history uses mid (tracks market center, not cost-to-enter)
        price_hist = _price_history[ticker]
        price_hist.append(yes_mid)
        if len(price_hist) > 20:
            price_hist.pop(0)

        price_change = 0.0
        if len(price_hist) >= 3:
            price_change = price_hist[-1] - price_hist[-3]

        volatility = 0.03
        if len(price_hist) >= 5:
            try:
                volatility = statistics.stdev(price_hist[-5:])
            except:
                volatility = 0.03

        return MarketSignal(
            ticker=ticker,
            price_yes=price_yes,          # ASK price — actual cost for BET_YES
            price_no=price_no,            # ASK price — actual cost for BET_NO
            volume=volume * vol_spike,
            price_change=price_change,
            volatility=max(0.001, volatility),
            order_book_imbalance=0.0
        )

    except Exception as e:
        print(f"[BUILD_SIGNAL] Error on {market.get('ticker', '?')}: {e}")
        return None


def find_companion(ticker: str, all_markets: List[dict]) -> Optional[MarketSignal]:
    """Find related market for cross-market z-score."""
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
    """Full scan pipeline: discover → fetch → enrich → analyze → rank."""
    print(f"\n{'='*70}")
    print(f"[SCAN] {datetime.now().strftime('%H:%M:%S')} — Crypto market scan")
    print(f"{'='*70}")
    
    skip_reasons = {"no_signal": 0, "low_volume": 0, "wide_spread": 0, "pass": 0}
    skip_examples = {"no_signal": [], "wide_spread": [], "pass": []}
    
    markets = fetch_and_enrich_crypto_markets()
    
    if not markets:
        print(f"\n[SCAN] ❌ No enriched crypto markets available")
        print(f"{'='*70}\n")
        return []
    
    opportunities = []
    signals_built = 0
    
    for market in markets:
        ticker = market.get("ticker", "?")
        signal = build_signal(market)
        
        if not signal:
            skip_reasons["no_signal"] += 1
            if len(skip_examples["no_signal"]) < 3:
                skip_examples["no_signal"].append(ticker)
            continue
        
        signals_built += 1
        
        arb_edge = compute_arb_edge(signal.price_yes, signal.price_no)
        if signal.volume < MIN_VOLUME and arb_edge is None:
            skip_reasons["low_volume"] += 1
            continue

        # Phase 2: Pre-filter wide-spread markets before ranking.
        # ARB opportunities bypass this check (spread is irrelevant when buying both sides).
        if arb_edge is None:
            yes_ask = market.get("yes_ask")
            yes_bid = market.get("yes_bid")
            no_ask  = market.get("no_ask")
            no_bid  = market.get("no_bid")
            yes_spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None
            no_spread  = (no_ask  - no_bid)  if (no_ask  is not None and no_bid  is not None) else None
            spreads = [s for s in [yes_spread, no_spread] if s is not None]
            if spreads and min(spreads) > MAX_SPREAD:
                skip_reasons["wide_spread"] += 1
                if len(skip_examples["wide_spread"]) < 3:
                    skip_examples["wide_spread"].append(
                        f"{ticker}: spread={min(spreads):.4f} (max={MAX_SPREAD:.2f})"
                    )
                continue

        companion = find_companion(signal.ticker, markets)
        
        decision = analyze_market(
            signal=signal,
            bankroll=bankroll,
            related_signal=companion,
            inventory_imbalance=0.0,
            market_time_remaining=300.0
        )
        
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
            "result_time": market.get("expected_expiration_time", ""),
            # Raw quote fields — carried through for execution-quality filtering
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "no_bid":  market.get("no_bid"),
            "no_ask":  market.get("no_ask"),
        }
        
        if result["action"] == "PASS":
            skip_reasons["pass"] += 1
            if len(skip_examples["pass"]) < 3:
                skip_examples["pass"].append(f"{ticker}: edge={decision.edge:.4f}")
        
        opportunities.append(result)
    
    # Sort: ARB first, then by edge descending
    def sort_key(o):
        rank = {"ARB": 0, "BET_YES": 1, "BET_NO": 1, "PASS": 2}
        return (rank.get(o["action"], 3), -o["edge"])
    
    opportunities.sort(key=sort_key)
    
    actionable = [o for o in opportunities if o["action"] != "PASS"]
    arb = len([o for o in opportunities if o["action"] == "ARB"])
    bet = len([o for o in opportunities if "BET" in o["action"]])
    
    print(f"\n[SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[SCAN] RESULTS:")
    print(f"[SCAN]   Markets with quotes: {len(markets)}")
    print(f"[SCAN]   Valid signals built: {signals_built}")
    print(f"[SCAN]   Total opportunities: {len(opportunities)}")
    print(f"[SCAN]   Actionable: {len(actionable)} (ARB: {arb}, BET: {bet})")
    print(f"[SCAN] ──────────────────────────────────────────────────────────────────")
    print(f"[SCAN] SKIPS:")
    print(f"[SCAN]   No signal: {skip_reasons['no_signal']}")
    for ex in skip_examples["no_signal"]:
        print(f"[SCAN]     • {ex}")
    print(f"[SCAN]   Low volume (no ARB): {skip_reasons['low_volume']}")
    print(f"[SCAN]   Wide spread (>{MAX_SPREAD:.2f}): {skip_reasons['wide_spread']}")
    for ex in skip_examples["wide_spread"]:
        print(f"[SCAN]     • {ex}")
    print(f"[SCAN]   PASS (edge < {MIN_EDGE:.3f}): {skip_reasons['pass']}")
    for ex in skip_examples["pass"]:
        print(f"[SCAN]     • {ex}")
    print(f"[SCAN] ──────────────────────────────────────────────────────────────────")
    
    if actionable:
        print(f"\n[SCAN] 🎯 TOP OPPORTUNITIES:")
        for o in actionable[:5]:
            flag = "🚨 ARB" if o["action"] == "ARB" else "✅ BET"
            print(f"[SCAN]   {flag} {o['ticker']:30} | {o['action']:7} | conf={o['confidence']:.1%} | edge={o['edge']:+.4f} | ${o['bet_size']:5.0f}")
    else:
        print(f"\n[SCAN] ⚠️  NO ACTIONABLE OPPORTUNITIES")
        if skip_reasons["pass"] > 0:
            print(f"[SCAN]   ({skip_reasons['pass']} had edge < {MIN_EDGE:.3f})")
    
    print(f"{'='*70}\n")
    return opportunities


def continuous_scan(interval: int = SCAN_INTERVAL, bankroll: float = BANKROLL):
    """Run continuous scan loop."""
    print(f"[SCANNER] Starting | interval={interval}s | bankroll=${bankroll}")
    while True:
        try:
            results = scan_crypto_markets(bankroll)
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
    print("\n" + "="*70)
    print("MARKET SCANNER")
    print("="*70)
    results = scan_crypto_markets()
    print(f"\n" + "="*70)
    print(f"COMPLETE - {len(results)} opportunities")
    actionable = len([r for r in results if r["action"] != "PASS"])
    print(f"Actionable: {actionable}")
    print(f"="*70 + "\n")