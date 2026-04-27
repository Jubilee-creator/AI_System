"""
brain/market_scanner.py
-----------------------
AI_System — Crypto Market Scanner
Targets Kalshi BTC/ETH/SOL crypto markets.
Feeds raw market data → decision_engine → ranked opportunities.

PRODUCTION VERSION: Series-based discovery with correct quote parsing
M-7: Rate-limited via brokers.kalshi_client; parallel market enrichment.
"""

import os
import time
import json
import statistics
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional, List
from collections import defaultdict

# Local engine
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.decision_engine import (
    MarketSignal, TradeDecision, analyze_market, compute_arb_edge
)
from brokers.kalshi_client import kalshi_get, get_client


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

_QUOTE_FIELDS = ["yes_bid", "yes_ask", "no_bid", "no_ask"]

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
MAX_ENRICH_WORKERS = 15  # parallel threads for market detail calls



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

def _extract_quotes_from_dict(d: dict) -> dict:
    """
    Extract quote + volume from any Kalshi market dict, regardless of format.

    The Kalshi list endpoint returns integer cents:
        yes_bid = 25   →  0.25 probability
    The detail endpoint returns string dollars:
        yes_bid_dollars = "0.2500"  →  0.25 probability
    Internal dicts may already hold floats in [0, 1].

    After extracting whatever is present, derives any missing sides using
    the Kalshi identity:  yes + no = 1.00  (complementary contracts).
    Returns only fields that are present and valid; missing fields are absent.
    """
    def to_prob(val) -> Optional[float]:
        if val is None:
            return None
        # JSON integer from list endpoint → cents (1 = 1¢ = 0.01, not 100%)
        if isinstance(val, int):
            if 0 <= val <= 100:
                return round(val / 100.0, 6)
            return None
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        if 0.0 <= v <= 1.0:
            return round(v, 6)
        if 1.0 < v <= 100.0:   # float-cents edge case
            return round(v / 100.0, 6)
        return None

    out: dict = {}

    # Priority 1: string-dollar fields (detail endpoint format)
    for src, dst in [
        ("yes_bid_dollars", "yes_bid"),
        ("yes_ask_dollars", "yes_ask"),
        ("no_bid_dollars",  "no_bid"),
        ("no_ask_dollars",  "no_ask"),
    ]:
        v = to_prob(d.get(src))
        if v is not None:
            out[dst] = v

    # Priority 2: direct bid/ask fields (list endpoint integer-cents)
    for field in _QUOTE_FIELDS:
        if field not in out:
            v = to_prob(d.get(field))
            if v is not None:
                out[field] = v

    # Derive missing sides: yes + no = 1.00 (Kalshi complementary identity)
    if "yes_bid" in out and "no_ask" not in out:
        out["no_ask"] = round(1.0 - out["yes_bid"], 6)
    if "yes_ask" in out and "no_bid" not in out:
        out["no_bid"] = round(1.0 - out["yes_ask"], 6)
    if "no_bid" in out and "yes_ask" not in out:
        out["yes_ask"] = round(1.0 - out["no_bid"], 6)
    if "no_ask" in out and "yes_bid" not in out:
        out["yes_bid"] = round(1.0 - out["no_ask"], 6)

    # Volume: prefer dollar-volume from detail, fall back to contract count from list
    for f in ("volume_fp", "volume_24h_fp", "open_interest_fp", "volume", "open_interest"):
        raw = d.get(f)
        if raw is not None:
            try:
                out["volume"] = float(raw)
                break
            except (TypeError, ValueError):
                pass

    return out


def _has_full_quotes(d: dict) -> bool:
    """True if all four bid/ask fields are present (not None)."""
    return all(d.get(f) is not None for f in _QUOTE_FIELDS)


def enrich_market_with_quotes(market: dict) -> tuple[dict, int]:
    """
    Enrich market with quote data using a 3-tier waterfall.

    Tier 0 (no extra API call):  list-response data already contains quotes.
    Tier 1 (1 API call):         GET /markets/{ticker} fills missing fields.
    Tier 2 (1 API call):         GET /markets/{ticker}/orderbook last resort.

    Returns (enriched_market, tier_used) where tier_used is 0, 1, or 2.
    Signal generation, edge logic, and filters are untouched downstream.
    """
    ticker = market.get("ticker", "")
    enriched = dict(market)

    # ── Tier 0: list-response data ────────────────────────────────
    list_quotes = _extract_quotes_from_dict(market)
    enriched.update(list_quotes)

    if _has_full_quotes(enriched) and enriched.get("volume") is not None:
        return enriched, 0

    # ── Tier 1: market detail endpoint ───────────────────────────
    detail = kalshi_get(f"/markets/{ticker}", silent=True)
    if detail and "market" in detail:
        detail_quotes = _extract_quotes_from_dict(detail["market"])
        for k, v in detail_quotes.items():
            if enriched.get(k) is None:  # never overwrite good list data
                enriched[k] = v

    if _has_full_quotes(enriched):
        return enriched, 1

    # ── Tier 2: orderbook fallback ────────────────────────────────
    orderbook = kalshi_get(f"/markets/{ticker}/orderbook", silent=True)
    if orderbook and "orderbook_fp" in orderbook:
        ob_fp = orderbook["orderbook_fp"]
        yes_levels = ob_fp.get("yes_dollars", [])
        no_levels  = ob_fp.get("no_dollars",  [])

        yes_bid = no_bid = None

        if yes_levels and isinstance(yes_levels, list) and len(yes_levels) > 0:
            if isinstance(yes_levels[0], list) and len(yes_levels[0]) > 0:
                yes_bid = float(yes_levels[0][0])

        if no_levels and isinstance(no_levels, list) and len(no_levels) > 0:
            if isinstance(no_levels[0], list) and len(no_levels[0]) > 0:
                no_bid = float(no_levels[0][0])

        if yes_bid is not None and no_bid is not None:
            enriched["yes_bid"] = yes_bid
            enriched["no_bid"]  = no_bid
            enriched["yes_ask"] = 1.0 - no_bid
            enriched["no_ask"]  = 1.0 - yes_bid
        elif yes_bid is not None:
            enriched["yes_bid"] = yes_bid
            enriched["yes_ask"] = min(1.0, yes_bid + 0.01)
            enriched["no_ask"]  = 1.0 - yes_bid
            enriched["no_bid"]  = max(0.0, 1.0 - enriched["yes_ask"])
        elif no_bid is not None:
            enriched["no_bid"]  = no_bid
            enriched["no_ask"]  = min(1.0, no_bid + 0.01)
            enriched["yes_ask"] = 1.0 - no_bid
            enriched["yes_bid"] = max(0.0, 1.0 - enriched["no_ask"])

    return enriched, 2


def fetch_and_enrich_crypto_markets() -> List[dict]:
    """Main market fetching pipeline — rate-limited, parallel enrichment."""
    client = get_client()
    client.start_scan()
    t0 = time.monotonic()

    # Step 1: Discover series
    crypto_series = discover_crypto_series()
    t1 = time.monotonic()

    if not crypto_series:
        return []

    # Step 2: Fetch markets per series (N series calls, sequential)
    print(f"\n[SCANNER] Fetching markets for {len(crypto_series)} crypto series...")

    all_markets = []
    for series in crypto_series:
        markets = fetch_markets_for_series(series)
        if markets:
            all_markets.extend(markets)

    t2 = time.monotonic()
    print(f"[SCANNER] ✓ Found {len(all_markets)} total crypto markets")

    if not all_markets:
        return []

    # Step 3: Parallel enrichment — 3-tier waterfall, rate-limited
    print(f"\n[SCANNER] Enriching {len(all_markets)} markets"
          f" ({MAX_ENRICH_WORKERS} workers, rate-limited)...")

    enriched_list   = []
    failed_examples = []
    tier_counts     = [0, 0, 0]   # [list-only, detail-called, orderbook-called]

    with ThreadPoolExecutor(max_workers=MAX_ENRICH_WORKERS) as pool:
        futures = {pool.submit(enrich_market_with_quotes, m): m for m in all_markets}
        for future in as_completed(futures):
            enriched_market, tier = future.result()
            tier_counts[min(tier, 2)] += 1
            has_quotes = any(
                enriched_market.get(f) is not None for f in _QUOTE_FIELDS
            )
            if has_quotes:
                enriched_list.append(enriched_market)
            elif len(failed_examples) < 5:
                failed_examples.append(enriched_market.get("ticker", "?"))

    t3 = time.monotonic()
    stats = client.scan_stats()
    enriched_count = len(enriched_list)

    print(f"[SCANNER] ✓ Enriched: {enriched_count}/{len(all_markets)} have quotes")
    print(
        f"[SCANNER] Quote src : "
        f"list-only={tier_counts[0]} | detail-fetched={tier_counts[1]}"
        f" | orderbook-fetched={tier_counts[2]}"
        f"  (detail skipped={tier_counts[0]}/{len(all_markets)})"
    )
    print(
        f"[SCANNER] Timing   : series={t1-t0:.1f}s | "
        f"markets={t2-t1:.1f}s | enrich={t3-t2:.1f}s | total={t3-t0:.1f}s"
    )
    print(
        f"[SCANNER] API stats: {stats['requests']} requests @ {stats['rate_rps']} req/s"
        f" | throttled={stats['throttled']} | retried={stats['retried']}"
        f" | wait={stats['wait_ms_total']:.0f}ms"
    )

    if failed_examples:
        print(f"[SCANNER] No-quote markets: {', '.join(failed_examples)}")

    return enriched_list


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
    Quote fields are already floats in 0.0-1.0 probability units.
    """
    try:
        ticker = market.get("ticker", "")
        
        # Extract quotes (already floats in 0-1 range)
        yes_ask = market.get("yes_ask")
        yes_bid = market.get("yes_bid")
        no_ask = market.get("no_ask")
        no_bid = market.get("no_bid")
        
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
        
        if yes_mid is None and no_mid is None:
            return None
        
        # Infer missing side
        if yes_mid is None and no_mid is not None:
            yes_mid = 1.0 - no_mid
        if no_mid is None and yes_mid is not None:
            no_mid = 1.0 - yes_mid
        
        if yes_mid is None or no_mid is None:
            return None
        
        # Volume
        volume = market.get("volume", 0.0)
        volume = float(volume)
        vol_spike = _volume_tracker.update(ticker, volume)
        
        # Price history
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
            except:
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
    
    skip_reasons = {"no_signal": 0, "low_volume": 0, "pass": 0}
    skip_examples = {"no_signal": [], "pass": []}
    
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
            "result_time": market.get("expected_expiration_time", "")
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
    """Run continuous scan loop — sleeps only the time remaining after each scan."""
    print(f"[SCANNER] Starting | min_interval={interval}s | bankroll=${bankroll}")
    while True:
        t_start = time.monotonic()
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
            continue

        elapsed = time.monotonic() - t_start
        sleep_s = max(0.0, interval - elapsed)
        if sleep_s > 0:
            print(f"[SCANNER] Next scan in {sleep_s:.0f}s")
            time.sleep(sleep_s)
        else:
            print(f"[SCANNER] Scan took {elapsed:.1f}s (>{interval}s interval) — restarting immediately")


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