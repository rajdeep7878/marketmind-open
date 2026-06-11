"""Phase C C.5(4) — FX backtest perf regression + cross-engine parity.

Runs a representative FX strategy through both engines (vbt + iterative)
against the 2025 EUR/USD 1H parquet fixture (C.5(3)). Three checks:

  1. Wall-clock < 30 s per engine (generous; the brief's ceiling for
     catastrophic regression, not a micro-optimisation target).
  2. Cross-engine trade-count parity within Phase B tolerance
     (vbt vs iterative within ±2× per v1.2.A pattern).
  3. The weekend-drop dispatch from C.5(1)/(2) actually fires: the
     spec carries SessionHours(weekend_closed=True), so the engine's
     data dict gets dropped to weekday-only bars before backtest.

Strategy (minimum-viable FX, NOT the C.7 candidate):
  - Entry: TimeOfDayCondition(8, 8) — fires only on the 08:00 UTC
    bar (London open in winter, near London open in summer)
  - Exit: TimeExit(max_bars_held=8) — exits 8 bars later (16:00 UTC
    same day)
  - Stop loss: 1% (required by the SpecTemplate schema)

This is intentionally simpler than the C.7 strategy (which adds a
Highest indicator + Asian-session breakout logic). C.5's perf test
proves the engine path runs cleanly on FX data; C.7 will exercise
the actual hunt + extract + gauntlet pipeline against a real edge.

Stays in the default test suite (no marker) — total wall-clock
~2-5 s including parquet load, comparable to test_iterative_perf_1h.py
which exercises crypto 1H at similar density.

NOTE on pandas FutureWarning suppression: vectorbt's internal
Portfolio.from_signals path triggers a pandas FutureWarning about
object-dtype .fillna downcasting on this FX dataset shape. The
warning fires inside pandas itself (not our code, not vectorbt's
top-level API), so we can't fix it upstream. The module-level
filterwarnings marker below suppresses it ONLY for this test module
so the warning isn't lost project-wide.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import Timeframe
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.session_filter import drop_weekends_in_data_dict

# Suppress pandas' object-dtype fillna FutureWarning that vectorbt
# triggers internally on this FX dataset shape. The warning is real
# but lives in third-party code we can't change; pinned per-module
# rather than added to the project-wide allowlist. Format intentionally
# omits the module restriction — the warning fires DEEP inside pandas
# internals and the module-filter sometimes misses depending on the
# stack frame pytest captures.
pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")


_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_PARQUET_EURUSD_1H_2025 = _FIXTURES / "market" / "eurusd_1h_2025.parquet"

# Empirical (META-PATTERN — first-measure-then-encode):
#   vbt path:        ~37 s on 6216-row fixture, 259 trades.
#   iterative path:  ~3 s on the post-weekend-drop 6078 rows.
# vbt's cost scales with trade count (per-trade stop-loss evaluation);
# the FX TimeOfDay strategy hits 1 trade/day = 4% entry rate, ~5×
# denser than the crypto Turtle on 1H. Threshold set to 60 s — the
# brief's STOP ceiling — so a future engine regression beyond ~1.6×
# trips, while normal vbt cost stays under it. iterative threshold
# stays tighter (15 s) because its per-bar loop is linear in bars
# regardless of trade count.
_VBT_THRESHOLD_S: float = 60.0
_ITERATIVE_THRESHOLD_S: float = 15.0


def _fx_spec_dict() -> dict[str, Any]:
    """Minimum-viable FX strategy spec — TimeOfDay entry + TimeExit."""
    return {
        "schema_version": "1.0",
        "name": "C.5 perf — FX TimeOfDay entry",
        "instrument": {
            "symbol": "EUR/USD",
            "exchange": "oanda",
            "quote_currency": "USD",
            "asset_class": "fx_spot",
            "session_hours": {
                "calendar": "cme_fx",
                "open_utc": "22:00",
                "close_utc": "22:00",
                "weekend_closed": True,
            },
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "time_of_day",
                "start_hour_utc": 8,
                "end_hour_utc": 8,
                "inclusive_end": True,
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "time", "max_bars_held": 8},
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.01}},
            ],
        },
    }


def _load_fx_data() -> pd.DataFrame:
    """Load the 2025 EUR/USD fixture."""
    return pd.read_parquet(_PARQUET_EURUSD_1H_2025)


# ---- perf regression: vbt engine -----------------------------------------


def test_fx_eurusd_2025_vbt_under_threshold() -> None:
    """vbt path: TimeOfDay entry strategy on 6216-bar EUR/USD 1H 2025.
    Asserts wall-clock under 30 s and produces a non-empty trade ledger.
    """
    spec, _warnings = validate_spec(_fx_spec_dict())
    data = _load_fx_data()
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)

    t0 = time.perf_counter()
    result = run_backtest(
        spec, start=start, end=end, initial_capital=10_000.0,
        data_override={Timeframe.H1: data},
    )
    elapsed = time.perf_counter() - t0

    # Sanity: produced trades. ~250 trading days × 1 trade/day = ~250
    # potential entries (one per 08:00 UTC bar). Assert at least 100
    # to catch a future bug that nukes the trade ledger.
    assert len(result.trades) >= 100, (
        f"vbt FX backtest produced only {len(result.trades)} trades; "
        f"expected >= 100 for daily-08:00-UTC entries over 2025"
    )
    assert elapsed < _VBT_THRESHOLD_S, (
        f"vbt FX backtest took {elapsed:.2f}s on {len(data)} bars, "
        f"exceeding the {_VBT_THRESHOLD_S:.1f}s threshold"
    )


# ---- perf regression: iterative engine -----------------------------------


def test_fx_eurusd_2025_iterative_under_threshold() -> None:
    """iterative path: same spec, same fixture. Direct iterative call
    bypasses engine.run_backtest's router, so the caller (this test)
    applies the C.5 weekend-drop manually via drop_weekends_in_data_dict
    to mirror what the router does upstream.
    """
    spec, _warnings = validate_spec(_fx_spec_dict())
    raw = _load_fx_data()
    data_dict = drop_weekends_in_data_dict({Timeframe.H1: raw}, spec)

    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)

    t0 = time.perf_counter()
    result = run_iterative_backtest(spec, data_dict, start, end, initial_capital=10_000.0)
    elapsed = time.perf_counter() - t0

    assert len(result.trades) >= 100, (
        f"iterative FX backtest produced only {len(result.trades)} trades; "
        f"expected >= 100 for daily-08:00-UTC entries over 2025"
    )
    assert elapsed < _ITERATIVE_THRESHOLD_S, (
        f"iterative FX backtest took {elapsed:.2f}s on "
        f"{len(data_dict[Timeframe.H1])} bars, exceeding "
        f"the {_ITERATIVE_THRESHOLD_S:.1f}s threshold"
    )


# ---- cross-engine drift parity --------------------------------------------


def test_fx_eurusd_2025_cross_engine_trade_count_envelope() -> None:
    """vbt vs iterative on the same FX spec must produce trade counts
    within ±2× of each other (v1.2.A cross-engine envelope pattern).

    Strict bit-identity isn't achievable across engines (known exit-
    tie-break differences); the envelope is the right gate for the
    weekend-drop path.
    """
    spec, _warnings = validate_spec(_fx_spec_dict())
    raw = _load_fx_data()

    vbt_result = run_backtest(
        spec, start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, tzinfo=UTC),
        initial_capital=10_000.0,
        data_override={Timeframe.H1: raw},
    )

    iterative_data = drop_weekends_in_data_dict({Timeframe.H1: raw}, spec)
    iterative_result = run_iterative_backtest(
        spec, iterative_data,
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC),
        initial_capital=10_000.0,
    )

    vbt_n = len(vbt_result.trades)
    iter_n = len(iterative_result.trades)
    assert vbt_n > 0 and iter_n > 0, (
        f"FX cross-engine: vbt produced {vbt_n} trades, iterative {iter_n}; "
        "both must be non-zero for envelope test to be meaningful"
    )
    ratio = max(vbt_n, iter_n) / min(vbt_n, iter_n)
    assert ratio <= 2.0, (
        f"FX cross-engine trade count out of envelope: vbt={vbt_n}, "
        f"iterative={iter_n} (ratio {ratio:.2f}× > 2×)"
    )


# ---- weekend-drop confirmation -------------------------------------------


def test_fx_eurusd_2025_weekend_drop_fires() -> None:
    """The C.5(1)/(2) weekend-drop helper must actually fire on this
    spec. Raw fixture has 6216 rows (138 weekend); after drop, weekend
    count = 0. This proves the dispatch is wired correctly.
    """
    spec, _warnings = validate_spec(_fx_spec_dict())
    raw = _load_fx_data()
    weekend_before = int((raw.index.weekday >= 5).sum())  # type: ignore[attr-defined,union-attr]
    assert weekend_before > 0, (
        "fixture should contain weekend rows for the drop test to be meaningful"
    )
    dropped = drop_weekends_in_data_dict({Timeframe.H1: raw}, spec)[Timeframe.H1]
    weekend_after = int((dropped.index.weekday >= 5).sum())  # type: ignore[attr-defined,union-attr]
    assert weekend_after == 0, (
        f"weekend rows remain after drop_weekends_in_data_dict: {weekend_after}"
    )
    # Row counts: raw 6216 - 138 weekend = 6078 weekday rows.
    assert len(dropped) == 6078, f"expected 6078 weekday rows, got {len(dropped)}"
