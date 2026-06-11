"""Direct tests for the post-hoc exit-reason attributor.

Each case constructs a minimal SignalSet + primary_df and verifies the
attributor returns the expected reason. No vbt or full engine — the
function is pure over (trade record fields, SignalSet, primary_df).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from marketmind_shared.schemas import (
    SignalDiagnostics,
    SignalDiagnosticsFailureMode,
)
from marketmind_shared.schemas.strategy_spec.common import Direction
from marketmind_shared.schemas.strategy_spec.exit import (
    StopLossFixedPrice,
    StopLossPercent,
    StopLossTrailingPercent,
    TakeProfitPercent,
)
from marketmind_workers.backtest.exit_attribution import (
    EXIT_REASON_END,
    EXIT_REASON_OPEN,
    EXIT_REASON_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    EXIT_REASON_TIME,
    attribute_exit,
)
from marketmind_workers.backtest.translator import SignalSet


def _stub_diagnostics() -> SignalDiagnostics:
    """The attributor never reads diagnostics; return a NONE-mode
    placeholder so SignalSet's required field is satisfied.
    """
    return SignalDiagnostics(
        bars_evaluated=0,
        nan_warmup_count=0,
        nan_post_warmup_count=0,
        true_count=0,
        deterministic_false_count=0,
        failure_mode=SignalDiagnosticsFailureMode.NONE,
        warmup_bars=0,
    )


_START = datetime(2024, 1, 1, tzinfo=UTC)


def _df(num_bars: int) -> pd.DataFrame:
    idx = pd.DatetimeIndex([_START + timedelta(days=i) for i in range(num_bars)])
    arr = np.linspace(100.0, 100.0 + num_bars, num=num_bars, dtype="float64")
    return pd.DataFrame(
        {"open": arr, "high": arr * 1.01, "low": arr * 0.99, "close": arr, "volume": arr},
        index=idx,
    )


def _signals(
    *,
    stop_loss: object | None = None,
    take_profit: object | None = None,
    max_bars_held: int | None = None,
    direction: Direction = Direction.LONG,
    num_bars: int = 30,
) -> SignalSet:
    idx = pd.DatetimeIndex([_START + timedelta(days=i) for i in range(num_bars)])
    return SignalSet(
        entries=pd.Series([False] * num_bars, index=idx),
        exits=pd.Series([False] * num_bars, index=idx),
        stop_loss=stop_loss,  # type: ignore[arg-type]
        take_profit=take_profit,  # type: ignore[arg-type]
        max_bars_held=max_bars_held,
        direction=direction,
        entry_diagnostics=_stub_diagnostics(),
    )


def test_open_status_returns_open() -> None:
    df = _df(20)
    s = _signals()
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=10)),
        entry_price=100.0,
        exit_price=105.0,
        direction=Direction.LONG,
        status="Open",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_OPEN


def test_stop_loss_percent_long_matches_when_exit_at_level() -> None:
    s = _signals(stop_loss=StopLossPercent(value=0.05))
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=5)),
        entry_price=100.0,
        exit_price=95.0,  # 5% below
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_STOP_LOSS


def test_take_profit_percent_long_matches_when_exit_at_level() -> None:
    s = _signals(take_profit=TakeProfitPercent(value=0.10))
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=5)),
        entry_price=100.0,
        exit_price=110.0,  # 10% above
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_TAKE_PROFIT


def test_stop_loss_fixed_price_matches() -> None:
    s = _signals(stop_loss=StopLossFixedPrice(price=92.0))
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=4)),
        entry_price=100.0,
        exit_price=92.0,
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_STOP_LOSS


def test_stop_loss_percent_short_inverts_sign() -> None:
    # For SHORT: SL above entry. SL of 0.05 puts the stop at entry * 1.05.
    s = _signals(stop_loss=StopLossPercent(value=0.05), direction=Direction.SHORT)
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=4)),
        entry_price=100.0,
        exit_price=105.0,
        direction=Direction.SHORT,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_STOP_LOSS


def test_time_exit_when_bars_held_matches_max() -> None:
    s = _signals(max_bars_held=5)
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=7)),  # 5 bars later
        entry_price=100.0,
        exit_price=101.0,  # not near any stop level
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_TIME


def test_end_of_data_when_exit_is_last_bar() -> None:
    s = _signals()
    df = _df(20)
    last_ts = pd.Timestamp(df.index[-1])  # type: ignore[arg-type]
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=last_ts,
        entry_price=100.0,
        exit_price=119.0,  # not near stop or TP — no stop / no TP configured
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_END


def test_signal_exit_when_no_other_rule_matches() -> None:
    s = _signals(
        stop_loss=StopLossPercent(value=0.10),  # 90.0 — exit_price not near
        take_profit=TakeProfitPercent(value=0.50),  # 150.0 — exit_price not near
    )
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=10)),  # not last bar
        entry_price=100.0,
        exit_price=102.5,
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_SIGNAL


def test_trailing_stop_loss_attributed_when_trade_lost() -> None:
    s = _signals(stop_loss=StopLossTrailingPercent(value=0.05))
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=8)),
        entry_price=100.0,
        exit_price=94.0,  # losing exit -> attributed to trailing stop
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_STOP_LOSS


def test_trailing_stop_loss_not_attributed_when_trade_won() -> None:
    s = _signals(stop_loss=StopLossTrailingPercent(value=0.05))
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=8)),
        entry_price=100.0,
        exit_price=108.0,  # winning exit with no TP configured -> signal
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_SIGNAL


def test_sl_takes_precedence_over_tp_when_both_match() -> None:
    # Misconfigured spec where SL and TP would both match same price.
    # Real specs never do this but exit_attribution should be
    # deterministic in any case — and our convention is SL wins.
    s = _signals(
        stop_loss=StopLossFixedPrice(price=100.0),
        take_profit=TakeProfitPercent(value=0.0001),  # 100 * 1.0001 ≈ 100
    )
    df = _df(20)
    reason = attribute_exit(
        entry_time=pd.Timestamp(_START + timedelta(days=2)),
        exit_time=pd.Timestamp(_START + timedelta(days=4)),
        entry_price=100.0,
        exit_price=100.0,
        direction=Direction.LONG,
        status="Closed",
        signals=s,
        primary_df=df,
    )
    assert reason == EXIT_REASON_STOP_LOSS
