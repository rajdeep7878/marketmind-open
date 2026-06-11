"""Phase B.1 — FeeModel unit tests.

The model abstracts the per-fill commission lookup the backtest engines
used to do via `spec.costs.commission_pct`. The default StaticFeeModel
reproduces v1's flat 10 bps for Binance Spot BTC/USDT exactly — that
identity is what preserves bit-identity for the three existing seeded
strategies in the engine-integration regression (commit 3/5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from marketmind_workers.backtest.fee_model import (
    FeeTier,
    StaticFeeModel,
    commission_for_spec,
    default_fee_model,
    load_fee_model_from_json,
)


def test_default_model_returns_10_bps_for_btc_usdt_taker() -> None:
    # The bit-identity gate: the default must equal v1's 0.001.
    m = default_fee_model()
    assert m.commission_for("binance_spot", "BTC/USDT", "taker") == pytest.approx(0.001)
    assert m.commission_for("binance_spot", "BTC/USDT", "maker") == pytest.approx(0.001)


def test_unknown_exchange_falls_back_to_pessimist_default() -> None:
    m = default_fee_model()
    # Unknown exchange — falls back to the conservative 10 bps default,
    # which keeps a new-exchange backtest from accidentally running
    # cost-free.
    assert m.commission_for("kraken_spot", "BTC/USDT", "taker") == pytest.approx(0.001)


def test_unknown_symbol_falls_back_to_pessimist_default() -> None:
    m = default_fee_model()
    # Known exchange, unknown symbol — same conservative fallback.
    assert m.commission_for("binance_spot", "ETH/USDT", "taker") == pytest.approx(0.001)


def test_tiered_lookup_picks_highest_qualifying_tier() -> None:
    # Custom table with three VIP tiers — the lookup must select the
    # highest tier the notional volume qualifies for.
    table = {
        "binance_spot": {
            "BTC/USDT": {
                "taker": [
                    FeeTier(volume_30d_usd_min=0.0, bps=10.0),
                    FeeTier(volume_30d_usd_min=1_000_000.0, bps=8.0),
                    FeeTier(volume_30d_usd_min=10_000_000.0, bps=6.0),
                ],
                "maker": [FeeTier(volume_30d_usd_min=0.0, bps=10.0)],
            },
        },
    }
    m = StaticFeeModel(table)
    assert m.commission_for("binance_spot", "BTC/USDT", "taker", 0.0) == pytest.approx(0.001)
    assert m.commission_for("binance_spot", "BTC/USDT", "taker", 500_000.0) == pytest.approx(0.001)
    assert m.commission_for("binance_spot", "BTC/USDT", "taker", 1_000_000.0) == pytest.approx(0.0008)
    assert m.commission_for("binance_spot", "BTC/USDT", "taker", 15_000_000.0) == pytest.approx(0.0006)


def test_load_fee_model_from_json(tmp_path: Path) -> None:
    payload = {
        "binance_spot": {
            "BTC/USDT": {
                "taker": [{"volume_30d_usd_min": 0.0, "bps": 12.0}],
                "maker": [{"volume_30d_usd_min": 0.0, "bps": 8.0}],
            },
        },
    }
    p = tmp_path / "fees.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    m = load_fee_model_from_json(p)
    assert m.commission_for("binance_spot", "BTC/USDT", "taker") == pytest.approx(0.0012)
    assert m.commission_for("binance_spot", "BTC/USDT", "maker") == pytest.approx(0.0008)


def test_commission_for_spec_via_default_model() -> None:
    # Minimal spec stand-in — only instrument.{exchange,symbol} is read.
    class _Inst:
        exchange = "binance"
        symbol = "BTC/USDT"

    class _Spec:
        instrument = _Inst()

    # Default side = "taker"; default model returns 0.001 → identity.
    assert commission_for_spec(_Spec()) == pytest.approx(0.001)
    # Explicit maker request — same value at the default tier.
    assert commission_for_spec(_Spec(), side="maker") == pytest.approx(0.001)


def test_commission_for_spec_maps_binance_to_binance_spot() -> None:
    # The instrument.exchange value "binance" maps to "binance_spot" in
    # the fee table — this is the only mapping currently in place.
    class _Inst:
        exchange = "binance"
        symbol = "BTC/USDT"

    class _Spec:
        instrument = _Inst()

    # With a custom table keyed under "binance_spot" but not under
    # "binance", commission_for_spec must still find the 12 bps tier.
    table = {
        "binance_spot": {
            "BTC/USDT": {
                "taker": [FeeTier(volume_30d_usd_min=0.0, bps=12.0)],
                "maker": [FeeTier(volume_30d_usd_min=0.0, bps=12.0)],
            },
        },
    }
    m = StaticFeeModel(table)
    assert commission_for_spec(_Spec(), model=m) == pytest.approx(0.0012)
