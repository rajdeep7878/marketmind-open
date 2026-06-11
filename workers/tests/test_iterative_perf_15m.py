"""Phase B.8 — 15m backtest performance regression test.

Sibling to ``test_iterative_perf_1h.py``. Same Modern Turtle System 1
spec, same pattern, four times the bar count: 223,527 15m bars vs
55,912 1H bars (vs 13,985 4H bars). The iterative Tier-3 engine runs
at ~17× the 4H runtime for 16× the bars — essentially linear scaling
once more.

Local measurement (Phase B.8 commit, 2026-05-23): median ~4.44 s
across three warm runs, well under the 8 s test threshold and well
under the design doc B.8 acceptance budget of "<30 s for 6 years of
15m on ~210k bars".

Threshold reasoning:
  - Median local runtime: ~4.44 s.
  - 8 s = ~1.8× median headroom — tighter than the 1H test's 5× because
    the wall-clock baseline is longer (a flat 5 s headroom would be
    proportionally smaller). 8 s still absorbs CI runner variance
    (~50 % cold-cache penalty observed locally) without being so loose
    that a real regression slips through.
  - Early-warning signal: median creeping above ~6 s = 1.35× today's.
    Investigate BEFORE the test starts flaking.

The fixture (`tests/fixtures/market/btc_usdt_15m.parquet`, ~9.6 MB)
was fetched once via the `market_data` service and committed for
offline CI reproducibility — same pattern as the 4H + 1H fixtures.

Runs in the default suite (no marker). Wall-clock ~5 s including
parquet load. The 1H sibling runs ~2 s; with both in the suite,
the cost is bounded.
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
_PARQUET_15M = _FIXTURES / "market" / "btc_usdt_15m.parquet"

# Threshold reasoning above. Local median ~4.44 s; ~1.8× headroom is
# the CI-flake margin. Tightening to ~6 s is plausible after a few
# green CI runs of headroom under it.
_THRESHOLD_SECONDS: float = 8.0


def test_iterative_15m_turtle_under_threshold() -> None:
    """Modern Turtle on 6 years of 15m BTC/USDT — wall-clock budget.

    Single timed run, same shape as the 1H perf sibling. Asserts the
    trade ledger is non-empty so a future engine bug that nukes
    trades can't masquerade as good perf.
    """
    raw = json.loads(_TURTLE.read_text())
    # Override primary_timeframe — canonical fixture is 4H; this test
    # exercises the SAME spec at 15m density to keep the cost comparable
    # across the three timeframes (4H 0.26 s, 1H 1.05 s, 15m 4.44 s).
    raw["primary_timeframe"] = "15m"
    spec = StrategySpec.model_validate(raw)
    data = pd.read_parquet(_PARQUET_15M)

    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)

    t0 = time.perf_counter()
    result = run_iterative_backtest(spec, {spec.primary_timeframe: data}, start, end)
    elapsed = time.perf_counter() - t0

    assert result.trades, "Turtle System 1 on 15m must produce trades"

    assert elapsed < _THRESHOLD_SECONDS, (
        f"15m Turtle backtest took {elapsed:.2f}s on {len(data)} bars, "
        f"exceeding the {_THRESHOLD_SECONDS:.1f}s threshold. "
        f"Local baseline (B.8 commit): ~4.44s median. Investigate."
    )
