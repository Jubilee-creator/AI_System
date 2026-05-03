# Research Signal Discovery Report
**Generated:** 2026-05-03 18:50 UTC  
**Source CSV:** `data/research/joined/20260503T185046Z_research_joined.csv`  
**Rows:** 103  

---
## 1. Executive Verdict

### **NO EDGE FOUND (proof-eligible outcome rows insufficient: 0 or near-zero)**

- outcome-known rows: **51** (min for proof: 30 normal_modern)
- proof-eligible outcome rows: **4** / 30 minimum
- KXBTCD shows persistent above-breakeven WR across all 4 proof classes, but the sample is
  too small and too contaminated to call this proven edge.
- Momentum and PCE 0.06-0.10 signals are contaminated by legacy edge artifact.
- PCE >0.10 anti-edge is already handled by EDGE_DANGER_BLOCK_HIGH_EDGE guard.
- OTHER prefix has persistent negative performance but n=11.

> **real_money_allowed = False (hardcoded)**  
> **scale_allowed = False (hardcoded)**  
> No finding in this report unlocks either gate.

---
## 2. Dataset Used

| Field | Value |
|-------|-------|
| CSV path | `data/research/joined/20260503T185046Z_research_joined.csv` |
| Total rows | 103 |
| Columns | 39 |
| Generated at | 2026-05-03 18:50 UTC |

---
## 3. Safety Locks Confirmed

| Lock | Status |
|------|--------|
| real_money_allowed | **False — hardcoded** |
| scale_allowed | **False — hardcoded** |
| TRADING_MODE | PAPER |
| GLOBAL_FORCED_LEARNING_MODE | True — Kelly overridden, $5 flat |
| kalshi_client | GET-only, no POST |

This report is read-only. It does not change proof gates, strategy thresholds,
or trading behavior.

---
## 4. Outcome Class Breakdown

| Class | n | % | Usable for analysis? |
|-------|---|---|----------------------|
| MARKET_RESOLVED | 38 | 36.9% | **YES** — true market resolution |
| FORCED_TIME_EXIT | 13 | 12.6% | **YES (with caution)** — position closed at market price |
| FORCED_VOID | 10 | 9.7% | **NO** — validation reset, pnl=0 |
| OPEN_NO_OUTCOME | 42 | 40.8% | **NO** — ghost OPEN, no resolution |
| **outcome_known total** | **51** | **49.5%** | **YES** — basis for all analysis below |

**CAUTION:** FORCED_TIME_EXIT rows represent positions closed by the auto-settler
before market resolution. Their outcomes may not reflect the market's actual resolution.
Performance analysis is shown both combined and MARKET_RESOLVED-only where possible.

---
## 5. Data Coverage Breakdown

| Coverage | n (of 51 outcome-known) | % |
|----------|------|---|
| crypto_joined | 34 | 66.7% |
| kalshi_joined | 41 | 80.4% |
| both joined | 34 | 66.7% |
| model_probability present | 29 | — (modern rows only) |
| proof_eligible | 4 | 7.8% |
| legacy_or_incomplete | 22 | — |

---
## 6. Performance by Market Prefix

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| prefix:BTC15M | 2 | 1 | 1 | 0.500 | -40.5% | -40.5% | 0.705 | -20.5% | 0 | 0% | useless/anecdotal |
| prefix:DOGE | 1 | 0 | 1 | 0.000 | -27.0% | -27.0% | 0.640 | -64.0% | 0 | 0% | useless/anecdotal |
| prefix:ETH15M | 3 | 1 | 2 | 0.333 | -55.0% | -100.0% | 0.523 | -19.0% | 0 | 33% | useless/anecdotal |
| prefix:KXBTCD | 24 | 18 | 6 | 0.750 | -1.6% | +28.5% | 0.664 | +8.6% | 4 | 33% | weak exploratory |
| prefix:KXETHD | 7 | 4 | 3 | 0.571 | +0.7% | +15.0% | 0.699 | -12.7% | 0 | 57% | very weak |
| prefix:OTHER | 11 | 2 | 9 | 0.182 | -22.3% | -3.0% | 0.467 | -28.5% | 0 | 64% | weak exploratory |
| prefix:SOL | 1 | 0 | 1 | 0.000 | -6.0% | -6.0% | 0.820 | -82.0% | 0 | 0% | useless/anecdotal |
| prefix:XRP | 2 | 1 | 1 | 0.500 | -32.0% | -32.0% | 0.365 | +13.5% | 0 | 100% | useless/anecdotal |

**Notes:**
- KXBTCD: n=22, WR=0.773 above breakeven (0.662). Consistent across 4 proof classes.
  This is the strongest signal in the dataset — but contaminated and too small for proof.
- KXETHD: n=7, WR=0.571 vs breakeven 0.699. BELOW breakeven. mROI +0.7% only due to ROI skew.
- OTHER: n=11, WR=0.182 vs breakeven 0.467. Consistent anti-edge across novel market structures.
- BTC15M/ETH15M/SOL/XRP/DOGE: n < 5 each — anecdotal only, ignore.

### KXBTCD by proof class:

| Proof Class | n | W | L | WR | mROI | medROI |
|-------------|---|---|---|----|------|--------|
| BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL | 4 | 3 | 1 | 0.750 | -3.5% | +23.0% |
| BOOTSTRAP_PROVISIONAL_EXCLUDED | 5 | 3 | 2 | 0.600 | -21.0% | +20.0% |
| DATA_COLLECTION_OVERRIDE_EXCLUDED | 7 | 6 | 1 | 0.857 | +10.4% | +24.0% |
| LEGACY_OR_INCOMPLETE | 8 | 6 | 2 | 0.750 | +0.9% | +36.5% |

> All proof classes show WR >= 0.600 for KXBTCD. The persistence across legacy,
> DC-override, bootstrap-provisional, and era-allow rows is notable — but note
> that DIFFERENT entry criteria were applied in each era, so this is NOT clean
> evidence of a single strategy edge. The market structure may simply favor YES bets
> (BTC tends not to crash dramatically within single-day windows).

---
## 7. Performance by Entry Price Bucket

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| ep:<0.35 | 5 | 0 | 5 | 0.000 | -80.2% | -100.0% | 0.124 | -12.4% | 0 | 100% | very weak |
| ep:0.55-0.65 | 24 | 13 | 11 | 0.542 | +1.2% | +22.0% | 0.609 | -6.7% | 3 | 33% | weak exploratory |
| ep:0.65-0.75 | 15 | 9 | 6 | 0.600 | -22.6% | +15.0% | 0.684 | -8.4% | 1 | 60% | weak exploratory |
| ep:>0.75 | 7 | 5 | 2 | 0.714 | +12.9% | +19.0% | 0.804 | -9.0% | 0 | 0% | very weak |

**Notes:**
- `<0.35`: n=5, ALL LEGACY rows. 0/5 wins. This is the legacy edge artifact —
  cheap entries produced inflated edge scores under the old formula.
  The EDGE_DANGER_BLOCK_HIGH_EDGE guard already prevents these in production.
- `0.35-0.45`: no outcome-known rows.
- `0.55-0.65`: n=23, WR=0.522, mROI=-0.4%. Near-breakeven performance.
- `0.65-0.75`: n=14, WR=0.643, mROI=-17.1%. WR looks OK but mROI negative due to asymmetry.
- `>0.75`: n=7, WR=0.714, mROI=+12.9%. ALL DC_OVERRIDE_EXCLUDED rows. Cannot trust.

**WARNING:** The lack of rows in `0.35-0.45` suggests the system has never traded at
these prices, or they were excluded. No conclusions possible for that bucket.

---
## 8. Performance by Probability and Edge Buckets

### 8a. model_probability (modern rows only, n=27 outcome-known)

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| mp:0.60-0.70 | 11 | 4 | 7 | 0.364 | -22.7% | -3.0% | 0.595 | -23.2% | 1 | 0% | weak exploratory |
| mp:0.70-0.80 | 10 | 5 | 5 | 0.500 | -18.0% | +2.5% | 0.653 | -15.3% | 2 | 0% | weak exploratory |
| mp:0.80-0.90 | 7 | 4 | 3 | 0.571 | -3.1% | +19.0% | 0.784 | -21.3% | 1 | 0% | very weak |
| mp:>0.90 | 1 | 1 | 0 | 1.000 | +12.0% | +12.0% | 0.880 | +12.0% | 0 | 0% | useless/anecdotal |

> model_probability is only present for rows with modern metadata (54/99 rows).
> The 27 outcome-known modern rows span n=11/9/6/1 across buckets — all very weak.
> MP 0.80-0.90 shows WR=0.667, mROI=+13% but n=6 only.

### 8b. risk_edge (modern rows only, n=27 outcome-known)

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| re:0.03-0.05 | 11 | 7 | 4 | 0.636 | -4.0% | +12.0% | 0.737 | -10.1% | 4 | 0% | weak exploratory |
| re:0.05-0.08 | 18 | 7 | 11 | 0.389 | -22.0% | -3.0% | 0.630 | -24.1% | 0 | 0% | weak exploratory |

> RE 0.03-0.05: n=9, WR=0.667, mROI=+2%. **Appears slightly better than RE 0.05-0.08**
> (WR=0.389). This is counterintuitive — lower pre-council edge performing better.
> Possible explanation: rows with risk_edge 0.05-0.08 may be riskier entries where
> the council PENALIZES confidence, resulting in the post-council edge being lower
> and outcomes worse. But n is too small (9 and 18) to conclude anything.

### 8c. post_council_edge (all rows, n=49 outcome-known)

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| pce:<0.00 | 11 | 6 | 5 | 0.545 | -3.5% | +19.0% | 0.659 | -11.4% | 0 | 18% | weak exploratory |
| pce:0.00-0.03 | 10 | 4 | 6 | 0.400 | -27.8% | -4.0% | 0.637 | -23.7% | 0 | 0% | weak exploratory |
| pce:0.03-0.06 | 14 | 8 | 6 | 0.571 | -9.4% | +10.0% | 0.710 | -13.9% | 4 | 36% | weak exploratory |
| pce:0.06-0.10 | 11 | 9 | 2 | 0.818 | +20.6% | +35.0% | 0.631 | +18.7% | 0 | 91% | weak exploratory |
| pce:>0.10 | 5 | 0 | 5 | 0.000 | -80.2% | -100.0% | 0.124 | -12.4% | 0 | 100% | very weak |

**CRITICAL WARNING — PCE buckets are CONTAMINATED by the legacy edge artifact:**

| PCE bucket | Legacy% | Explanation |
|------------|---------|-------------|
| PCE >0.10  | 100%    | ALL legacy rows. Formula: edge=confidence-ep-0.01. With ep=0.05, edge=0.65. Already blocked by EDGE_DANGER_BLOCK_HIGH_EDGE guard. |
| PCE 0.06-0.10 | 91%  | 10/11 legacy rows. Same formula artifact for ep=0.55-0.70 range. |
| PCE 0.03-0.06 | mixed | Mix of legacy and modern rows. |
| PCE <0.00  | mixed   | Rows where council penalized confidence below minimum. |

> The apparent WR=0.818 at PCE 0.06-0.10 is NOT evidence of edge. It is the
> combination of (a) legacy formula producing PCE=0.07 for moderate EP + moderate conf,
> and (b) KXBTCD having naturally high WR. These two artifacts compound.
> **Do not use PCE buckets as a signal filter until legacy rows are excluded.**

---
## 9. Crypto Feature Findings

### 9a. Crypto momentum buckets (outcome-known + crypto-joined, n=34)

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| mom:5m_neg_mild | 2 | 0 | 2 | 0.000 | -100.0% | -100.0% | 0.630 | -63.0% | 0 | 0% | useless/anecdotal |
| mom:5m_flat | 25 | 16 | 9 | 0.640 | -10.2% | +15.0% | 0.662 | -2.2% | 1 | 36% | weak exploratory |
| mom:5m_pos_mild | 6 | 6 | 0 | 1.000 | +34.2% | +36.5% | 0.658 | +34.2% | 1 | 50% | very weak |
| mom:15m_neg_mild | 8 | 3 | 5 | 0.375 | -40.5% | -52.5% | 0.664 | -28.9% | 0 | 25% | very weak |
| mom:15m_flat | 14 | 9 | 5 | 0.643 | -12.1% | +13.5% | 0.663 | -2.0% | 1 | 36% | weak exploratory |
| mom:15m_pos_mild | 10 | 9 | 1 | 0.900 | +20.0% | +36.5% | 0.660 | +24.0% | 1 | 40% | weak exploratory |

**CRITICAL WARNING — Momentum signals are confounded by prefix:**

All crypto-joined outcome-known rows are KXBTCD (22 rows) and KXETHD (7 rows) plus
a few BTC15M/ETH15M. The positive momentum signal (WR=1.0 at 5m, WR=0.9 at 15m)
cannot be separated from the prefix effect:

| 5m positive momentum rows | proof_class |
|----------------------------|-------------|
| 4/6 LEGACY, 2/6 BOOTSTRAP_PROVISIONAL, 1/6 ERA_ALLOW | All KXBTCD/KXETHD |

> If KXBTCD wins 77% regardless of momentum, and the 6 positive-momentum rows happen
> to all be KXBTCD, then the 100% WR is just KXBTCD regime + tiny sample selection,
> NOT a momentum signal. This must be tested by controlling for prefix.

### 9b. Crypto volatility

- crypto_volatility_15m computed for: **4 / 103 rows**
- Most candle data only covers 2026-04-27 onwards. Most trades predate this window.
- Insufficient data for volatility analysis. No conclusions possible.

---
## 10. Kalshi Feature Findings

Kalshi-joined rows: **83 / 103** total
Kalshi-joined outcome-known: **41 / 51**

### 10a. Spread analysis (ask - bid)

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| spread:tight_<0.05 | 37 | 24 | 13 | 0.649 | -7.6% | +20.0% | 0.651 | -0.2% | 2 | 43% | moderate research signal |
| spread:mod_0.05-0.10 | 3 | 1 | 2 | 0.333 | -56.3% | -100.0% | 0.263 | +7.0% | 0 | 100% | useless/anecdotal |
| spread:wide_>0.10 | 1 | 0 | 1 | 0.000 | -2.0% | -2.0% | 0.790 | -79.0% | 0 | 0% | useless/anecdotal |

- Tight spread (<0.05): n=37, WR=0.649 — moderate performance, mixed proof classes.
- Wider spread (>0.05): n=4, WR=0.250 — worse performance but n=4 (anecdotal).
- Spread appears inversely correlated with outcome quality, but n is too small.

### 10b. Kalshi mid vs entry price gap

| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | proof_el | legacy% | confidence |
|---------|------|---|---|----|------|--------|-----------|----------|---------|---------|------------|
| midgap:model_above_mid_>+0.05 | 4 | 3 | 1 | 0.750 | -1.8% | +27.0% | 0.600 | +15.0% | 0 | 25% | useless/anecdotal |
| midgap:near_fair_-0.05_to_+0.05 | 34 | 22 | 12 | 0.647 | -4.3% | +21.5% | 0.626 | +2.1% | 2 | 53% | moderate research signal |
| midgap:model_below_mid_<-0.05 | 3 | 0 | 3 | 0.000 | -100.0% | -100.0% | 0.657 | -65.7% | 0 | 0% | useless/anecdotal |

- model_below_mid (entry < Kalshi market mid by >0.05): n=3, WR=0.000 — ALL losses.
  This is potentially the most actionable Kalshi finding: when our model prices
  a YES bet BELOW the market consensus, it consistently loses.
  **BUT n=3 is completely anecdotal.** Do not add a blocker.

---
## 11. Candidate Edge Pockets

### Candidate: `prefix:KXBTCD`
- **n_ok:** 24  
- **wins/losses:** 18/6  
- **win_rate:** 0.750  
- **breakeven_WR:** 0.664  
- **vs_breakeven:** +8.6%  
- **mean_ROI:** -1.6%  
- **median_ROI:** +28.5%  
- **mean_CLV:** 0.0741  
- **proof_eligible_n:** 4 / 24  
- **legacy%:** 33%  
- **confidence:** weak exploratory  
- **May be real because:** KXBTCD has shown consistently higher WR than breakeven across ALL proof classes (legacy, DC, bootstrap, era_allow). This persistence across proof class boundaries is the most credible signal in the dataset.  
- **May be fake because:** LEGACY rows 33% — edge formula artifact  
- **Forward-test rule:** Forward-test: track WR for all KXBTCD trades in next 30 normal_modern rows. If WR stays > 0.70, KXBTCD bias is real. If WR reverts toward 0.65, it was regime luck.  

### Candidate: `proof:LEGACY_OR_INCOMPLETE`
- **n_ok:** 22  
- **wins/losses:** 13/9  
- **win_rate:** 0.591  
- **breakeven_WR:** 0.530  
- **vs_breakeven:** +6.0%  
- **mean_ROI:** -8.3%  
- **median_ROI:** +23.0%  
- **mean_CLV:** 0.0022  
- **proof_eligible_n:** 0 / 22  
- **legacy%:** 100%  
- **confidence:** weak exploratory  
- **May be real because:** WR exceeds breakeven with positive median ROI.  
- **May be fake because:** LEGACY rows 100% — edge formula artifact; ZERO proof-eligible rows  
- **Forward-test rule:** Forward-test: collect 30+ proof-eligible rows in this segment before drawing conclusions.  

### Candidate: `pce:0.06-0.10`
- **n_ok:** 11  
- **wins/losses:** 9/2  
- **win_rate:** 0.818  
- **breakeven_WR:** 0.631  
- **vs_breakeven:** +18.7%  
- **mean_ROI:** +20.6%  
- **median_ROI:** +35.0%  
- **mean_CLV:** -0.0200  
- **proof_eligible_n:** 0 / 11  
- **legacy%:** 91%  
- **confidence:** weak exploratory  
- **May be real because:** Post-council edge in 0.06-0.10 range shows 9/11 wins. Edge level suggests council is correctly identifying genuine probability advantage.  
- **May be fake because:** LEGACY rows 91% — edge formula artifact; ZERO proof-eligible rows  
- **Forward-test rule:** Forward-test: collect 30+ proof-eligible rows in this segment before drawing conclusions.  

### Candidate: `mom:5m_pos_mild`
- **n_ok:** 6  
- **wins/losses:** 6/0  
- **win_rate:** 1.000  
- **breakeven_WR:** 0.658  
- **vs_breakeven:** +34.2%  
- **mean_ROI:** +34.2%  
- **median_ROI:** +36.5%  
- **mean_CLV:** 0.3300  
- **proof_eligible_n:** 1 / 6  
- **legacy%:** 50%  
- **confidence:** very weak  
- **May be real because:** Positive crypto momentum before entry correlates with wins. Could indicate regime alignment (rising price favors YES bets at threshold).  
- **May be fake because:** LEGACY rows 50% — edge formula artifact; tiny sample n=6  
- **Forward-test rule:** Forward-test: collect 30+ proof-eligible rows in this segment before drawing conclusions.  

### Candidate: `mom:15m_pos_mild`
- **n_ok:** 10  
- **wins/losses:** 9/1  
- **win_rate:** 0.900  
- **breakeven_WR:** 0.660  
- **vs_breakeven:** +24.0%  
- **mean_ROI:** +20.0%  
- **median_ROI:** +36.5%  
- **mean_CLV:** 0.1914  
- **proof_eligible_n:** 1 / 10  
- **legacy%:** 40%  
- **confidence:** weak exploratory  
- **May be real because:** Positive crypto momentum before entry correlates with wins. Could indicate regime alignment (rising price favors YES bets at threshold).  
- **May be fake because:** LEGACY rows 40% — edge formula artifact  
- **Forward-test rule:** Forward-test: for KXBTCD/KXETHD with 15m BTC/ETH return > +0.1%, track if WR remains > 0.85. NOTE: must control for prefix — test separately from baseline.  


---
## 12. Anti-Edge Pockets

### Anti-Edge: `src:FORCED_TIME_EXIT`
- **n_ok:** 13  
- **wins/losses:** 3/10  
- **win_rate:** 0.231  
- **breakeven_WR:** 0.621  
- **vs_breakeven:** -39.0%  
- **mean_ROI:** -1.6%  
- **proof_eligible_n:** 1  
- **legacy%:** 38%  
- **confidence:** weak exploratory  
- **Possible cause:** Win rate significantly below breakeven threshold.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `prefix:OTHER`
- **n_ok:** 11  
- **wins/losses:** 2/9  
- **win_rate:** 0.182  
- **breakeven_WR:** 0.467  
- **vs_breakeven:** -28.5%  
- **mean_ROI:** -22.3%  
- **proof_eligible_n:** 0  
- **legacy%:** 64%  
- **confidence:** weak exploratory  
- **Possible cause:** OTHER prefix contains novel/unusual market structures (KXETHMINY, KXETH hourly buckets) not seen in normal KXBTCD/KXETHD daily markets. Different resolution mechanics and lower liquidity.  
- **Future action:** WATCHLIST: Consider blocking non-KXBTCD/KXETHD/BTC15M/ETH15M prefixes once data accumulates. Do NOT add blocker yet — n=11 is insufficient.  

### Anti-Edge: `proof:BOOTSTRAP_PROVISIONAL_EXCLUDED`
- **n_ok:** 10  
- **wins/losses:** 4/6  
- **win_rate:** 0.400  
- **breakeven_WR:** 0.637  
- **vs_breakeven:** -23.7%  
- **mean_ROI:** -27.8%  
- **proof_eligible_n:** 0  
- **legacy%:** 0%  
- **confidence:** weak exploratory  
- **Possible cause:** Win rate significantly below breakeven threshold.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `proof:DATA_COLLECTION_OVERRIDE_EXCLUDED`
- **n_ok:** 15  
- **wins/losses:** 7/8  
- **win_rate:** 0.467  
- **breakeven_WR:** 0.701  
- **vs_breakeven:** -23.4%  
- **mean_ROI:** -9.9%  
- **proof_eligible_n:** 0  
- **legacy%:** 0%  
- **confidence:** weak exploratory  
- **Possible cause:** Win rate significantly below breakeven threshold.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `mp:0.60-0.70`
- **n_ok:** 11  
- **wins/losses:** 4/7  
- **win_rate:** 0.364  
- **breakeven_WR:** 0.596  
- **vs_breakeven:** -23.2%  
- **mean_ROI:** -22.7%  
- **proof_eligible_n:** 1  
- **legacy%:** 0%  
- **confidence:** weak exploratory  
- **Possible cause:** Lower model probability rows underperform. WR below breakeven suggests model overestimates at these confidence levels (calibration deficit).  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `mp:0.70-0.80`
- **n_ok:** 10  
- **wins/losses:** 5/5  
- **win_rate:** 0.500  
- **breakeven_WR:** 0.653  
- **vs_breakeven:** -15.3%  
- **mean_ROI:** -18.0%  
- **proof_eligible_n:** 2  
- **legacy%:** 0%  
- **confidence:** weak exploratory  
- **Possible cause:** Lower model probability rows underperform. WR below breakeven suggests model overestimates at these confidence levels (calibration deficit).  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `mp:0.80-0.90`
- **n_ok:** 7  
- **wins/losses:** 4/3  
- **win_rate:** 0.571  
- **breakeven_WR:** 0.784  
- **vs_breakeven:** -21.3%  
- **mean_ROI:** -3.1%  
- **proof_eligible_n:** 1  
- **legacy%:** 0%  
- **confidence:** very weak  
- **Possible cause:** Win rate significantly below breakeven threshold.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `re:0.05-0.08`
- **n_ok:** 18  
- **wins/losses:** 7/11  
- **win_rate:** 0.389  
- **breakeven_WR:** 0.630  
- **vs_breakeven:** -24.1%  
- **mean_ROI:** -22.0%  
- **proof_eligible_n:** 0  
- **legacy%:** 0%  
- **confidence:** weak exploratory  
- **Possible cause:** Win rate significantly below breakeven threshold.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `pce:0.00-0.03`
- **n_ok:** 10  
- **wins/losses:** 4/6  
- **win_rate:** 0.400  
- **breakeven_WR:** 0.637  
- **vs_breakeven:** -23.7%  
- **mean_ROI:** -27.8%  
- **proof_eligible_n:** 0  
- **legacy%:** 0%  
- **confidence:** weak exploratory  
- **Possible cause:** Win rate significantly below breakeven threshold.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  

### Anti-Edge: `mom:15m_neg_mild`
- **n_ok:** 8  
- **wins/losses:** 3/5  
- **win_rate:** 0.375  
- **breakeven_WR:** 0.664  
- **vs_breakeven:** -28.9%  
- **mean_ROI:** -40.5%  
- **proof_eligible_n:** 0  
- **legacy%:** 25%  
- **confidence:** very weak  
- **Possible cause:** Negative crypto momentum before entry. Could indicate market moving against direction of YES bet.  
- **Future action:** WATCHLIST ONLY: collect more data before considering a blocker.  


---
## 13. Contamination and Limitations

### Proof class mix in outcome-known rows:

| Proof Class | n | % | Eligible? |
|-------------|---|---|-----------|
| LEGACY_OR_INCOMPLETE | 22 | 43.1% | NO |
| DATA_COLLECTION_OVERRIDE_EXCLUDED | 15 | 29.4% | NO |
| BOOTSTRAP_PROVISIONAL_EXCLUDED | 10 | 19.6% | NO |
| BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL | 4 | 7.8% | YES |

### Key contamination issues:

1. **Legacy edge formula artifact**: `edge = confidence - entry_price - 0.01` for
   LEGACY_OR_INCOMPLETE rows produces edge values of 0.10-0.84 for cheap entries.
   This inflates PCE buckets 0.06-0.10 and >0.10. The EDGE_DANGER_BLOCK guard
   prevents this from recurring, but historical rows remain in the dataset.

2. **Cross-era contamination**: Rows from 4 different proof eras (legacy, DC-override,
   bootstrap-provisional, era-allow) have different entry criteria. Mixing them
   produces misleading averages. Strategy was different in each era.

3. **FORCED_TIME_EXIT inclusion**: 13/49 outcome-known rows are auto-settled positions,
   not market-resolved outcomes. Their outcomes may differ from true market resolution.

4. **Crypto coverage gap**: Candle data only starts 2026-04-27. Older trades get
   wrong/no volatility data. Momentum signals are only computable for recent trades.

5. **Survivorship / selection bias**: We only traded markets where the scanner
   found sufficient edge AND the council approved. The 'winning' segments may reflect
   selection criteria, not forward-looking edge.

6. **Kalshi coverage fragmentation**: 12/99 rows have NO_MATCHING_KALSHI_TICKER.
   For 72% of rows, Kalshi data exists. Spread and mid-gap analysis only on joined rows.

---
## 14. What To Forward-Test Next

Priority forward-test signals (in order of credibility):

1. **KXBTCD WR > breakeven (0.65-0.70)**
   - Hypothesis: KXBTCD daily BTC price markets resolve YES consistently above
     the model's 0.66 threshold entry price in the current regime.
   - Test: track next 30 KXBTCD normal_modern rows. If WR > 0.68, signal persists.
   - Confound to watch: if BTC enters a bearish regime, this reverses instantly.

2. **Kalshi spread as quality filter**
   - Hypothesis: trades with tight Kalshi spread (<0.05) have better outcomes
     because they represent more liquid, efficiently priced markets.
   - Test: separate Kalshi-joined trades by spread. Need n=30+ per bucket.

3. **Model-below-market-mid as blocking signal**
   - Hypothesis: when our YES entry price < Kalshi market mid price by >0.05,
     the market knows more than the model and the trade loses.
   - Test: n=3 currently. Need 10+ rows in this bucket before any conclusion.

4. **15m positive crypto momentum with KXBTCD prefix**
   - Hypothesis: BTC rising in the 15 minutes before our entry makes KXBTCD
     YES bets more likely to win (upward trend favors threshold clearance).
   - Test: track WR for KXBTCD rows with 15m BTC return > +0.1%. Must control
     for prefix — compute WR difference vs baseline KXBTCD WR (not all-market WR).
   - Confound: 9/10 current positive-15m rows are LEGACY — regime may have changed.

---
## 15. What NOT To Patch

Do NOT change ANY of the following based on this analysis:

- MIN_EDGE (0.03) — too small sample to justify
- MIN_CONFIDENCE (0.65) — too small sample to justify
- EDGE_DANGER_BLOCK_HIGH_EDGE — already blocking the legacy artifact correctly
- BOOTSTRAP_MIN_EDGE / BOOTSTRAP_MIN_CONFIDENCE
- Proof gate thresholds (30 normal_modern)
- real_money_allowed / scale_allowed locks
- GLOBAL_FORCED_LEARNING_MODE
- MAX_CONCURRENT_OPEN_TRADES
- PaperTrader execution logic
- Decision Council logic

Do NOT add a blocker for OTHER prefix yet — n=11 is insufficient evidence.
Do NOT add a momentum filter yet — confounded by prefix and n < 10.
Do NOT add a Kalshi mid-gap filter — n=3 for the concerning bucket.

---
## 16. Brutally Honest Recommendation

### Overall verdict: **NO EDGE FOUND (proof-eligible outcome rows insufficient: 0 or near-zero)**

**What looks potentially real:**

- KXBTCD shows WR above breakeven across ALL 4 proof classes. This persistence is
  the closest thing to a real signal in the data. It may reflect that BTC daily price
  markets are genuinely easier to predict than other markets in this regime.
  The market structure (daily settle, known threshold) may favor systematic YES bets.

**What looks fake or weak:**

- PCE 0.06-0.10 candidate edge is 91% legacy artifact. Discard.
- 5m/15m momentum WR=1.0/0.9 is completely confounded with prefix. Discard.
- >0.75 entry price performance is 100% DC_OVERRIDE_EXCLUDED rows. Discard.
- PCE >0.10 anti-edge is already known and already blocked.

**What the data is too small to answer:**

- Is KXBTCD WR 0.773 real or regime luck? Need 30 normal_modern rows to know.
- Does Kalshi spread quality actually matter? Need 30+ Kalshi-joined rows per bucket.
- Does crypto momentum add value on top of prefix alone? Need prefix-controlled test.

**Bottom line:**

The system is working correctly as a data collection engine. The sample is too
small and too contaminated to prove or disprove edge. The honest state is:
*data collection in progress, no actionable edge signal yet*.

Collecting more normal_modern rows (bootstrap_era_allow path) is the correct
next step. Phase 8P should focus on data accumulation, not signal exploitation.

---
*Report generated 2026-05-03 18:50 UTC | real_money_allowed=False | scale_allowed=False*  
*This report is read-only. It does not change proof gates, strategy, or trading behavior.*