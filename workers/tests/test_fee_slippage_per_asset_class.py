"""Phase C C.2 — per-asset-class fee/slippage dispatch tests.

Companion to test_fee_model.py + test_slippage_model.py (which exist
since Phase B and cover the in-table lookup walk). This module exercises
the C.2 fallback dispatch: when the (exchange, symbol, side) tier table
doesn't cover an instrument, dispatch on AssetClass to a per-class
default. crypto_spot keeps v1's 10/5 bps fallback (BIT-IDENTICAL);
fx/metals get new defaults; equities raise NotImplementedError naming
C.9.

Three concerns:
  1. Per-class fallback returns the documented value (parametrised
     across all AssetClass literals).
  2. crypto_spot bit-identity vs the pre-C.2 path (the load-bearing
     regression check — the 3 production strategies are all
     crypto_spot and MUST observe identical fees/slip).
  3. commission_for_spec / slippage_for_spec end-to-end on a realistic
     spec with each asset_class — the path that exercises the full
     dispatch chain that engine.py:298 + iterative.py:558 take.
"""

from __future__ import annotations

from typing import Any

import pytest
from marketmind_workers.backtest.fee_model import (
    _fallback_commission_for_class,  # pyright: ignore[reportPrivateUsage]
    commission_for_spec,
    default_fee_model,
)
from marketmind_workers.backtest.slippage_model import (
    _fallback_slippage_for_class,  # pyright: ignore[reportPrivateUsage]
    default_slippage_model,
    slippage_for_spec,
)

# ---- per-class fallback values (the dispatch table) -----------------------


@pytest.mark.parametrize(
    "asset_class,expected_fraction",
    [
        # Crypto: 10 bps — bit-identical with pre-C.2 _FALLBACK_BPS.
        ("crypto_spot", 0.001),
        # FX (Oanda demo): no explicit commission.
        ("fx_spot", 0.0),
        # Metals (Oanda XAU/USD): no explicit commission.
        ("metals_spot", 0.0),
        # Phase E.3: crypto perpetuals — conservative, == crypto_spot (10 bps).
        # Per-leg RT 30 bps; a 2-leg PAIR RT ~60 bps (E.4's cost-sanity gate).
        ("crypto_perp", 0.001),
    ],
)
def test_fallback_commission_per_asset_class(
    asset_class: str, expected_fraction: float,
) -> None:
    """Per-AssetClass fallback commission matches the documented table."""
    actual = _fallback_commission_for_class(asset_class)  # type: ignore[arg-type]
    assert actual == pytest.approx(expected_fraction), (
        f"commission fallback for {asset_class} = {actual}, expected {expected_fraction}"
    )


@pytest.mark.parametrize(
    "asset_class,expected_fraction",
    [
        # Crypto: 5 bps — bit-identical with pre-C.2 _FALLBACK_BPS.
        ("crypto_spot", 0.0005),
        # FX: 5 bps (~1 pip on EUR/USD per C.1.6 live data).
        ("fx_spot", 0.0005),
        # Metals: 12 bps (XAU spreads wider; design doc §C.2 table value).
        ("metals_spot", 0.0012),
        # Phase E.3: crypto perpetuals — conservative, == crypto_spot (5 bps).
        ("crypto_perp", 0.0005),
    ],
)
def test_fallback_slippage_per_asset_class(
    asset_class: str, expected_fraction: float,
) -> None:
    """Per-AssetClass fallback slippage matches the documented table."""
    actual = _fallback_slippage_for_class(asset_class)  # type: ignore[arg-type]
    assert actual == pytest.approx(expected_fraction), (
        f"slippage fallback for {asset_class} = {actual}, expected {expected_fraction}"
    )


def test_fallback_commission_none_routes_to_crypto() -> None:
    """`None` is the legacy-stub path: pre-C.1.1 specs that didn't
    carry asset_class (or duck-typed callers passing dicts) must route
    to the crypto fallback so bit-identity holds for ANY pre-C.2 user.
    """
    assert _fallback_commission_for_class(None) == _fallback_commission_for_class("crypto_spot")


def test_fallback_slippage_none_routes_to_crypto() -> None:
    assert _fallback_slippage_for_class(None) == _fallback_slippage_for_class("crypto_spot")


# ---- equity NotImplementedError + C.9 pointer ------------------------------


@pytest.mark.parametrize("asset_class", ["equity_etf", "equity_single"])
def test_fallback_commission_equity_raises_with_c9_pointer(asset_class: str) -> None:
    """Equity asset classes raise NotImplementedError naming the
    AssetClass and pointing at C.9. This catches a future fx-or-equity
    hunt that tries to backtest before AlpacaAdapter ships.
    """
    with pytest.raises(NotImplementedError, match=rf"{asset_class}.*C\.9") as exc:
        _fallback_commission_for_class(asset_class)  # type: ignore[arg-type]
    assert asset_class in str(exc.value)
    assert "C.9" in str(exc.value)


@pytest.mark.parametrize("asset_class", ["equity_etf", "equity_single"])
def test_fallback_slippage_equity_raises_with_c9_pointer(asset_class: str) -> None:
    with pytest.raises(NotImplementedError, match=rf"{asset_class}.*C\.9") as exc:
        _fallback_slippage_for_class(asset_class)  # type: ignore[arg-type]
    assert asset_class in str(exc.value)
    assert "C.9" in str(exc.value)


# ---- the load-bearing crypto bit-identity regression ---------------------


def test_crypto_spot_bit_identical_via_default_models() -> None:
    """THE load-bearing C.2 regression check. The 3 production strategies
    all run on crypto_spot. After this sub-phase, default_fee_model() +
    default_slippage_model() must return EXACTLY the pre-C.2 values for
    the (binance_spot, BTC/USDT) lookup that drives every existing
    backtest gauntlet run.

    Pre-C.2 values (frozen from B.1/B.2):
      commission_for(binance_spot, BTC/USDT, taker) = 0.001  (10 bps)
      slippage_for(binance_spot, BTC/USDT, taker)   = 0.0005 (5 bps)

    Any divergence breaks the 3 strategies' equity curves silently.
    """
    fee_m = default_fee_model()
    slip_m = default_slippage_model()

    # In-table path (pre-C.2 hit this directly).
    assert fee_m.commission_for("binance_spot", "BTC/USDT", "taker") == pytest.approx(0.001)
    assert slip_m.slippage_for("binance_spot", "BTC/USDT", "taker") == pytest.approx(0.0005)

    # Asset-class-aware in-table path (post-C.2 — must produce same value).
    assert fee_m.commission_for(
        "binance_spot", "BTC/USDT", "taker", asset_class="crypto_spot",
    ) == pytest.approx(0.001)
    assert slip_m.slippage_for(
        "binance_spot", "BTC/USDT", "taker", asset_class="crypto_spot",
    ) == pytest.approx(0.0005)

    # Fallback path with asset_class=None (legacy callers).
    assert fee_m.commission_for("kraken_spot", "BTC/USDT", "taker") == pytest.approx(0.001)
    assert slip_m.slippage_for("kraken_spot", "BTC/USDT", "taker") == pytest.approx(0.0005)


# ---- end-to-end: commission_for_spec / slippage_for_spec dispatch --------


def _stub_spec(exchange: str, symbol: str, asset_class: str) -> Any:
    """Minimal duck-typed spec carrying the three fields the resolvers
    read. Production specs are full StrategySpec instances; this stub
    mirrors the existing test convention in test_fee_model.py.
    """
    class _Inst:
        pass

    inst = _Inst()
    inst.exchange = exchange  # type: ignore[attr-defined]
    inst.symbol = symbol  # type: ignore[attr-defined]
    inst.asset_class = asset_class  # type: ignore[attr-defined]

    class _Spec:
        instrument = inst

    return _Spec()


def test_commission_for_spec_dispatches_on_crypto_spot() -> None:
    """End-to-end: a crypto spec resolves to the 10 bps default
    via the binance_spot in-table hit. Identical to the existing
    test_commission_for_spec_via_default_model coverage; included
    here so the per-class table forms a complete dispatch matrix.
    """
    spec = _stub_spec("binance", "BTC/USDT", "crypto_spot")
    assert commission_for_spec(spec) == pytest.approx(0.001)


def test_commission_for_spec_dispatches_on_fx_spot() -> None:
    """End-to-end: an FX spec falls back to 0 bps commission (Oanda
    demo charges via spread, not explicit fee).
    """
    spec = _stub_spec("oanda", "EUR/USD", "fx_spot")
    assert commission_for_spec(spec) == 0.0


def test_commission_for_spec_dispatches_on_metals_spot() -> None:
    spec = _stub_spec("oanda", "XAU/USD", "metals_spot")
    assert commission_for_spec(spec) == 0.0


def test_commission_for_spec_dispatches_on_equity_etf_raises() -> None:
    spec = _stub_spec("alpaca", "SPY", "equity_etf")
    with pytest.raises(NotImplementedError, match=r"equity_etf.*C\.9"):
        commission_for_spec(spec)


def test_slippage_for_spec_dispatches_on_crypto_spot() -> None:
    spec = _stub_spec("binance", "BTC/USDT", "crypto_spot")
    assert slippage_for_spec(spec) == pytest.approx(0.0005)


def test_slippage_for_spec_dispatches_on_fx_spot() -> None:
    """End-to-end: an FX spec falls back to 5 bps slippage (~1 pip
    EUR/USD per C.1.6 live findings).
    """
    spec = _stub_spec("oanda", "EUR/USD", "fx_spot")
    assert slippage_for_spec(spec) == pytest.approx(0.0005)


def test_slippage_for_spec_dispatches_on_metals_spot() -> None:
    """End-to-end: a metals spec falls back to 12 bps (XAU spreads
    wider in % terms per design doc §C.2).
    """
    spec = _stub_spec("oanda", "XAU/USD", "metals_spot")
    assert slippage_for_spec(spec) == pytest.approx(0.0012)


def test_slippage_for_spec_dispatches_on_equity_single_raises() -> None:
    spec = _stub_spec("alpaca", "AAPL", "equity_single")
    with pytest.raises(NotImplementedError, match=r"equity_single.*C\.9"):
        slippage_for_spec(spec)


# ---- composed: crypto + non-crypto in one suite hits the dispatch matrix --


@pytest.mark.parametrize(
    "exchange,symbol,asset_class,expected_commission,expected_slippage",
    [
        # Crypto: in-table hit on binance_spot BTC/USDT.
        ("binance", "BTC/USDT", "crypto_spot", 0.001, 0.0005),
        # FX majors (3 examples): fallback path; 0 commission + 5 bps slip.
        ("oanda", "EUR/USD", "fx_spot", 0.0, 0.0005),
        ("oanda", "GBP/USD", "fx_spot", 0.0, 0.0005),
        ("oanda", "USD/JPY", "fx_spot", 0.0, 0.0005),
        # Metals: fallback path; 0 commission + 12 bps slip.
        ("oanda", "XAU/USD", "metals_spot", 0.0, 0.0012),
        ("oanda", "XAG/USD", "metals_spot", 0.0, 0.0012),
    ],
)
def test_full_dispatch_matrix(
    exchange: str,
    symbol: str,
    asset_class: str,
    expected_commission: float,
    expected_slippage: float,
) -> None:
    """End-to-end matrix: every non-equity AssetClass via the public
    spec resolvers. This is what the gauntlet sees on every backtest.
    """
    spec = _stub_spec(exchange, symbol, asset_class)
    assert commission_for_spec(spec) == pytest.approx(expected_commission)
    assert slippage_for_spec(spec) == pytest.approx(expected_slippage)
