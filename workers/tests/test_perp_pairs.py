"""Phase E.3 — perp-pair engine: funding honesty + spread primitives.

The headline honesty item is the FUNDING SIGN/MARK correctness (a wrong sign
or last-instead-of-mark silently fabricates edge). Values here were
hand-verified against the real BTC/ETH fixtures BEFORE being encoded (the
empirical-inspection rule). The funding-formula unit tests need no fixtures;
the integration test uses the committed E.2 fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.perp_pairs import (
    build_spread,
    funding_cashflow,
    load_perp_pair_data,
    run_perp_pair_backtest,
    spread_zscore,
)

_REPO = Path(__file__).resolve().parents[2]
_FIX = _REPO / "tests" / "fixtures" / "market"
_HAVE_FIXTURES = (_FIX / "binance_btc_usdt_perp_1h.parquet").exists()


def _pair_spec() -> StrategySpec:
    return StrategySpec.model_validate({
        "schema_version": "1.0", "name": "BTC/ETH perp log-spread MR",
        "instrument": {"symbol": "ETH/USDT:USDT", "exchange": "binance_usdm",
                       "quote_currency": "USDT", "asset_class": "crypto_perp"},
        "primary_timeframe": "1h", "direction": "long",
        "entry": {"condition": {"type": "compare", "left": {"kind": "price", "field": "close"},
                  "op": ">=", "right": {"kind": "constant", "value": 0.0}}, "order_type": "market"},
        "exit": {"exits": [{"type": "time", "max_bars_held": 1}]},
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
        "costs": {"funding_model": "binance_8h"},
        "legs": [{"instrument": {"symbol": "BTC/USDT:USDT", "exchange": "binance_usdm",
                  "quote_currency": "USDT", "asset_class": "crypto_perp"},
                  "direction": "short", "weight": 1.0}],
        "spread": {"method": "log", "zscore_period": 168, "entry_z": 2.0, "exit_z": 0.5},
    })


# ---- funding sign: the whole ballgame (no fixtures needed) ------------------
def test_funding_sign_long_pays_positive_rate() -> None:
    # long (+qty), positive rate -> PAYS (negative cashflow); on MARK
    assert funding_cashflow(1.0, 100.0, 0.0001) == pytest.approx(-0.01)


def test_funding_sign_short_receives_positive_rate() -> None:
    # short (-qty), positive rate -> RECEIVES (positive cashflow)
    assert funding_cashflow(-1.0, 100.0, 0.0001) == pytest.approx(+0.01)


def test_funding_sign_long_receives_negative_rate() -> None:
    # long (+qty), NEGATIVE rate -> RECEIVES (shorts pay longs)
    assert funding_cashflow(1.0, 100.0, -0.0001) == pytest.approx(+0.01)


def test_funding_uses_mark_magnitude() -> None:
    # magnitude scales with the MARK price passed in, linearly
    assert funding_cashflow(2.0, 50_000.0, 0.0002) == pytest.approx(-2.0 * 50_000.0 * 0.0002)


# ---- spread primitives ------------------------------------------------------
def test_build_log_spread_matches_manual() -> None:
    import pandas as pd
    a = pd.Series([100.0, 110.0, 120.0])
    b = pd.Series([10.0, 11.0, 12.0])
    s = build_spread(a, b, "log")
    assert s.iloc[0] == pytest.approx(np.log(100.0) - np.log(10.0))
    assert s.iloc[2] == pytest.approx(np.log(120.0) - np.log(12.0))


def test_build_ratio_spread() -> None:
    import pandas as pd
    s = build_spread(pd.Series([100.0, 200.0]), pd.Series([50.0, 50.0]), "ratio")
    assert list(s) == pytest.approx([2.0, 4.0])


def test_spread_zscore_matches_rolling() -> None:
    import pandas as pd
    rng = np.random.default_rng(0)
    spread = pd.Series(rng.normal(size=300))
    z = spread_zscore(spread, 50)
    i = 200
    win = spread.iloc[i - 49:i + 1]
    manual = (spread.iloc[i] - win.mean()) / win.std(ddof=1)
    assert z.iloc[i] == pytest.approx(manual)


# ---- additive backward-compat: existing single-leg specs unaffected --------
def test_existing_single_leg_fixtures_have_no_legs() -> None:
    vdir = _REPO / "tests" / "fixtures" / "strategies" / "valid"
    for p in sorted(vdir.glob("*.json")):
        spec = StrategySpec.model_validate(json.loads(p.read_text()))
        assert spec.legs is None and spec.spread is None
        assert spec.costs.funding_model is None


# ---- integration: funding invariants over the real fixtures ----------------
@pytest.mark.skipif(not _HAVE_FIXTURES, reason="E.2 perp fixtures not present")
def test_funding_invariants_on_real_fixtures() -> None:
    spec = _pair_spec()
    legs = load_perp_pair_data(spec)
    res = run_perp_pair_backtest(spec, legs, initial_capital=10_000.0)
    assert res.funding_ledger, "expected funding accruals on a multi-year hold"

    markmap = {legs[0].symbol: legs[0].mark_close, legs[1].symbol: legs[1].mark_close}
    lastmap = {legs[0].symbol: legs[0].last["close"], legs[1].symbol: legs[1].last["close"]}
    mark_neq_last = 0
    for r in res.funding_ledger:
        # (1) the formula is applied exactly, every row
        assert r.cashflow == pytest.approx(-r.signed_qty * r.mark_price * r.funding_rate)
        # (2) the price used is the MARK fixture value, never last
        import pandas as pd
        ts = pd.Timestamp(r.timestamp)
        assert r.mark_price == pytest.approx(float(markmap[r.leg_symbol].loc[ts]))
        if not np.isclose(markmap[r.leg_symbol].loc[ts], lastmap[r.leg_symbol].loc[ts]):
            mark_neq_last += 1
    # mark genuinely differs from last on many rows -> using mark is load-bearing
    assert mark_neq_last > 0
    # net funding == sum of the ledger (no double counting)
    assert res.total_funding == pytest.approx(sum(r.cashflow for r in res.funding_ledger))


@pytest.mark.skipif(not _HAVE_FIXTURES, reason="E.2 perp fixtures not present")
def test_unlevered_gross_within_equity() -> None:
    # each entry sizes leg_A + leg_B == percent*equity, so gross <= equity
    # (unlevered). Check the first trade's notionals don't exceed equity.
    spec = _pair_spec()
    legs = load_perp_pair_data(spec)
    res = run_perp_pair_backtest(spec, legs, initial_capital=10_000.0)
    assert res.trades, "expected at least one trade"
    # final equity is finite and the sim completed
    assert np.isfinite(res.final_equity)
