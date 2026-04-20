"""
edge_calculator.py — The Truth Machine
Does this trade make money? Math says yes or no.
"""

import math
from dataclasses import dataclass
from typing import Optional

@dataclass
class MarketData:
    """Raw market data (normalized across venues)"""
    ticker: str
    yes_price: float
    no_price: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume_24h: int
    spread: float
    liquidity: int
    fee_rate: float
    time_to_expiry: float
    venue: str


@dataclass
class EdgeAnalysis:
    """The ONLY output that matters"""
    ticker: str
    has_edge: bool
    estimated_true_prob: float
    market_implied_prob: float
    raw_edge: float
    fee_cost: float
    slippage_cost: float
    total_cost: float
    net_edge: float
    expected_value: float
    confidence: float
    liquidity_score: float
    closing_line_value: float
    reason: str
    min_edge_threshold: float = 0.03


def calculate_expected_value(
    true_prob: float,
    entry_price: float,
    fee_rate: float,
    slippage: float = 0.0
) -> float:
    """EV = (win_prob × profit) - (lose_prob × loss) - fees - slippage"""
    win_prob = true_prob
    lose_prob = 1.0 - true_prob
    
    profit_if_win = (1.0 - entry_price)
    loss_if_lose = entry_price
    
    expected_profit = (win_prob * profit_if_win) - (lose_prob * loss_if_lose)
    total_costs = fee_rate + slippage
    
    ev = expected_profit - total_costs
    return ev


def estimate_slippage(spread: float, liquidity: int, order_size: int = 100) -> float:
    """Estimate slippage from spread and liquidity"""
    base_slippage = spread / 2
    
    if liquidity == 0:
        return 0.05
    
    size_ratio = order_size / liquidity
    
    if size_ratio > 1.0:
        liquidity_penalty = 0.02 * (size_ratio - 1.0)
    else:
        liquidity_penalty = 0.0
    
    total_slippage = base_slippage + liquidity_penalty
    return min(total_slippage, 0.05)


def calculate_confidence(
    market_data: MarketData,
    edge: float,
    data_quality: float = 0.8
) -> float:
    """How confident are we in this edge?"""
    edge_conf = min(abs(edge) / 0.10, 1.0)
    
    if market_data.volume_24h > 10000:
        volume_conf = 0.6
    elif market_data.volume_24h > 1000:
        volume_conf = 0.8
    elif market_data.volume_24h > 100:
        volume_conf = 0.7
    else:
        volume_conf = 0.4
    
    if market_data.time_to_expiry < 1:
        time_conf = 0.9
    elif market_data.time_to_expiry < 24:
        time_conf = 0.7
    else:
        time_conf = 0.5
    
    confidence = (
        edge_conf * 0.4 +
        volume_conf * 0.3 +
        time_conf * 0.2 +
        data_quality * 0.1
    )
    
    return min(confidence, 1.0)


def analyze_edge(
    market_data: MarketData,
    estimated_true_prob: float,
    min_edge_threshold: float = 0.03,
    min_liquidity: int = 50,
    max_spread: float = 0.05,
    order_size: int = 100
) -> EdgeAnalysis:
    """
    The MAIN function. Returns: should you trade or not + why.
    """
    market_implied = market_data.yes_price
    raw_edge = estimated_true_prob - market_implied
    
    slippage = estimate_slippage(market_data.spread, market_data.liquidity, order_size)
    fee_cost = market_data.fee_rate
    total_cost = fee_cost + slippage
    net_edge = raw_edge - total_cost
    
    ev = calculate_expected_value(
        true_prob=estimated_true_prob,
        entry_price=market_data.yes_price,
        fee_rate=fee_cost,
        slippage=slippage
    )
    
    confidence = calculate_confidence(market_data, net_edge)
    
    if market_data.liquidity >= 500:
        liquidity_score = 1.0
    elif market_data.liquidity >= 100:
        liquidity_score = 0.8
    elif market_data.liquidity >= min_liquidity:
        liquidity_score = 0.5
    else:
        liquidity_score = 0.2
    
    reasons = []
    has_edge = True
    
    if market_data.liquidity < min_liquidity:
        has_edge = False
        reasons.append(f"Illiquid: only {market_data.liquidity} contracts")
    
    if market_data.spread > max_spread:
        has_edge = False
        reasons.append(f"Wide spread: {market_data.spread:.3f}")
    
    if net_edge < min_edge_threshold:
        has_edge = False
        reasons.append(f"Low edge: {net_edge:.3f} < {min_edge_threshold}")
    
    if ev <= 0:
        has_edge = False
        reasons.append(f"Negative EV: {ev:.3f}")
    
    if has_edge:
        reasons.append(f"Net edge: {net_edge:.3f} ({net_edge*100:.1f}%)")
        reasons.append(f"EV: +{ev:.3f} per $1")
        reasons.append(f"Conf: {confidence:.2f}")
    
    reason = " | ".join(reasons)
    
    return EdgeAnalysis(
        ticker=market_data.ticker,
        has_edge=has_edge,
        estimated_true_prob=estimated_true_prob,
        market_implied_prob=market_implied,
        raw_edge=raw_edge,
        fee_cost=fee_cost,
        slippage_cost=slippage,
        total_cost=total_cost,
        net_edge=net_edge,
        expected_value=ev,
        confidence=confidence,
        liquidity_score=liquidity_score,
        closing_line_value=0.0,
        reason=reason,
        min_edge_threshold=min_edge_threshold
    )


if __name__ == "__main__":
    print("\n" + "="*70)
    print("EDGE CALCULATOR TEST")
    print("="*70)
    
    market = MarketData(
        ticker="BTCUSD-UP",
        yes_price=0.47,
        no_price=0.54,
        yes_bid=0.46,
        yes_ask=0.48,
        no_bid=0.53,
        no_ask=0.55,
        volume_24h=5000,
        spread=0.02,
        liquidity=200,
        fee_rate=0.01,
        time_to_expiry=4.0,
        venue="kalshi"
    )
    
    estimated_prob = 0.64
    analysis = analyze_edge(market, estimated_prob)
    
    print(f"\nTicker: {analysis.ticker}")
    print(f"Your estimate: {analysis.estimated_true_prob:.1%}")
    print(f"Market thinks: {analysis.market_implied_prob:.1%}")
    print(f"Raw edge: {analysis.raw_edge:.3f} ({analysis.raw_edge*100:.1f}%)")
    print(f"Costs: {analysis.total_cost:.3f}")
    print(f"Net edge: {analysis.net_edge:.3f} ({analysis.net_edge*100:.1f}%)")
    print(f"EV: {analysis.expected_value:+.3f} per $1")
    print(f"Confidence: {analysis.confidence:.2f}")
    print(f"\n{'✓ TRADE' if analysis.has_edge else '✗ SKIP'}")
    print(f"Reason: {analysis.reason}")
    print("="*70 + "\n")