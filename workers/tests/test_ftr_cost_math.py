"""Round-trip cost computation per profile, sensitivity multipliers, and
reconciliation with the legacy 30 bps crypto model (no double counting)."""

from __future__ import annotations

import pytest
from marketmind_workers.ftr.backtest.costs import (
    LEGACY_CRYPTO_ROUND_TRIP_BPS,
    cost_breakdown,
    sensitivity_breakdowns,
)
from marketmind_workers.ftr.config.venue_profiles import (
    COST_SENSITIVITY_MULTIPLIERS,
    VENUE_PROFILES,
    get_profile,
)


def test_round_trip_formula_per_profile() -> None:
    # 2*fee + 2*half_spread + 2*slippage, BTC half-spread tier
    assert get_profile("binance_spot_reference").round_trip_cost_bps("BTC/USDT") == 26.0
    assert get_profile("kraken_pro_uk_tier0").round_trip_cost_bps("BTC/USDT") == 90.0
    assert get_profile("coinbase_advanced_uk_tier0").round_trip_cost_bps("BTC/USDT") == 130.0
    # non-BTC default half-spread
    assert get_profile("binance_spot_reference").round_trip_cost_bps("ETH/USDT") == 29.0
    assert get_profile("coinbase_advanced_uk_tier0").round_trip_cost_bps("ETH/USDT") == 134.0


def test_sensitivity_multipliers() -> None:
    bds = sensitivity_breakdowns("kraken_pro_uk_tier0", "BTC/USDT")
    assert [b.multiplier for b in bds] == list(COST_SENSITIVITY_MULTIPLIERS)
    assert bds[0].round_trip_bps == 90.0
    assert bds[1].round_trip_bps == pytest.approx(135.0)
    assert bds[2].round_trip_bps == pytest.approx(180.0)


def test_legacy_reconciliation_no_double_count() -> None:
    """binance_spot_reference (26 bps) vs legacy 30 bps: the FTR profile is
    the ONLY cost source in an FTR run; the legacy figure is reported for
    comparability and is the more pessimistic of the two."""
    ref = cost_breakdown("binance_spot_reference", "BTC/USDT")
    assert ref.round_trip_bps < LEGACY_CRYPTO_ROUND_TRIP_BPS
    assert LEGACY_CRYPTO_ROUND_TRIP_BPS - ref.round_trip_bps == pytest.approx(4.0)


def test_uk_feasibility_flags() -> None:
    assert not VENUE_PROFILES["binance_spot_reference"].uk_execution_feasible
    assert VENUE_PROFILES["kraken_pro_uk_tier0"].uk_execution_feasible
    assert VENUE_PROFILES["coinbase_advanced_uk_tier0"].uk_execution_feasible


def test_no_zero_cost_profile_exists() -> None:
    """Non-negotiable constraint 6: costs never default to zero."""
    for prof in VENUE_PROFILES.values():
        assert prof.round_trip_cost_bps("BTC/USDT") > 0
        assert prof.round_trip_cost_bps("DOGE/USDT") > 0
