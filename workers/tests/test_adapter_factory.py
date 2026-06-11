"""Phase C C.1.4 — adapter factory dispatch tests.

Two helpers live in `workers/.../trader/exchanges.py` as of C.1.4(1):
  - `make_adapter(asset_class)`     — concrete adapter per AssetClass
  - `infer_asset_class_from_symbol(symbol)` — symbol-string → AssetClass

These tests cover the dispatch matrix end-to-end. Cassette discipline
preserved from C.1.3: the fx_spot / metals_spot branches construct
OandaAdapter with dummy credentials and rely on the adapter's
constructor-time validation; no HTTP is ever attempted.

Bit-identity regression for the crypto_spot path: a representative
crypto_spot construction must produce a BinanceAdapter — the 3
production strategies all run on crypto_spot, so this branch IS the
load-bearing safety check.
"""

from __future__ import annotations

import pytest
from marketmind_workers.trader.exchanges import (
    BinanceAdapter,
    IngestionError,
    infer_asset_class_from_symbol,
    make_adapter,
)
from marketmind_workers.trader.exchanges_oanda import OandaAdapter

# --- make_adapter dispatch matrix ------------------------------------------


def test_make_adapter_crypto_spot_returns_binance_adapter() -> None:
    """The load-bearing regression check. The 3 production strategies
    all run on crypto_spot — this dispatch branch MUST construct a
    BinanceAdapter or the bot's ingestion path silently breaks.
    """
    adapter = make_adapter("crypto_spot")
    assert isinstance(adapter, BinanceAdapter)


def test_make_adapter_fx_spot_returns_oanda_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """fx_spot routes to OandaAdapter with env-var-sourced creds.
    OANDA_API_KEY + OANDA_ACCOUNT_ID provided as dummy values
    (DUMMY_API_TOKEN_FOR_CASSETTE matches the C.1.3 cassette
    convention); OANDA_ENVIRONMENT defaults to practice.
    """
    monkeypatch.setenv("OANDA_API_KEY", "DUMMY_API_TOKEN_FOR_CASSETTE")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-001-1234567-001")
    monkeypatch.delenv("OANDA_ENVIRONMENT", raising=False)  # default to practice
    adapter = make_adapter("fx_spot")
    assert isinstance(adapter, OandaAdapter)


def test_make_adapter_metals_spot_returns_oanda_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """metals_spot uses the same Oanda v20 endpoint as fx_spot."""
    monkeypatch.setenv("OANDA_API_KEY", "DUMMY_API_TOKEN_FOR_CASSETTE")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-001-1234567-001")
    monkeypatch.delenv("OANDA_ENVIRONMENT", raising=False)
    adapter = make_adapter("metals_spot")
    assert isinstance(adapter, OandaAdapter)


def test_make_adapter_fx_spot_missing_creds_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """fx_spot without OANDA_API_KEY/ACCOUNT_ID raises IngestionError
    with a message naming the missing requirement + the docs path."""
    monkeypatch.delenv("OANDA_API_KEY", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    with pytest.raises(IngestionError, match=r"OANDA_API_KEY|OANDA_ACCOUNT_ID"):
        make_adapter("fx_spot")


def test_make_adapter_fx_spot_trade_environment_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OANDA_ENVIRONMENT="trade" is rejected at the factory level —
    BEFORE the OandaAdapter constructor would also reject it. Forward-
    fails with the paper-only message.
    """
    monkeypatch.setenv("OANDA_API_KEY", "DUMMY")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-001-1234567-001")
    monkeypatch.setenv("OANDA_ENVIRONMENT", "trade")
    with pytest.raises(IngestionError, match=r"paper-only|practice"):
        make_adapter("fx_spot")


def test_make_adapter_equity_etf_not_implemented() -> None:
    """equity_etf raises NotImplementedError naming the asset_class
    (debugging-friendly: when C.1.x lands, you can grep for which
    branch tripped a deployment)."""
    with pytest.raises(NotImplementedError, match=r"equity_etf|C\.1\.x"):
        make_adapter("equity_etf")


def test_make_adapter_equity_single_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match=r"equity_single|C\.1\.x"):
        make_adapter("equity_single")


# --- infer_asset_class_from_symbol -----------------------------------------


@pytest.mark.parametrize(
    "symbol,expected",
    [
        # crypto_spot
        ("BTC/USDT", "crypto_spot"),
        ("ETH/USDT", "crypto_spot"),
        ("SOL/USDC", "crypto_spot"),
        ("ETH/BTC", "crypto_spot"),
        # fx_spot
        ("EUR/USD", "fx_spot"),
        ("GBP/USD", "fx_spot"),
        ("USD/JPY", "fx_spot"),
        ("AUD/CAD", "fx_spot"),
        # metals_spot
        ("XAU/USD", "metals_spot"),
        ("XAG/USD", "metals_spot"),
        ("XPT/USD", "metals_spot"),
        # equity_etf
        ("SPY", "equity_etf"),
        ("QQQ", "equity_etf"),
        ("IWM", "equity_etf"),
        # equity_single
        ("AAPL", "equity_single"),
        ("MSFT", "equity_single"),
        ("NVDA", "equity_single"),
        ("F", "equity_single"),
    ],
)
def test_infer_asset_class_known_patterns(symbol: str, expected: str) -> None:
    """Documented symbol-string → AssetClass registry."""
    assert infer_asset_class_from_symbol(symbol) == expected


@pytest.mark.parametrize(
    "bad_symbol",
    [
        "FOO/BAR",          # unrecognised pair
        "EUR/ZZZ",          # FX-looking base, unknown quote
        "spy",              # lowercase ticker
        "TOOLONG",          # > 5 letters
        "AB1",              # alphanumeric
        "",                 # empty
        "BTC-USDT",         # dash separator, not ccxt convention
    ],
)
def test_infer_asset_class_rejects_unrecognised(bad_symbol: str) -> None:
    """Anything that doesn't match a documented pattern raises
    ValueError naming the offending symbol."""
    with pytest.raises(ValueError, match=r"cannot classify"):
        infer_asset_class_from_symbol(bad_symbol)


# --- composed: factory + inference end-to-end ------------------------------


def test_production_strategy_symbols_dispatch_to_binance_adapter() -> None:
    """The load-bearing regression check, composed.

    The 3 production strategies declare instrument.symbol in:
    {"BTC/USDT", "ETH/USDT"}. Both must classify as crypto_spot
    and dispatch to BinanceAdapter. If this test fails, the bot's
    ingestion path has silently broken for production deployments.
    """
    for sym in ["BTC/USDT", "ETH/USDT"]:
        ac = infer_asset_class_from_symbol(sym)
        assert ac == "crypto_spot", f"{sym} → {ac} (expected crypto_spot)"
        adapter = make_adapter(ac)
        assert isinstance(adapter, BinanceAdapter)


def test_eurusd_end_to_end_dispatches_to_oanda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C.7's first FX strategy will declare instrument.symbol=EUR/USD;
    end-to-end inference + factory must route to OandaAdapter.
    """
    monkeypatch.setenv("OANDA_API_KEY", "DUMMY")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-001-1234567-001")
    monkeypatch.delenv("OANDA_ENVIRONMENT", raising=False)
    ac = infer_asset_class_from_symbol("EUR/USD")
    assert ac == "fx_spot"
    adapter = make_adapter(ac)
    assert isinstance(adapter, OandaAdapter)
