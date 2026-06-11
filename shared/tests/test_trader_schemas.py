"""Smoke tests for the Trader v1 cross-service DTOs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from marketmind_shared.schemas.strategy_spec.common import Timeframe
from marketmind_shared.schemas.trader import (
    Alert,
    AlertChannel,
    BlockDecision,
    Candle,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperOrder,
    PaperPosition,
    PositionStatus,
    RiskEventType,
    Severity,
    SignalEvaluation,
    SignalKind,
    TemplateName,
    TraderStrategyVersion,
)
from pydantic import ValidationError


def test_candle_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        Candle(
            symbol="BTC/USDT",
            timeframe=Timeframe.H4,
            open_ts=datetime(2026, 5, 18, 12, 0),  # noqa: DTZ001  # naive — what we test
            close_ts=datetime(2026, 5, 18, 16, 0, tzinfo=UTC),
            open=Decimal("60000"),
            high=Decimal("60100"),
            low=Decimal("59900"),
            close=Decimal("60050"),
            volume=Decimal("1000"),
            is_closed=True,
        )


def test_candle_rejects_non_utc_offset() -> None:
    pst = timezone(timedelta(hours=-8))
    with pytest.raises(ValidationError, match="UTC"):
        Candle(
            symbol="BTC/USDT",
            timeframe=Timeframe.H4,
            open_ts=datetime(2026, 5, 18, 12, 0, tzinfo=pst),
            close_ts=datetime(2026, 5, 18, 16, 0, tzinfo=UTC),
            open=Decimal("60000"),
            high=Decimal("60100"),
            low=Decimal("59900"),
            close=Decimal("60050"),
            volume=Decimal("1000"),
            is_closed=True,
        )


def test_candle_accepts_utc_and_is_frozen() -> None:
    c = Candle(
        symbol="BTC/USDT",
        timeframe=Timeframe.H4,
        open_ts=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
        close_ts=datetime(2026, 5, 18, 16, 0, tzinfo=UTC),
        open=Decimal("60000"),
        high=Decimal("60100"),
        low=Decimal("59900"),
        close=Decimal("60050"),
        volume=Decimal("1000"),
        is_closed=True,
    )
    assert c.open == Decimal("60000")
    # frozen=True from _StrictModel — mutation raises.
    with pytest.raises(ValidationError):
        c.open = Decimal("70000")  # type: ignore[misc]


def test_signal_evaluation_basic_construction() -> None:
    se = SignalEvaluation(
        kind=SignalKind.BUY,
        reason="EMA cross + trend filter",
        indicators={"ema_fast": 60050.0, "ema_slow": 59800.0, "atr": 200.0},
        proposed_entry_price=Decimal("60050"),
        proposed_stop_price=Decimal("59600"),
    )
    assert se.kind == SignalKind.BUY
    assert se.proposed_take_profit_price is None
    assert se.indicators["atr"] == 200.0


def test_signal_evaluation_hold_has_no_special_construction() -> None:
    # HOLD evaluations carry the same shape; they're just never persisted.
    se = SignalEvaluation(
        kind=SignalKind.HOLD,
        reason="conditions not met",
        proposed_entry_price=Decimal("60050"),
        proposed_stop_price=Decimal("0"),
    )
    assert se.kind == SignalKind.HOLD


def test_paper_order_rejects_zero_size() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        PaperOrder(
            id=uuid4(),
            signal_id=uuid4(),
            strategy_version_id=uuid4(),
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            requested_size=Decimal("0"),
            requested_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
            status=OrderStatus.PENDING,
            intended_fill_ts=datetime(2026, 5, 18, 16, 0, tzinfo=UTC),
        )


def test_paper_position_requires_stop_price() -> None:
    # The trader invariant "every position has a stop" is enforced by
    # `Field(gt=0)` on the stop_price column: a zero or missing stop
    # cannot validate.
    with pytest.raises(ValidationError, match="stop_price"):
        PaperPosition(
            id=uuid4(),
            strategy_version_id=uuid4(),
            symbol="BTC/USDT",
            entry_order_id=uuid4(),
            entry_price=Decimal("60000"),
            entry_ts=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
            size=Decimal("0.1"),
            stop_price=Decimal("0"),  # invalid
            status=PositionStatus.OPEN,
        )


def test_alert_defaults_undelivered() -> None:
    a = Alert(
        id=uuid4(),
        ts=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
        channel=AlertChannel.LOG,
        severity=Severity.INFO,
        subject="bot started",
        body="trader_worker boot complete",
    )
    assert a.delivered is False
    assert a.delivery_error is None


def test_block_decision_approved_carries_size() -> None:
    decision = BlockDecision(kind="approved", size=Decimal("0.05"))
    assert decision.kind == "approved"
    assert decision.size == Decimal("0.05")
    assert decision.reason is None


def test_block_decision_blocked_carries_event_type() -> None:
    decision = BlockDecision(
        kind="blocked",
        reason="daily loss cap reached",
        event_type=RiskEventType.DAILY_LOSS_BREACH,
        risk_event_id=uuid4(),
    )
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.DAILY_LOSS_BREACH


def test_trader_strategy_version_rejects_risk_pct_above_one() -> None:
    # Field(gt=0, le=1): risk_pct=5 (interpreted as "5 = 5%" by mistake)
    # must be rejected.
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        TraderStrategyVersion(
            id=uuid4(),
            strategy_id=uuid4(),
            version=1,
            marketmind_spec_id=uuid4(),
            template=TemplateName.MA_TREND,
            parameters={},
            symbols=["BTC/USDT"],
            timeframes=[Timeframe.H4],
            risk_pct=Decimal("5"),  # bug: should be 0.05
            fee_bps=Decimal("10"),
            slippage_bps=Decimal("10"),
            backtest_metrics={},
            created_at=datetime(2026, 5, 18, tzinfo=UTC),
        )


def test_trader_strategy_version_template_must_be_known() -> None:
    with pytest.raises(ValidationError):
        TraderStrategyVersion(
            id=uuid4(),
            strategy_id=uuid4(),
            version=1,
            marketmind_spec_id=uuid4(),
            template="banana_bread",  # type: ignore[arg-type]  # invalid template
            parameters={},
            symbols=["BTC/USDT"],
            timeframes=[Timeframe.H4],
            risk_pct=Decimal("0.005"),
            fee_bps=Decimal("10"),
            slippage_bps=Decimal("10"),
            backtest_metrics={},
            created_at=datetime(2026, 5, 18, tzinfo=UTC),
        )
