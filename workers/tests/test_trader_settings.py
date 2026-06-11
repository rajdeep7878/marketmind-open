"""Phase C C.1.5 — TraderSettings.assert_symbols_homogeneous_asset_class.

The C.1.4 ingestion loop dispatches a single adapter per cycle (based
on the first symbol's inferred class). A mixed TRADER_SYMBOLS
deployment would silently use the wrong adapter for some symbols.
This module covers the boot-time validator that catches the
misconfiguration before any job runs.

Empirical-inspection (META-PATTERN): every error-message-match
pattern was first read from an interactive raise to confirm the
exact wording.
"""

from __future__ import annotations

import pytest
from marketmind_workers.trader.config import TraderSettings


def _settings(symbols: str) -> TraderSettings:
    """Construct a TraderSettings with a custom TRADER_SYMBOLS. Other
    fields fall back to their pydantic-settings defaults.
    """
    return TraderSettings(trader_symbols=symbols)  # type: ignore[call-arg]


# --- happy paths -----------------------------------------------------------


def test_homogeneous_crypto_spot_accepts() -> None:
    """The current production TRADER_SYMBOLS value — 3 strategies all
    crypto_spot, must continue to pass without raising. THIS IS THE
    LOAD-BEARING REGRESSION CHECK for C.1.5.
    """
    s = _settings("BTC/USDT,ETH/USDT")
    s.assert_symbols_homogeneous_asset_class()  # no raise


def test_single_crypto_spot_accepts() -> None:
    s = _settings("BTC/USDT")
    s.assert_symbols_homogeneous_asset_class()


def test_homogeneous_fx_spot_accepts() -> None:
    """Forward-compatibility for C.7's first FX seed."""
    s = _settings("EUR/USD,GBP/USD,USD/JPY")
    s.assert_symbols_homogeneous_asset_class()


def test_homogeneous_metals_spot_accepts() -> None:
    s = _settings("XAU/USD,XAG/USD")
    s.assert_symbols_homogeneous_asset_class()


def test_homogeneous_equity_etf_accepts() -> None:
    s = _settings("SPY,QQQ,IWM")
    s.assert_symbols_homogeneous_asset_class()


def test_empty_symbols_accepts_silently() -> None:
    """Empty TRADER_SYMBOLS is accepted — the ingestion loop has its
    own fallback for that case (no need to fail boot here).
    """
    s = _settings("")
    s.assert_symbols_homogeneous_asset_class()


# --- reject paths ----------------------------------------------------------


def test_mixed_crypto_and_fx_raises_with_clear_message() -> None:
    """The exact failure mode the validator is designed to catch:
    BTC/USDT + EUR/USD share the ingestion cycle but would dispatch
    to different adapters.
    """
    s = _settings("BTC/USDT,EUR/USD")
    with pytest.raises(ValueError) as exc:
        s.assert_symbols_homogeneous_asset_class()
    msg = str(exc.value)
    # Required content:
    assert "mixes asset classes" in msg
    assert "BTC/USDT" in msg and "EUR/USD" in msg
    assert "crypto_spot" in msg and "fx_spot" in msg
    assert "C.5" in msg and "C.6" in msg and "C.7" in msg, (
        "error message must point to the multi-class sub-phases"
    )


def test_mixed_crypto_and_equity_raises() -> None:
    s = _settings("BTC/USDT,SPY")
    with pytest.raises(ValueError, match=r"mixes asset classes"):
        s.assert_symbols_homogeneous_asset_class()


def test_mixed_fx_and_metals_raises() -> None:
    """Even classes both routed to OandaAdapter still reject — the
    C.1.4 dispatch is one cycle, one adapter; future sub-phases
    will relax this for Oanda-side classes specifically.
    """
    s = _settings("EUR/USD,XAU/USD")
    with pytest.raises(ValueError, match=r"mixes asset classes"):
        s.assert_symbols_homogeneous_asset_class()


def test_unclassifiable_symbol_raises_consolidated_error() -> None:
    """A symbol that cannot be classified by
    infer_asset_class_from_symbol surfaces a single consolidated
    error rather than letting the deep ValueError bubble up
    unannotated.
    """
    s = _settings("FOO/BAR,BTC/USDT")
    with pytest.raises(ValueError) as exc:
        s.assert_symbols_homogeneous_asset_class()
    msg = str(exc.value)
    assert "FOO/BAR" in msg
    assert "cannot be classified" in msg or "cannot classify" in msg
