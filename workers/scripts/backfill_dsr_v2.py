"""Backfill prob_real_v2 across stored gauntlet runs (post-DSR-frequency-fix).

Reads every row in `overfitting_analyses`, joins to `backtest_results` to
obtain `bars_per_year`, recomputes `prob_real` with the corrected
frequency-consistent `n_observations = round(bars_processed / bars_per_year)`,
and writes the result to a sidecar JSON keyed by analysis_id.

The original `prob_real` in `overfitting_analyses.deflated_sharpe_json` is
NEVER touched. The composite score and verdict are NEVER touched. Seed
decisions are NEVER touched. This is observational only — Phase C onward
should consult `prob_real_v2` from the sidecar when evaluating gauntlet
runs; historical `prob_real` is retained for traceability.

Idempotent: re-running produces identical output (sorted JSON output).

Sidecar path: workers/data/dsr_backfill_v2.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import psycopg
from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe

SIDECAR_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "dsr_backfill_v2.json"
)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL must be set")
    return url


def main() -> None:
    rows = _fetch_rows()
    print(f"=== DSR v2 backfill: {len(rows)} stored gauntlet runs ===")
    out: dict[str, dict[str, float | int | str]] = {}
    deltas: list[tuple[str, float]] = []
    for r in rows:
        analysis_id = cast("str", r["analysis_id"])
        observed_sharpe = cast("float", r["observed_sharpe"])
        original_prob_real = cast("float", r["original_prob_real"])
        bars_processed = cast("int", r["bars_processed"])
        bars_per_year = cast("float", r["bars_per_year"])
        n_trials = cast("int", r["n_trials_estimate"])
        skew = cast("float", r["skewness"])
        kurt = cast("float", r["kurtosis"])
        # Same conversion the fix uses in overfitting_analysis.py.
        t_years = max(bars_processed / bars_per_year, 2.0)
        n_obs_v2 = round(t_years)
        result_v2 = deflated_sharpe(
            observed_sharpe=observed_sharpe,
            n_trials_estimate=n_trials,
            n_observations=n_obs_v2,
            returns_skewness=skew,
            returns_kurtosis=kurt,
        )
        prob_real_v2 = result_v2.probability_strategy_is_real
        delta = prob_real_v2 - original_prob_real
        deltas.append((analysis_id, delta))
        out[analysis_id] = {
            "observed_sharpe": observed_sharpe,
            "original_n_observations_bars": bars_processed,
            "original_prob_real": original_prob_real,
            "bars_per_year": bars_per_year,
            "n_observations_v2_years": n_obs_v2,
            "prob_real_v2": prob_real_v2,
            "delta_prob_real": delta,
        }
        print(
            f"  {analysis_id} : observed_sharpe={observed_sharpe:+.4f}  "
            f"prob_real: {original_prob_real:.6f} → {prob_real_v2:.6f}  "
            f"(Δ {delta:+.6f})"
        )
    SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIDECAR_PATH.write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    max_abs_delta = max(abs(d) for _, d in deltas) if deltas else 0.0
    max_row = max(deltas, key=lambda t: abs(t[1])) if deltas else ("(none)", 0.0)
    print()
    print("=== summary ===")
    print(f"  rows processed   : {len(rows)}")
    print(f"  max abs(delta)   : {max_abs_delta:.6f} (row {max_row[0]})")
    print(f"  sidecar written  : {SIDECAR_PATH}")
    print(
        "  original prob_real values UNCHANGED in overfitting_analyses table"
    )


def _fetch_rows() -> list[dict[str, Any]]:
    """JOIN overfitting_analyses ⨝ backtest_results, extract DSR fields."""
    sql = """
        SELECT
            oa.id::text                                              AS analysis_id,
            oa.backtest_id::text                                     AS backtest_id,
            (oa.deflated_sharpe_json->>'observed_sharpe')::float     AS observed_sharpe,
            (oa.deflated_sharpe_json->>'probability_strategy_is_real')::float
                                                                     AS original_prob_real,
            (oa.deflated_sharpe_json->>'n_trials_estimate')::int     AS n_trials_estimate,
            (oa.deflated_sharpe_json->>'n_observations')::int        AS original_n_observations,
            COALESCE((oa.deflated_sharpe_json->>'returns_skewness')::float, 0.0)
                                                                     AS skewness,
            COALESCE((oa.deflated_sharpe_json->>'returns_kurtosis')::float, 3.0)
                                                                     AS kurtosis,
            (br.result_json#>>'{metrics,bars_processed}')::int       AS bars_processed,
            (br.result_json#>>'{metrics,bars_per_year}')::float      AS bars_per_year
        FROM overfitting_analyses oa
        JOIN backtest_results br ON br.id = oa.backtest_id
        ORDER BY oa.created_at
    """
    with psycopg.connect(_database_url()) as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description] if cur.description else []
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


if __name__ == "__main__":
    main()
