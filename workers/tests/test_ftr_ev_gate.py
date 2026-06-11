"""The EV gate refuses entries below cost + margin; the EV>0 floor cannot
be configured away (mandate Stage 7)."""

from __future__ import annotations

import pandas as pd
import pytest
from marketmind_workers.ftr.strategies.ml_hourly import decide_window
from marketmind_workers.ftr.strategies.specs import MLHourlySpec
from pydantic import ValidationError

from .ftr_helpers import synthetic_ohlcv


def _spec(**overrides: object) -> MLHourlySpec:
    base: dict[str, object] = {
        "kind": "ml_hourly_longflat",
        "strategy_id": "test-ev",
        "venue_profile": "kraken_pro_uk_tier0",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
        "horizon_bars": 12,
    }
    base.update(overrides)
    return MLHourlySpec.model_validate(base)


def test_high_confidence_but_small_move_is_refused() -> None:
    """p_up = 0.99 with tiny expected move: edge < cost => no entry ever."""
    df = synthetic_ohlcv(n_bars=600, seed=21, vol=0.0001)  # ~1 bps hourly vol
    p_up = pd.Series(0.99, index=df.index[300:])
    dec = decide_window(df, p_up, spec=_spec(), k_calibration=1.0)
    assert (dec.frame["action"] != "ENTER_LONG").all()
    assert (dec.frame["ev_bps"] <= 0).all()
    # the refusals carry cost-aware reason codes
    reasons = set(dec.frame["reason"].unique())
    assert reasons <= {"SKIP_COST_DOMINATES", "SKIP_EV_NEGATIVE", "HOLD_NO_SIGNAL"}


def test_entry_requires_both_ev_and_p_min() -> None:
    df = synthetic_ohlcv(n_bars=600, seed=22, vol=0.02)  # huge vol => big E[move]
    # p above 0.5 (positive edge) but below p_min: must not enter
    p_up = pd.Series(0.54, index=df.index[300:])
    dec = decide_window(df, p_up, spec=_spec(p_min=0.55), k_calibration=1.0)
    assert (dec.frame["action"] != "ENTER_LONG").all()
    assert (dec.frame["reason"] == "SKIP_PROB_BELOW_MIN").any()
    # same setup with p above p_min: EV is large positive => enters
    p_up_hi = pd.Series(0.80, index=df.index[300:])
    dec_hi = decide_window(df, p_up_hi, spec=_spec(p_min=0.55), k_calibration=1.0)
    assert (dec_hi.frame["action"] == "ENTER_LONG").any()


def test_floor_cannot_be_configured_away() -> None:
    """No spec field can take the EV floor below 'cost itself'. The safety
    margin is clamped at >= 0 and there is no disable flag."""
    with pytest.raises(ValidationError):
        _spec(safety_margin_bps=-1.0)
    with pytest.raises(ValidationError):
        _spec(disable_ev_gate=True)  # extra="forbid": no such knob exists
    # And even at safety_margin=0, the venue round-trip cost still binds:
    df = synthetic_ohlcv(n_bars=600, seed=23, vol=0.0001)
    p_up = pd.Series(0.99, index=df.index[300:])
    dec = decide_window(df, p_up, spec=_spec(safety_margin_bps=0.0), k_calibration=1.0)
    assert (dec.frame["action"] != "ENTER_LONG").all()


def test_cost_multiplier_tightens_the_gate() -> None:
    df = synthetic_ohlcv(n_bars=800, seed=24, vol=0.004)
    p_up = pd.Series(0.62, index=df.index[400:])
    base = decide_window(df, p_up, spec=_spec(), k_calibration=1.0)
    stressed = decide_window(df, p_up, spec=_spec(), k_calibration=1.0, cost_multiplier=1.5)
    n_base = int((base.frame["action"] == "ENTER_LONG").sum())
    n_stressed = int((stressed.frame["action"] == "ENTER_LONG").sum())
    assert n_stressed <= n_base
