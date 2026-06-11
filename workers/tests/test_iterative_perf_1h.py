"""Phase B.4 — 1H backtest performance regression test.

The iterative Tier-3 engine ran ~0.355 s on 6 years of 4H BTC/USDT
(~14 k bars) — that was Phase A's drift-parity baseline. Phase B is
about supporting 1H (and eventually 15 m) where bar counts grow
linearly: 6 years of 1H ≈ 56 k bars, 4× the 4H count.

This test runs Modern Turtle System 1 (the same prior_signal-gated
spec used by every other drift-parity / engine test) on the
55,912-bar 1H BTC/USDT fixture and asserts wall-clock under a
**threshold of 5 seconds**. Local measurement (Phase B.4 commit,
2026-05-23): median ~1.05 s across three warm runs, i.e. **~4.1× the
4H runtime** — essentially linear scaling, no algorithmic regression.

Threshold reasoning:
  - Median local runtime: ~1.05 s.
  - 5 s = ~5× median headroom — accommodates CI runner variance
    (shared-core hosts, cold caches, GIL contention from parallel
    workers) without being so loose that a real regression slips
    through. Tightening to ~3 s is plausible if the threshold proves
    consistently safe over a few CI runs; deliberately starting
    permissive.
  - If the median ever climbs over ~2 s, that's a 2× regression vs
    today and warrants investigation BEFORE the test starts
    flaking — the alert should fire from the median, not from the
    threshold tripping.

The fixture (`tests/fixtures/market/btc_usdt_1h.parquet`, ~2.6 MB) was
fetched once via the `market_data` service and committed for offline
CI reproducibility — same pattern as the 4H fixture.

Runs in the default suite (no marker) — total wall-clock ~2 s
including the parquet load, comparable to other engine tests
(test_iterative_live_drift_parity etc. take similar). The existing
`integration` marker means "needs the compose stack" and doesn't
apply here.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.iterative import run_iterative_backtest

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_TURTLE = _FIXTURES / "strategies" / "valid" / "11_turtle_system1.json"
_PARQUET_1H = _FIXTURES / "market" / "btc_usdt_1h.parquet"

# Threshold reasoning above. Local median ~1.05 s; ~5× headroom is the
# CI-flake margin. Tighten when the test has a few green CI runs of
# headroom under it.
_THRESHOLD_SECONDS: float = 5.0


def test_iterative_1h_turtle_under_threshold() -> None:
    """Modern Turtle on 6 years of 1H BTC/USDT — wall-clock budget.

    Single timed run. We don't median across N here because the test
    is a budget check, not a benchmark — if any single run blows the
    budget, that's a regression to surface (could be a one-off GC
    blip; could be a real algorithmic regression — either way, worth
    a human look).
    """
    raw = json.loads(_TURTLE.read_text())
    # Override primary_timeframe — the canonical fixture is 4H; this
    # test exercises the SAME spec at 1H density to keep the cost
    # comparable between the two timeframes.
    raw["primary_timeframe"] = "1h"
    spec = StrategySpec.model_validate(raw)
    data = pd.read_parquet(_PARQUET_1H)

    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)

    t0 = time.perf_counter()
    result = run_iterative_backtest(spec, {spec.primary_timeframe: data}, start, end)
    elapsed = time.perf_counter() - t0

    # Sanity: the run actually produced trades. A spec that produces
    # zero trades is artificially fast and would silently pass the
    # budget; assert non-empty so a future engine bug that nukes the
    # trade ledger doesn't masquerade as "good perf".
    assert result.trades, "Turtle System 1 on 1H must produce trades"

    assert elapsed < _THRESHOLD_SECONDS, (
        f"1H Turtle backtest took {elapsed:.2f}s on {len(data)} bars, "
        f"exceeding the {_THRESHOLD_SECONDS:.1f}s threshold. "
        f"Local baseline (B.4 commit): ~1.05s median. Investigate."
    )
