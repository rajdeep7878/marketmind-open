"""Phase B.2 — SlippageModel unit tests.

Sibling to ``test_fee_model.py``. The model abstracts the per-fill
slippage lookup the backtest engines used to do via
``spec.costs.slippage_pct``. The default ``StaticSlippageModel``
reproduces v1's flat **5 bps** for Binance Spot BTC/USDT exactly —
that identity is what preserves bit-identity for the three existing
seeded strategies in the engine-integration regression (commit 3/5).

Note: 5 bps, not 10 — slippage default is half the fee default. The
asymmetry is intentional (spreads on BTC/USDT majors are tighter than
round-trip commission); easy to typo, hence the explicit assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from marketmind_workers.backtest.slippage_model import (
    SlippageTier,
    StaticSlippageModel,
    default_slippage_model,
    load_slippage_model_from_json,
    slippage_for_spec,
)


def test_default_model_returns_5_bps_for_btc_usdt_taker() -> None:
    # The bit-identity gate: the default must equal v1's 0.0005.
    m = default_slippage_model()
    assert m.slippage_for("binance_spot", "BTC/USDT", "taker") == pytest.approx(0.0005)
    assert m.slippage_for("binance_spot", "BTC/USDT", "maker") == pytest.approx(0.0005)


def test_unknown_exchange_falls_back_to_pessimist_default() -> None:
    m = default_slippage_model()
    # Unknown exchange — falls back to the conservative 5 bps default,
    # which keeps a new-exchange backtest from accidentally running
    # slippage-free.
    assert m.slippage_for("kraken_spot", "BTC/USDT", "taker") == pytest.approx(0.0005)


def test_unknown_symbol_falls_back_to_pessimist_default() -> None:
    m = default_slippage_model()
    # Known exchange, unknown symbol — same conservative fallback.
    assert m.slippage_for("binance_spot", "ETH/USDT", "taker") == pytest.approx(0.0005)


def test_tiered_lookup_picks_highest_qualifying_tier() -> None:
    # Custom table with three volume tiers — the lookup must select
    # the highest tier the notional qualifies for. Higher volume → can
    # post tighter pegged orders → less slippage, so bps shrinks.
    table = {
        "binance_spot": {
            "BTC/USDT": {
                "taker": [
                    SlippageTier(volume_30d_usd_min=0.0, bps=5.0),
                    SlippageTier(volume_30d_usd_min=1_000_000.0, bps=4.0),
                    SlippageTier(volume_30d_usd_min=10_000_000.0, bps=3.0),
                ],
                "maker": [SlippageTier(volume_30d_usd_min=0.0, bps=5.0)],
            },
        },
    }
    m = StaticSlippageModel(table)
    assert m.slippage_for("binance_spot", "BTC/USDT", "taker", 0.0) == pytest.approx(0.0005)
    assert m.slippage_for("binance_spot", "BTC/USDT", "taker", 500_000.0) == pytest.approx(0.0005)
    assert m.slippage_for("binance_spot", "BTC/USDT", "taker", 1_000_000.0) == pytest.approx(0.0004)
    assert m.slippage_for("binance_spot", "BTC/USDT", "taker", 15_000_000.0) == pytest.approx(0.0003)


def test_load_slippage_model_from_json(tmp_path: Path) -> None:
    payload = {
        "binance_spot": {
            "BTC/USDT": {
                "taker": [{"volume_30d_usd_min": 0.0, "bps": 7.0}],
                "maker": [{"volume_30d_usd_min": 0.0, "bps": 3.0}],
            },
        },
    }
    p = tmp_path / "slippage.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    m = load_slippage_model_from_json(p)
    assert m.slippage_for("binance_spot", "BTC/USDT", "taker") == pytest.approx(0.0007)
    assert m.slippage_for("binance_spot", "BTC/USDT", "maker") == pytest.approx(0.0003)


def test_slippage_for_spec_via_default_model() -> None:
    # Minimal spec stand-in — only instrument.{exchange,symbol} is read.
    class _Inst:
        exchange = "binance"
        symbol = "BTC/USDT"

    class _Spec:
        instrument = _Inst()

    # Default side = "taker"; default model returns 0.0005 → identity.
    assert slippage_for_spec(_Spec()) == pytest.approx(0.0005)
    # Explicit maker request — same value at the default tier.
    assert slippage_for_spec(_Spec(), side="maker") == pytest.approx(0.0005)


def test_slippage_for_spec_maps_binance_to_binance_spot() -> None:
    # The instrument.exchange value "binance" maps to "binance_spot" in
    # the slippage table — this is the only mapping currently in place.
    class _Inst:
        exchange = "binance"
        symbol = "BTC/USDT"

    class _Spec:
        instrument = _Inst()

    # With a custom table keyed under "binance_spot" but not under
    # "binance", slippage_for_spec must still find the 7 bps tier.
    table = {
        "binance_spot": {
            "BTC/USDT": {
                "taker": [SlippageTier(volume_30d_usd_min=0.0, bps=7.0)],
                "maker": [SlippageTier(volume_30d_usd_min=0.0, bps=7.0)],
            },
        },
    }
    m = StaticSlippageModel(table)
    assert slippage_for_spec(_Spec(), model=m) == pytest.approx(0.0007)
