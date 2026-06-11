"""Smoke tests for TraderSettings + assert_paper_only guard."""

from __future__ import annotations

from decimal import Decimal

import pytest
from marketmind_workers.trader.config import (
    TraderSettings,
    assert_paper_only,
    get_trader_settings,
)


def test_assert_paper_only_passes_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default behaviour: env var not set -> coerced to "false" -> passes.
    monkeypatch.delenv("TRADER_ALLOW_LIVE", raising=False)
    assert_paper_only()  # no exception


@pytest.mark.parametrize("value", ["false", "FALSE", "False"])
def test_assert_paper_only_case_insensitive_false(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TRADER_ALLOW_LIVE", value)
    assert_paper_only()


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
def test_assert_paper_only_rejects_any_non_false(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TRADER_ALLOW_LIVE", value)
    with pytest.raises(AssertionError, match="disabled in v1"):
        assert_paper_only()


def test_trader_settings_defaults_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pop env keys that would override the defaults so the test is
    # hermetic regardless of the dev .env.
    for key in (
        "TRADER_QUEUE_NAME",
        "TRADER_SYMBOLS",
        "TRADER_TIMEFRAMES",
        "TRADER_STARTING_CASH_GBP",
        "TRADER_MAX_RISK_PER_TRADE_PCT",
        "TRADER_MAX_PORTFOLIO_RISK_PCT",
    ):
        monkeypatch.delenv(key, raising=False)
    get_trader_settings.cache_clear()
    # _env_file=None bypasses the dev .env file too — otherwise
    # pydantic-settings would still load values from it after the
    # shell-env clear above, and the test would assert against
    # whatever the developer has set locally instead of the genuine
    # Python defaults. Surfaced during the Phase B.3 default change.
    s = TraderSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.trader_queue_name == "trader_default"
    assert s.trader_max_risk_per_trade_pct == Decimal("0.01")
    assert s.trader_max_portfolio_risk_pct == Decimal("0.05")
    assert s.trader_starting_cash_gbp == Decimal("1000")
    assert s.symbols_list() == ["BTC/USDT", "ETH/USDT"]
    # Phase B.3 (2026-05-23): default is multi-TF (4h + 1h).
    # Phase B.8 (2026-05-23): added 15m alongside.
    assert s.timeframes_list() == ["4h", "1h", "15m"]


def test_trader_settings_symbols_list_trims_and_drops_empties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADER_SYMBOLS", "BTC/USDT, ETH/USDT,,SOL/USDT ")
    get_trader_settings.cache_clear()
    s = TraderSettings()
    assert s.symbols_list() == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def test_trader_settings_decimal_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Risk caps must round-trip through env as Decimal — not float —
    # so a 0.02 in env doesn't become 0.020000000000000004 in memory.
    monkeypatch.setenv("TRADER_MAX_DAILY_LOSS_PCT", "0.02")
    get_trader_settings.cache_clear()
    s = TraderSettings()
    assert s.trader_max_daily_loss_pct == Decimal("0.02")
