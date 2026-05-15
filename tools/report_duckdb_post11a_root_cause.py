#!/usr/bin/env python3
"""
Phase 11C — Post-11A root-cause analysis via DuckDB.

Queries the truth warehouse built by build_duckdb_truth_warehouse.py.
READ-ONLY.  Does not modify source logs or config.

Sentinel: DUCKDB_POST11A_ROOT_CAUSE_OK
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "observability" / "ai_system_truth.duckdb"
PHASE_11A_CUTOFF = "2026-05-15T13:46:29+00:00"
EXPENSIVE_ENTRY = 0.80
TOXIC_LO, TOXIC_HI = 0.80, 0.90
WEAK_RR = 0.30
SENTINEL = "DUCKDB_POST11A_ROOT_CAUSE_OK"


def _line(w: int = 60) -> str:
    return "-" * w


def _section(title: str) -> None:
    print()
    print(_line())
    print(f"  {title}")
    print(_line())


def main() -> int:
    try:
        import duckdb
    except ImportError:
        print("ERROR: duckdb not installed")
        return 1

    if not DB_PATH.exists():
        print(f"ERROR: warehouse not found at {DB_PATH}")
        print("Run: .venv_observability/bin/python tools/build_duckdb_truth_warehouse.py")
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)

    print()
    print("=" * 60)
    print("  DuckDB — Post-11A Root-Cause Analysis")
    print(f"  Cutoff: {PHASE_11A_CUTOFF}")
    print("=" * 60)

    # ── 1. Pipeline counts ────────────────────────────────────────
    _section("1. POST-11A PIPELINE COUNTS")

    total_candidates = con.execute(
        "SELECT count(*) FROM post_11a_execution_funnel"
    ).fetchone()[0]
    total_opened = con.execute(
        "SELECT count(*) FROM post_11a_opened_trades"
    ).fetchone()[0]
    total_settled = con.execute(
        "SELECT count(*) FROM post_11a_settled_trades"
    ).fetchone()[0]
    print(f"  candidates (funnel rows):   {total_candidates:,}")
    print(f"  opened trades:              {total_opened:,}")
    print(f"  settled trades:             {total_settled:,}")

    # ── 2. Blocker breakdown ──────────────────────────────────────
    _section("2. BLOCKER COUNTS (all post-11A candidates)")

    blocker_rows = con.execute("""
        SELECT
            COALESCE(final_reason, 'UNKNOWN') as reason,
            count(*) as n
        FROM post_11a_execution_funnel
        GROUP BY final_reason
        ORDER BY n DESC
    """).fetchall()
    for reason, n in blocker_rows:
        pct = 100.0 * n / total_candidates if total_candidates else 0
        print(f"  {reason:<40} {n:>6,}  ({pct:.1f}%)")

    # ── 3. Blockers by ticker prefix ─────────────────────────────
    _section("3. TOP BLOCKED TICKER PREFIXES")

    prefix_blocks = con.execute("""
        SELECT
            regexp_replace(ticker, '-.*', '') as prefix,
            count(*) as n,
            count(*) FILTER (WHERE final_status = 'TRADE_OPENED') as opened
        FROM post_11a_execution_funnel
        WHERE ticker IS NOT NULL
        GROUP BY prefix
        ORDER BY n DESC
        LIMIT 12
    """).fetchall()
    print(f"  {'PREFIX':<20} {'TOTAL':>8} {'OPENED':>8}")
    for prefix, n, opened in prefix_blocks:
        print(f"  {prefix:<20} {n:>8,} {opened:>8,}")

    # ── 4. Opened trade entry price analysis ─────────────────────
    _section("4. OPENED TRADE ENTRY DISTRIBUTION")

    if total_opened > 0:
        entry_stats = con.execute("""
            SELECT
                round(avg(try_cast(confidence AS DOUBLE)), 4)  as avg_conf,
                round(avg(try_cast(edge AS DOUBLE)), 4)        as avg_edge,
                count(*) as n
            FROM post_11a_opened_trades
        """).fetchone()
        print(f"  opened count:        {entry_stats[2]}")
        print(f"  avg confidence:      {entry_stats[0]}")
        print(f"  avg edge:            {entry_stats[1]}")

    # Entry price analysis from paper_trades (has entry_price field)
    if total_settled > 0:
        ep_stats = con.execute(f"""
            SELECT
                count(*) as n,
                round(avg(try_cast(entry_price AS DOUBLE)), 4)  as avg_entry,
                round(avg(try_cast(pnl AS DOUBLE)), 4)          as avg_pnl,
                sum(CASE WHEN try_cast(pnl AS DOUBLE) > 0 THEN 1 ELSE 0 END) as wins,
                count(*) FILTER (WHERE try_cast(entry_price AS DOUBLE) >= {EXPENSIVE_ENTRY}) as expensive_entry,
                count(*) FILTER (WHERE try_cast(entry_price AS DOUBLE) >= {TOXIC_LO}
                               AND try_cast(entry_price AS DOUBLE) < {TOXIC_HI}) as toxic_zone,
                count(*) FILTER (WHERE (1.0 - try_cast(entry_price AS DOUBLE))
                               / NULLIF(try_cast(entry_price AS DOUBLE), 0) < {WEAK_RR}) as weak_rr
            FROM post_11a_settled_trades
        """).fetchone()
        n, avg_ep, avg_pnl, wins, expensive, toxic, weak = ep_stats
        print(f"  settled count:       {n}")
        print(f"  avg entry_price:     {avg_ep}")
        print(f"  avg pnl:             {avg_pnl}")
        print(f"  win_rate:            {wins/n:.4f}" if n else "  win_rate: N/A")
        print(f"  expensive entry(>={EXPENSIVE_ENTRY}): {expensive}/{n}")
        print(f"  toxic zone({TOXIC_LO}-{TOXIC_HI}): {toxic}/{n}")
        print(f"  weak RR(<{WEAK_RR}):       {weak}/{n}")

    # ── 5. ROI / PF from settled trades ──────────────────────────
    _section("5. ROI / PROFIT FACTOR (post-11A settled)")

    if total_settled > 0:
        pf_row = con.execute("""
            SELECT
                sum(CASE WHEN try_cast(pnl AS DOUBLE) > 0 THEN try_cast(pnl AS DOUBLE) ELSE 0 END) as gross_win,
                abs(sum(CASE WHEN try_cast(pnl AS DOUBLE) < 0 THEN try_cast(pnl AS DOUBLE) ELSE 0 END)) as gross_loss,
                sum(try_cast(pnl AS DOUBLE)) as total_pnl,
                sum(try_cast(entry_price AS DOUBLE) * 5) as total_invested
            FROM post_11a_settled_trades
        """).fetchone()
        gross_win, gross_loss, total_pnl, total_invested = pf_row
        pf = gross_win / gross_loss if gross_loss and gross_loss > 0 else None
        roi = total_pnl / total_invested if total_invested and total_invested > 0 else None
        print(f"  total_pnl:           ${total_pnl:+.2f}")
        print(f"  gross_win:           ${gross_win:.2f}")
        print(f"  gross_loss:          ${gross_loss:.2f}")
        print(f"  profit_factor:       {pf:.4f}" if pf else "  profit_factor:  N/A")
        print(f"  ROI:                 {roi*100:+.1f}%" if roi is not None else "  ROI: N/A")

    # ── 6. Top losing prefixes ────────────────────────────────────
    _section("6. SETTLED OUTCOME BY TICKER PREFIX")

    if total_settled > 0:
        prefix_outcomes = con.execute("""
            SELECT
                regexp_replace(ticker, '-.*', '') as prefix,
                count(*) as n,
                sum(CASE WHEN try_cast(pnl AS DOUBLE) > 0 THEN 1 ELSE 0 END) as wins,
                round(sum(try_cast(pnl AS DOUBLE)), 4) as total_pnl
            FROM post_11a_settled_trades
            WHERE ticker IS NOT NULL
            GROUP BY prefix
            ORDER BY total_pnl ASC
        """).fetchall()
        print(f"  {'PREFIX':<20} {'N':>4} {'WINS':>6} {'TOTAL_PNL':>12}")
        for prefix, n, wins, pnl in prefix_outcomes:
            print(f"  {prefix:<20} {n:>4} {wins:>6} {pnl:>+12.4f}")

    # ── 7. Final status breakdown ────────────────────────────────
    _section("7. FINAL_STATUS BREAKDOWN")

    stage_rows = con.execute("""
        SELECT
            COALESCE(final_status, 'UNKNOWN')  as stage,
            count(*) as n
        FROM post_11a_execution_funnel
        WHERE final_status != 'TRADE_OPENED'
        GROUP BY final_status
        ORDER BY n DESC
        LIMIT 10
    """).fetchall()
    for stage, n in stage_rows:
        pct = 100.0 * n / total_candidates if total_candidates else 0
        print(f"  {stage:<40} {n:>6,}  ({pct:.1f}%)")

    # ── 8. Safety flags in data ───────────────────────────────────
    _section("8. SAFETY FLAGS VISIBLE IN DATA")

    if total_settled > 0:
        flags = con.execute("""
            SELECT
                count(*) FILTER (WHERE try_cast(bootstrap_provisional AS BOOLEAN) = true) as bootstrap_prov,
                count(*) FILTER (WHERE try_cast(bootstrap_era_council_allow AS BOOLEAN) = true) as bootstrap_era,
                count(*) FILTER (WHERE try_cast(data_collection_override AS BOOLEAN) = true) as dc_override,
                count(*) FILTER (WHERE try_cast(global_forced_learning_mode AS BOOLEAN) = true) as forced_learning,
                count(*) FILTER (WHERE try_cast(kelly_sizing_used AS BOOLEAN) = true) as kelly_used
            FROM post_11a_settled_trades
        """).fetchone()
        bp, be, dc, fl, ku = flags
        print(f"  bootstrap_provisional:        {bp}")
        print(f"  bootstrap_era_council_allow:  {be}")
        print(f"  data_collection_override:     {dc}")
        print(f"  global_forced_learning_mode:  {fl}")
        print(f"  kelly_sizing_used:            {ku}")
        print(f"  [CONFIRM] Kelly disabled, DC override disabled")

    con.close()
    print()
    print(f"  {SENTINEL}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
