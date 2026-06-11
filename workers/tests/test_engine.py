"""Tests for the backtest execution engine.

market_data.get_market_data is monkeypatched to return synthetic
data — no Binance calls in the default suite. The engine wraps the
translator + vectorbt; we assert on:

  * the BacktestRun shape
  * that trades happen at the expected timestamps for known fixtures
  * costs reduce returns vs zero-cost runs
  * fills happen at next-bar-open, not signal-bar-close
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import BacktestRun, validate_spec
from marketmind_shared.schemas.strategy_spec import (
    CostModel,
    StrategySpec,
    Timeframe,
)
from marketmind_workers.backtest import engine
from marketmind_workers.backtest.engine import run_backtest

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "strategies" / "valid"


def _load_fixture(name: str) -> StrategySpec:
    spec_dict = json.loads((_FIXTURES_DIR / name).read_text())
    spec, _warnings = validate_spec(spec_dict)
    return spec


def _make_ohlcv(
    n: int,
    *,
    freq: str = "1D",
    start: datetime | None = None,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    idx = pd.date_range(start, periods=n, freq=freq)
    if closes is None:
        c = np.arange(100.0, 100.0 + n, dtype=float)
    else:
        c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": c - 0.1,
            "high": c + 0.5,
            "low": c - 0.5,
            "close": c,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _patch_market_data(
    monkeypatch: pytest.MonkeyPatch,
    data_by_tf: dict[Timeframe, pd.DataFrame],
) -> None:
    """Replace engine.get_market_data with a synthetic-returning stub."""

    def fake_get(
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        data_dir: str | Path = "/data",
        client: object | None = None,
    ) -> pd.DataFrame:
        del symbol, start, end, data_dir, client
        tf = Timeframe(timeframe)
        return data_by_tf[tf].copy()

    monkeypatch.setattr(engine, "get_market_data", fake_get)


# ---- Golden Cross happy path ----------------------------------------------


def test_run_backtest_golden_cross_produces_trades(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _load_fixture("01_golden_cross.json")
    n = 600
    # Same shape as the translator test — guaranteed cross.
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    df = _make_ohlcv(n, closes=closes)
    _patch_market_data(monkeypatch, {Timeframe.D1: df})

    result = run_backtest(
        spec,
        start=df.index[0].to_pydatetime(),
        end=df.index[-1].to_pydatetime(),
        initial_capital=10_000.0,
    )

    assert isinstance(result, BacktestRun)
    assert result.spec_name == spec.name
    assert result.meta.symbol == "BTC/USDT"
    assert result.meta.primary_timeframe is Timeframe.D1
    assert result.meta.initial_capital == 10_000.0
    # The engineered series guarantees at least one round-trip trade.
    assert len(result.trades) >= 1
    # equity_curve covers the full bar range
    assert len(result.equity_curve) >= n - 1


# ---- Costs reduce returns -------------------------------------------------


def test_costs_reduce_final_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Higher cost models → lower final equity, for the same spec.

    Phase B.1 + B.2 (2026-05-23) moved cost resolution from
    ``spec.costs.{commission,slippage}_pct`` to ``FeeModel`` and
    ``SlippageModel`` via the engine's
    ``default_fee_model()`` / ``default_slippage_model()`` factories.
    The spec's costs field is no longer the engine's source of truth,
    so this test now differentiates the two runs by **monkeypatching
    the model factories** between them — the new abstraction surface.
    The contract (high costs reduce equity) is unchanged.
    """
    from marketmind_workers.backtest.fee_model import FeeTier, StaticFeeModel
    from marketmind_workers.backtest.slippage_model import (
        SlippageTier,
        StaticSlippageModel,
    )

    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    df = _make_ohlcv(n, closes=closes)
    _patch_market_data(monkeypatch, {Timeframe.D1: df})

    spec = _load_fixture("01_golden_cross.json")

    # Run 1: default FeeModel + SlippageModel (10 bps + 5 bps).
    result_default = run_backtest(
        spec,
        start=df.index[0].to_pydatetime(),
        end=df.index[-1].to_pydatetime(),
    )

    # Run 2: high-cost models — 100 bps each (1%).
    def _high_fee_model() -> StaticFeeModel:
        return StaticFeeModel(
            {
                "binance_spot": {
                    "BTC/USDT": {
                        "taker": [FeeTier(volume_30d_usd_min=0.0, bps=100.0)],
                        "maker": [FeeTier(volume_30d_usd_min=0.0, bps=100.0)],
                    },
                },
            },
        )

    def _high_slippage_model() -> StaticSlippageModel:
        return StaticSlippageModel(
            {
                "binance_spot": {
                    "BTC/USDT": {
                        "taker": [SlippageTier(volume_30d_usd_min=0.0, bps=100.0)],
                        "maker": [SlippageTier(volume_30d_usd_min=0.0, bps=100.0)],
                    },
                },
            },
        )

    monkeypatch.setattr(
        "marketmind_workers.backtest.engine.default_fee_model",
        _high_fee_model,
    )
    monkeypatch.setattr(
        "marketmind_workers.backtest.engine.default_slippage_model",
        _high_slippage_model,
    )
    result_high = run_backtest(
        spec,
        start=df.index[0].to_pydatetime(),
        end=df.index[-1].to_pydatetime(),
    )

    # If neither produced trades, the test is vacuous. Assert both did.
    assert result_default.trades
    assert result_high.trades
    # Higher costs => lower final equity for the same trade sequence.
    assert result_high.equity_curve[-1].value < result_default.equity_curve[-1].value


# ---- Fill at next-bar-open ------------------------------------------------


def test_fills_at_next_bar_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """For the Golden Cross fixture's first entry, the trade's entry
    price must equal the OPEN of the bar AFTER the crossover bar, not
    any earlier bar's price.
    """
    spec = _load_fixture("01_golden_cross.json")
    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    df = _make_ohlcv(n, closes=closes)
    _patch_market_data(monkeypatch, {Timeframe.D1: df})

    result = run_backtest(
        spec,
        start=df.index[0].to_pydatetime(),
        end=df.index[-1].to_pydatetime(),
    )
    assert result.trades, "synthetic dataset should produce >=1 trade"
    first = result.trades[0]
    entry_idx = df.index.get_loc(first.entry_time)
    assert isinstance(entry_idx, int)
    # Trade entry time should be the SIGNAL bar, and its entry price
    # should equal the NEXT bar's open price (modulo slippage). vbt
    # records the trade with the signal bar's timestamp but the
    # execution price from the next bar's open.
    if entry_idx + 1 < len(df):
        next_open = float(df.iloc[entry_idx + 1]["open"])
        # Allow a small tolerance for slippage applied by vbt.
        commission = spec.costs.commission_pct
        slippage = spec.costs.slippage_pct
        max_dev = abs(next_open) * (commission + slippage + 0.01)
        assert abs(first.entry_price - next_open) <= max_dev


# ---- RSI mean reversion with stop_loss + condition exit -------------------


def test_run_backtest_rsi_mean_reversion(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _load_fixture("02_rsi_mean_reversion.json")
    n = 100
    warmup = [100.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(30)]
    drop = list(np.linspace(100, 50, 20))
    rally = list(np.linspace(50, 110, 30))
    tail = [110.0 + (3.0 if i % 2 == 0 else -3.0) for i in range(20)]
    closes = warmup + drop + rally + tail
    df = _make_ohlcv(n, freq="4h", closes=closes)
    _patch_market_data(monkeypatch, {Timeframe.H4: df})

    result = run_backtest(
        spec,
        start=df.index[0].to_pydatetime(),
        end=df.index[-1].to_pydatetime(),
    )
    # At least one round trip — the RSI dip should generate an entry,
    # and either the RSI overbought exit OR the 5% stop should close.
    assert len(result.trades) >= 1
    for tr in result.trades:
        assert tr.exit_reason != ""


# ---- BacktestRun is JSON-serialisable ------------------------------------


def test_backtest_run_round_trips_through_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _load_fixture("01_golden_cross.json")
    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    df = _make_ohlcv(n, closes=closes)
    _patch_market_data(monkeypatch, {Timeframe.D1: df})

    result = run_backtest(
        spec,
        start=df.index[0].to_pydatetime(),
        end=df.index[-1].to_pydatetime(),
    )
    blob = result.model_dump_json()
    restored = BacktestRun.model_validate_json(blob)
    assert restored.meta.symbol == result.meta.symbol
    assert len(restored.trades) == len(result.trades)


# ---- Default-flag propagation ---------------------------------------------


def test_default_costs_flag_set_when_costs_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.loads((_FIXTURES_DIR / "01_golden_cross.json").read_text())
    # fixture 01 doesn't include costs; verify the flag is True.
    assert "costs" not in raw
    spec, _ = validate_spec(raw)

    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    df = _make_ohlcv(n, closes=closes)
    _patch_market_data(monkeypatch, {Timeframe.D1: df})

    result = run_backtest(spec, df.index[0].to_pydatetime(), df.index[-1].to_pydatetime())
    # No costs in the raw spec - field gets defaulted by Pydantic to
    # DEFAULT_COST_MODEL. Phase 1's StrategySpec.costs has a default,
    # so the model_validate makes spec.costs non-None. The engine
    # treats DEFAULT_COST_MODEL specifically as "defaulted".
    assert result.meta.defaulted_costs is True


def test_default_sizing_flag_false_when_sizing_present() -> None:
    raw = json.loads((_FIXTURES_DIR / "02_rsi_mean_reversion.json").read_text())
    # fixture 02 has position_sizing 10% — not the 100% default.
    assert raw["position_sizing"]["percent"] == 0.1
    spec, _ = validate_spec(raw)
    cm = CostModel()
    _ = cm  # silence the unused-name lint; the model is just here for ref
    # No need to run the backtest — just check the flag logic.
    sized, defaulted = engine._resolve_sizing(spec.position_sizing)
    assert defaulted is False
    assert sized == spec.position_sizing
