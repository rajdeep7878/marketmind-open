"""Tests for the author-claim vs measured comparison.

Covers the parsing edge cases ("70%", "10x", "143 trades", "2.5",
unparseable text), the multi-asset heuristic, and the explanation
content for each claim type.
"""

from __future__ import annotations

import pytest
from marketmind_shared.schemas import (
    AuthorClaim,
    AuthorClaimType,
    BacktestMetrics,
)
from marketmind_workers.backtest.author_comparison import compare_author_claims


def _metrics(
    *,
    total_return_pct: float = 0.15,
    win_rate: float = 0.55,
    max_dd: float = 0.20,
    num_trades: int = 120,
    sharpe: float = 1.1,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr=total_return_pct,
        annualized_volatility=0.3,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe * 1.2,
        max_drawdown_pct=max_dd,
        max_drawdown_duration_days=14,
        calmar_ratio=total_return_pct / max(max_dd, 1e-9),
        num_trades=num_trades,
        win_rate=win_rate,
        profit_factor=1.3,
        profit_factor_capped=False,
        avg_win_pct=0.04,
        avg_loss_pct=-0.025,
        expectancy=0.005,
        largest_win_pct=0.10,
        largest_loss_pct=-0.07,
        longest_winning_streak=4,
        longest_losing_streak=3,
        avg_trade_duration_days=5.0,
        exposure_pct=0.6,
        bars_processed=365,
        bars_per_year=365.0,
    )


def _claim(
    *,
    claim_type: AuthorClaimType,
    value: str,
    instrument: str | None = None,
    quote: str = "From the video.",
    period: str | None = None,
) -> AuthorClaim:
    return AuthorClaim(
        claim_type=claim_type,
        value=value,
        timeframe="1d",
        instrument=instrument,
        period=period,
        quote=quote,
    )


# ---- Parsing --------------------------------------------------------------


def test_parses_percent_string() -> None:
    c = _claim(claim_type=AuthorClaimType.RETURN, value="70%")
    out = compare_author_claims([c], _metrics(total_return_pct=0.5), spec_symbol="BTC/USDT")
    assert out[0].author_value_parsed == pytest.approx(0.70)
    assert out[0].measured_value == pytest.approx(0.50)
    assert out[0].difference == pytest.approx(-0.20)
    assert out[0].discrepancy_ratio == pytest.approx(-0.20 / 0.70)


def test_parses_multiplier_string() -> None:
    # "10x" -> +900% return -> 9.0
    c = _claim(claim_type=AuthorClaimType.RETURN, value="10x in 2 years")
    out = compare_author_claims([c], _metrics(total_return_pct=2.0), spec_symbol="BTC/USDT")
    assert out[0].author_value_parsed == pytest.approx(9.0)


def test_parses_trade_count_with_text() -> None:
    c = _claim(claim_type=AuthorClaimType.TRADE_COUNT, value="143 trades")
    out = compare_author_claims([c], _metrics(num_trades=140), spec_symbol="BTC/USDT")
    assert out[0].author_value_parsed == pytest.approx(143.0)
    assert out[0].measured_value == pytest.approx(140.0)


def test_parses_sharpe_bare_float() -> None:
    c = _claim(claim_type=AuthorClaimType.SHARPE, value="2.5")
    out = compare_author_claims([c], _metrics(sharpe=1.2), spec_symbol="BTC/USDT")
    assert out[0].author_value_parsed == pytest.approx(2.5)


def test_parses_negative_drawdown() -> None:
    # Authors sometimes report DD as a negative %.
    c = _claim(claim_type=AuthorClaimType.DRAWDOWN, value="-25%")
    out = compare_author_claims([c], _metrics(max_dd=0.30), spec_symbol="BTC/USDT")
    assert out[0].author_value_parsed == pytest.approx(-0.25)


def test_unparseable_value_emits_explanation() -> None:
    c = _claim(claim_type=AuthorClaimType.RETURN, value="great returns honestly")
    out = compare_author_claims([c], _metrics(), spec_symbol="BTC/USDT")
    assert out[0].author_value_parsed is None
    assert out[0].discrepancy_ratio is None
    assert "could not extract a numeric value" in out[0].explanation.lower()


def test_other_claim_recorded_as_informational() -> None:
    c = _claim(claim_type=AuthorClaimType.OTHER, value="I make $250k a week")
    out = compare_author_claims([c], _metrics(), spec_symbol="BTC/USDT")
    assert out[0].measured_value is None
    assert "not a category" in out[0].explanation.lower()


# ---- Multi-asset detection ------------------------------------------------


def test_multi_asset_heuristic_flags_portfolio() -> None:
    c = _claim(
        claim_type=AuthorClaimType.RETURN,
        value="40%",
        quote="Tested on a portfolio of top 10 altcoins.",
    )
    out = compare_author_claims([c], _metrics(total_return_pct=0.10), spec_symbol="BTC/USDT")
    expl = out[0].explanation.lower()
    assert "multi" in expl or "portfolio" in expl or "instruments" in expl


def test_multi_asset_heuristic_flags_different_instrument() -> None:
    c = _claim(
        claim_type=AuthorClaimType.RETURN,
        value="80%",
        instrument="SPY",
    )
    out = compare_author_claims([c], _metrics(total_return_pct=0.20), spec_symbol="BTC/USDT")
    assert "multi" in out[0].explanation.lower() or "different" in out[0].explanation.lower()


def test_same_instrument_does_not_flag_multi_asset() -> None:
    c = _claim(
        claim_type=AuthorClaimType.RETURN,
        value="20%",
        instrument="BTC/USDT",
        period="2024-01-01 to 2024-12-31",
    )
    out = compare_author_claims(
        [c],
        _metrics(total_return_pct=0.19),
        spec_symbol="BTC/USDT",
    )
    expl = out[0].explanation.lower()
    assert "multi-asset" not in expl
    assert "portfolio" not in expl


# ---- In-sample-optimisation framing ---------------------------------------


def test_large_negative_discrepancy_calls_out_in_sample_overfit() -> None:
    c = _claim(claim_type=AuthorClaimType.RETURN, value="200%")
    out = compare_author_claims(
        [c],
        _metrics(total_return_pct=0.20),
        spec_symbol="BTC/USDT",
    )
    expl = out[0].explanation.lower()
    assert "in-sample" in expl or "optim" in expl


def test_drawdown_much_worse_flags_selectivity() -> None:
    c = _claim(claim_type=AuthorClaimType.DRAWDOWN, value="5%")
    out = compare_author_claims(
        [c],
        _metrics(max_dd=0.30),
        spec_symbol="BTC/USDT",
    )
    expl = out[0].explanation.lower()
    assert "drawdown" in expl
    assert "larger" in expl or "selectively" in expl


def test_one_per_claim_in_order() -> None:
    claims = [
        _claim(claim_type=AuthorClaimType.RETURN, value="50%"),
        _claim(claim_type=AuthorClaimType.WIN_RATE, value="60%"),
        _claim(claim_type=AuthorClaimType.TRADE_COUNT, value="200 trades"),
    ]
    out = compare_author_claims(claims, _metrics(), spec_symbol="BTC/USDT")
    assert [r.claim_type for r in out] == [
        AuthorClaimType.RETURN,
        AuthorClaimType.WIN_RATE,
        AuthorClaimType.TRADE_COUNT,
    ]
