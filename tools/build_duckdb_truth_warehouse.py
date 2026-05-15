#!/usr/bin/env python3
"""
Phase 11C — Build DuckDB truth warehouse from JSONL logs.

READ-ONLY with respect to source logs.  Writes only to:
  data/observability/ai_system_truth.duckdb

Tables:
  execution_funnel          — all rows from logs/execution_funnel.jsonl
  paper_trades              — all rows from logs/paper_trades.jsonl
  payoff_aware_shadow_ranking
  upstream_hygiene_shadow

Views (post-11A window):
  post_11a_execution_funnel
  post_11a_paper_trades
  post_11a_opened_trades
  post_11a_settled_trades

Sentinel: DUCKDB_TRUTH_WAREHOUSE_OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PHASE_11A_CUTOFF = "2026-05-15T13:46:29+00:00"

DB_DIR = ROOT / "data" / "observability"
DB_PATH = DB_DIR / "ai_system_truth.duckdb"

LOGS = {
    "execution_funnel":           ROOT / "logs" / "execution_funnel.jsonl",
    "paper_trades":               ROOT / "logs" / "paper_trades.jsonl",
    "payoff_aware_shadow_ranking":ROOT / "logs" / "payoff_aware_shadow_ranking.jsonl",
    "upstream_hygiene_shadow":    ROOT / "logs" / "upstream_hygiene_shadow.jsonl",
}

SENTINEL = "DUCKDB_TRUTH_WAREHOUSE_OK"


def _load_jsonl_pandas(path: Path):
    """Load a JSONL file into a pandas DataFrame, tolerating bad lines."""
    import pandas as pd

    if not path.exists():
        print(f"  [SKIP] {path.name} — file not found")
        return pd.DataFrame()

    rows = []
    bad = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    if bad:
        print(f"  [WARN] {path.name} — skipped {bad} malformed lines")
    return pd.DataFrame(rows) if rows else __import__("pandas").DataFrame()


def _load_large_ndjson_via_duckdb(con, table_name: str, path: Path) -> int:
    """
    Load a large NDJSON file directly in DuckDB (no pandas, memory-efficient).
    Uses DuckDB's read_ndjson with ignore_errors=true.
    Returns row count.
    """
    if not path.exists():
        print(f"  [SKIP] {path.name} — file not found")
        con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM (VALUES (NULL)) t(dummy) WHERE false")
        return 0

    abs_path = str(path.resolve())
    con.execute(f"""
        CREATE OR REPLACE TABLE {table_name} AS
        SELECT * FROM read_ndjson('{abs_path}', ignore_errors=true)
    """)
    return con.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]


def _load_small_jsonl_via_pandas(con, table_name: str, path: Path) -> int:
    """
    Load a small JSONL file via pandas (handles timezone-aware timestamps correctly).
    Returns row count.
    """
    df = _load_jsonl_pandas(path)
    if df.empty:
        con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM (VALUES (NULL)) t(dummy) WHERE false")
        return 0

    # Register the DataFrame and create a persistent table
    con.register(f"_tmp_{table_name}", df)
    con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM _tmp_{table_name}")
    con.unregister(f"_tmp_{table_name}")
    return con.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]


def _create_views(con) -> None:
    """Create post-11A analysis views."""
    cutoff = PHASE_11A_CUTOFF

    # execution_funnel: DuckDB reads timestamp_utc as naive TIMESTAMP (UTC).
    # Strip timezone suffix for proper TIMESTAMP comparison.
    cutoff_naive = cutoff[:19].replace("T", " ")  # "2026-05-15 13:46:29"
    con.execute(f"""
        CREATE OR REPLACE VIEW post_11a_execution_funnel AS
        SELECT * FROM execution_funnel
        WHERE timestamp_utc >= '{cutoff_naive}'::TIMESTAMP
    """)

    # paper_trades: timestamp column stored as VARCHAR-compatible (loaded via pandas)
    con.execute(f"""
        CREATE OR REPLACE VIEW post_11a_paper_trades AS
        SELECT * FROM paper_trades
        WHERE CAST("timestamp" AS VARCHAR) >= '{cutoff}'
    """)

    # Opened trades: funnel rows with final_status=TRADE_OPENED
    con.execute(f"""
        CREATE OR REPLACE VIEW post_11a_opened_trades AS
        SELECT * FROM post_11a_execution_funnel
        WHERE final_status = 'TRADE_OPENED'
    """)

    # Settled trades from paper_trades
    con.execute(f"""
        CREATE OR REPLACE VIEW post_11a_settled_trades AS
        SELECT * FROM post_11a_paper_trades
        WHERE status = 'SETTLED'
    """)


def main() -> int:
    try:
        import duckdb
    except ImportError:
        print("ERROR: duckdb not installed. Run: .venv_observability/bin/pip install duckdb")
        return 1

    DB_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print("  DuckDB Truth Warehouse Build")
    print(f"  Output: {DB_PATH.relative_to(ROOT)}")
    print("=" * 60)

    con = duckdb.connect(str(DB_PATH))

    print()
    print("  Loading tables:")

    # execution_funnel is 230MB — use DuckDB native streaming
    n = _load_large_ndjson_via_duckdb(con, "execution_funnel", LOGS["execution_funnel"])
    print(f"  [OK] execution_funnel             {n:>8,} rows  (DuckDB native NDJSON)")

    # Others are small — use pandas for correct timezone handling
    for table_name in ("paper_trades", "payoff_aware_shadow_ranking", "upstream_hygiene_shadow"):
        n = _load_small_jsonl_via_pandas(con, table_name, LOGS[table_name])
        print(f"  [OK] {table_name:<30} {n:>8,} rows  (pandas)")

    print()
    print("  Creating post-11A views:")
    _create_views(con)

    views = [
        "post_11a_execution_funnel",
        "post_11a_paper_trades",
        "post_11a_opened_trades",
        "post_11a_settled_trades",
    ]
    for v in views:
        n = con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]
        print(f"  [VIEW] {v:<35} {n:>6,} rows")

    con.close()
    print()
    print(f"  Database: {DB_PATH}")
    print(f"  {SENTINEL}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
