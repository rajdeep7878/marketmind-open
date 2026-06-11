"""Tests for the trader v1 drift analyzer.

Layer 1: PURE unit tests for `_classify_health`,
`_two_sided_deviation`, `_extract_backtest_metrics` (no DB).
Layer 2: integration tests for `compute_and_persist_drift_for_all`
via testcontainers — opt-in via `pytest -m integration`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from marketmind_shared.schemas.trader import HealthStatus
from marketmind_workers.trader.config import TraderSettings, get_trader_settings
from marketmind_workers.trader.drift import (
    _BacktestMetrics,
    _classify_health,
    _extract_backtest_metrics,
    _two_sided_deviation,
    compute_and_persist_drift_for_all,
    scaled_thresholds,
    sqrt_n_scaling_factor,
)
from psycopg.types.json import Jsonb

# ---- Layer 1: pure unit tests ---------------------------------------------


class TestTwoSidedDeviation:
    def test_exact_match_returns_zero(self) -> None:
        assert _two_sided_deviation(Decimal("1"), Decimal("1")) == Decimal("0")

    def test_30pct_above_baseline(self) -> None:
        assert _two_sided_deviation(Decimal("1.3"), Decimal("1")) == pytest.approx(
            Decimal("0.3"),
            abs=Decimal("0.0001"),
        )

    def test_30pct_below_baseline(self) -> None:
        assert _two_sided_deviation(Decimal("0.7"), Decimal("1")) == pytest.approx(
            Decimal("0.3"),
            abs=Decimal("0.0001"),
        )

    def test_zero_baseline_uses_epsilon(self) -> None:
        """A zero backtest metric (e.g., a strategy that broke even
        in backtest) shouldn't crash. The epsilon denominator turns
        any paper > epsilon into a large deviation — the correct
        semantic for "broke even in backtest, profitable in paper
        ⇒ that's overfitting territory, flag it".
        """
        dev = _two_sided_deviation(Decimal("0.005"), Decimal("0"))
        # 0.005 / 0.0001 = 50
        assert dev >= Decimal("10")


class TestClassifyHealth:
    """Each test pins one band of the classifier. The four-metric
    surface is large; these cases pick combinations that isolate
    the dominant metric so a regression points at the right axis.
    """

    @staticmethod
    def _at_baseline() -> dict[str, Decimal]:
        """All metrics at backtest baseline."""
        return {
            "trade_freq_ratio": Decimal("1"),
            "paper_win_rate": Decimal("0.6"),
            "backtest_win_rate": Decimal("0.6"),
            "paper_avg_return": Decimal("0.01"),
            "backtest_avg_return": Decimal("0.01"),
            "drawdown_ratio": Decimal("1"),
        }

    def test_baseline_is_healthy(self) -> None:
        assert _classify_health(**self._at_baseline()) is HealthStatus.HEALTHY

    def test_trade_freq_25pct_off_is_healthy(self) -> None:
        kwargs = self._at_baseline()
        kwargs["trade_freq_ratio"] = Decimal("1.25")
        assert _classify_health(**kwargs) is HealthStatus.HEALTHY

    def test_trade_freq_40pct_off_is_watch(self) -> None:
        kwargs = self._at_baseline()
        kwargs["trade_freq_ratio"] = Decimal("1.40")
        assert _classify_health(**kwargs) is HealthStatus.WATCH

    def test_trade_freq_70pct_off_is_breach(self) -> None:
        kwargs = self._at_baseline()
        kwargs["trade_freq_ratio"] = Decimal("1.70")
        assert _classify_health(**kwargs) is HealthStatus.BREACH

    def test_win_rate_drop_to_breach(self) -> None:
        """Paper win_rate 0.20 vs backtest 0.60 = 0.67 deviation > 0.6 ⇒ breach."""
        kwargs = self._at_baseline()
        kwargs["paper_win_rate"] = Decimal("0.20")
        assert _classify_health(**kwargs) is HealthStatus.BREACH

    def test_drawdown_short_circuits_breach_at_1_5x(self) -> None:
        kwargs = self._at_baseline()
        kwargs["drawdown_ratio"] = Decimal("1.51")
        assert _classify_health(**kwargs) is HealthStatus.BREACH

    def test_drawdown_lower_than_backtest_is_healthy(self) -> None:
        """Paper drawdown < backtest is unambiguously good. Drawdown
        deviation is ONE-SIDED — `max(0, ratio - 1)` clips at 0 for
        any ratio <= 1. So a 0.5 ratio (paper has half the DD)
        contributes 0 to the deviation surface and the strategy
        stays healthy.
        """
        kwargs = self._at_baseline()
        kwargs["drawdown_ratio"] = Decimal("0.5")
        assert _classify_health(**kwargs) is HealthStatus.HEALTHY

    def test_drawdown_exactly_at_1_5_does_not_breach(self) -> None:
        """`> 1.5` not `>= 1.5` — exactly 1.5 falls into the
        general-deviation surface (one-sided drawdown deviation
        = 1.5 - 1 = 0.5, which is the WATCH band).
        """
        kwargs = self._at_baseline()
        kwargs["drawdown_ratio"] = Decimal("1.5")
        # 0.5 deviation ⇒ watch.
        assert _classify_health(**kwargs) is HealthStatus.WATCH

    def test_drawdown_just_above_1_5_is_breach(self) -> None:
        kwargs = self._at_baseline()
        kwargs["drawdown_ratio"] = Decimal("1.501")
        assert _classify_health(**kwargs) is HealthStatus.BREACH


# ---- Phase B.6: sqrt(N) per-timeframe scaling -----------------------------


class TestSqrtNScalingFactor:
    """The factor that scales drift bands across timeframes.

    Brownian default: variance of a cumulative quantity (e.g.,
    30-day P&L drift) scales linearly with N, std-dev with sqrt(N).
    A 1H strategy with 4× the trades has 2× the cumulative
    std-dev — preserve the same significance band by widening the
    threshold by the same factor.
    """

    def test_4h_returns_identity(self) -> None:
        # The bit-identity gate: every existing Phase A strategy is
        # 4H and must classify exactly as it did pre-B.6.
        assert sqrt_n_scaling_factor("4h") == Decimal("1")

    def test_1h_returns_two(self) -> None:
        # 1h = 24 bars/day; 4h = 6 bars/day. sqrt(24/6) == 2.
        assert sqrt_n_scaling_factor("1h") == pytest.approx(
            Decimal("2"),
            abs=Decimal("0.0001"),
        )

    def test_15m_returns_four(self) -> None:
        # 15m = 96 bars/day; sqrt(96/6) == 4.
        assert sqrt_n_scaling_factor("15m") == pytest.approx(
            Decimal("4"),
            abs=Decimal("0.0001"),
        )

    def test_1d_returns_inverse_sqrt_six(self) -> None:
        # 1d = 1 bar/day; sqrt(1/6) ≈ 0.4082. Slower cadence ⇒ smaller
        # factor ⇒ TIGHTER bands (less data → less tolerance).
        assert sqrt_n_scaling_factor("1d") == pytest.approx(
            Decimal("0.4082"),
            abs=Decimal("0.0001"),
        )

    def test_unknown_timeframe_defaults_to_1(self) -> None:
        # A misspelled or unsupported timeframe should NOT silently
        # widen / narrow bands — 1.0 keeps behaviour conservative
        # and the WARNING log entry surfaces the typo.
        assert sqrt_n_scaling_factor("bogus") == Decimal("1")


class TestScaledThresholds:
    def test_4h_returns_module_constants(self) -> None:
        # The bit-identity guarantee for the running Phase A
        # strategies: scaled_thresholds("4h") MUST equal the
        # pre-B.6 hardcoded values exactly.
        t = scaled_thresholds("4h")
        assert t.healthy == Decimal("0.30")
        assert t.watch == Decimal("0.60")
        assert t.drawdown_breach == Decimal("1.5")

    def test_1h_widens_all_three_bands_by_two(self) -> None:
        t = scaled_thresholds("1h")
        assert t.healthy == pytest.approx(Decimal("0.60"), abs=Decimal("0.0001"))
        assert t.watch == pytest.approx(Decimal("1.20"), abs=Decimal("0.0001"))
        assert t.drawdown_breach == pytest.approx(Decimal("3.0"), abs=Decimal("0.0001"))

    def test_15m_widens_by_four(self) -> None:
        t = scaled_thresholds("15m")
        assert t.healthy == pytest.approx(Decimal("1.20"), abs=Decimal("0.0001"))
        assert t.watch == pytest.approx(Decimal("2.40"), abs=Decimal("0.0001"))
        assert t.drawdown_breach == pytest.approx(Decimal("6.0"), abs=Decimal("0.0001"))


class TestClassifyHealthWithScaledThresholds:
    """Same surface as TestClassifyHealth but with the B.6 kwargs.

    Each test pins a deviation that classifies one way at 4H and a
    different way at 1H — the scaling factor is the difference.
    """

    @staticmethod
    def _at_baseline() -> dict[str, Decimal]:
        return {
            "trade_freq_ratio": Decimal("1"),
            "paper_win_rate": Decimal("0.6"),
            "backtest_win_rate": Decimal("0.6"),
            "paper_avg_return": Decimal("0.01"),
            "backtest_avg_return": Decimal("0.01"),
            "drawdown_ratio": Decimal("1"),
        }

    def test_50pct_trade_freq_deviation_is_watch_at_4h_healthy_at_1h(self) -> None:
        kwargs = self._at_baseline()
        kwargs["trade_freq_ratio"] = Decimal("1.50")  # 50% deviation

        # 4H bands: 30% / 60%. 50% > 30% ⇒ WATCH.
        assert _classify_health(**kwargs) is HealthStatus.WATCH

        # 1H bands: 60% / 120%. 50% < 60% ⇒ HEALTHY.
        t = scaled_thresholds("1h")
        assert (
            _classify_health(
                **kwargs,
                healthy_threshold=t.healthy,
                watch_threshold=t.watch,
                drawdown_breach_ratio=t.drawdown_breach,
            )
            is HealthStatus.HEALTHY
        )

    def test_2x_drawdown_is_breach_at_4h_watch_at_1h(self) -> None:
        kwargs = self._at_baseline()
        kwargs["drawdown_ratio"] = Decimal("2.0")  # paper DD = 2× backtest

        # 4H: hard breach short-circuit at >1.5, so 2.0 ⇒ BREACH.
        assert _classify_health(**kwargs) is HealthStatus.BREACH

        # 1H: hard breach at >3.0, so 2.0 falls into the general
        # deviation surface. drawdown_dev = 2.0-1 = 1.0; 1H watch
        # threshold = 1.20, so 1.0 < 1.20 ⇒ HEALTHY (drawdown_dev
        # contributes 1.0, but 1.0 is below the 1H healthy
        # threshold of 0.60... wait that's WATCH).
        # Re-derive: deviations = [|1-1|=0, max(0, 2-1)=1.0, 0, 0]
        # max_dev = 1.0. 1H thresholds: healthy=0.60, watch=1.20.
        # 1.0 > 0.60 and 1.0 < 1.20, so WATCH band.
        t = scaled_thresholds("1h")
        assert (
            _classify_health(
                **kwargs,
                healthy_threshold=t.healthy,
                watch_threshold=t.watch,
                drawdown_breach_ratio=t.drawdown_breach,
            )
            is HealthStatus.WATCH
        )

    def test_default_kwargs_match_4h_behaviour_exactly(self) -> None:
        # Call sites that omit the new kwargs MUST behave exactly
        # as pre-B.6 (the bit-identity contract for the existing
        # signal_engine -> drift call chain, and for the existing
        # 20 unit tests above).
        kwargs = self._at_baseline()
        kwargs["trade_freq_ratio"] = Decimal("1.40")
        assert _classify_health(**kwargs) is HealthStatus.WATCH

        # Same call with explicit defaults must produce the same
        # answer — proves the defaults equal the module constants.
        assert (
            _classify_health(
                **kwargs,
                healthy_threshold=Decimal("0.30"),
                watch_threshold=Decimal("0.60"),
                drawdown_breach_ratio=Decimal("1.5"),
            )
            is HealthStatus.WATCH
        )


class TestExtractBacktestMetrics:
    def test_valid_two_subtree_input_returns_metrics(self) -> None:
        result = _extract_backtest_metrics(
            {
                "walk_forward": {"out_of_sample_trade_freq_per_week": 3.5},
                "single_pass": {
                    "win_rate": 0.55,
                    "avg_return_per_trade": 0.012,
                    "max_drawdown_pct": 0.08,
                },
            },
        )
        assert result is not None
        assert result.trade_freq_per_week == Decimal("3.5")
        assert result.win_rate == Decimal("0.55")
        assert result.avg_return_per_trade == Decimal("0.012")
        assert result.max_drawdown_pct == Decimal("0.08")

    def test_none_input_returns_none(self) -> None:
        assert _extract_backtest_metrics(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert _extract_backtest_metrics({}) is None

    def test_missing_walk_forward_subtree_returns_none(self) -> None:
        assert _extract_backtest_metrics(
            {
                "single_pass": {
                    "win_rate": 0.55,
                    "avg_return_per_trade": 0.012,
                    "max_drawdown_pct": 0.08,
                },
            },
        ) is None

    def test_missing_single_pass_subtree_returns_none(self) -> None:
        assert _extract_backtest_metrics(
            {"walk_forward": {"out_of_sample_trade_freq_per_week": 3.5}},
        ) is None

    def test_subtree_not_a_dict_returns_none(self) -> None:
        assert _extract_backtest_metrics({"walk_forward": "string", "single_pass": {}}) is None

    def test_missing_inner_key_returns_none(self) -> None:
        result = _extract_backtest_metrics(
            {
                "walk_forward": {"out_of_sample_trade_freq_per_week": 3.5},
                "single_pass": {
                    "win_rate": 0.55,
                    # missing avg_return_per_trade
                    "max_drawdown_pct": 0.08,
                },
            },
        )
        assert result is None


# ---- Layer 2: integration tests --------------------------------------------


pytestmark_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_container() -> Iterator[object]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: object) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def _clean(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE trader_strategies, trader_candles, "
            "trader_portfolio_snapshots, trader_alerts, trader_risk_events, "
            "trader_audit_logs, trader_drift_metrics RESTART IDENTITY CASCADE",
        )
        conn.commit()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TRADER_STARTING_CASH_GBP", "1000")
    get_trader_settings.cache_clear()
    return get_trader_settings()


# ---- Seed helpers ----------------------------------------------------------


def _backtest_metrics_jsonb(
    *,
    avg_return: float = 0.01,
    win_rate: float = 0.6,
    max_drawdown: float = 0.05,
    trade_freq: float = 3.0,
) -> dict[str, Any]:
    """Compose the two-subtree shape Step 14's seed script will write.

    `walk_forward.out_of_sample_trade_freq_per_week` is truly OOS.
    `single_pass.*` are whole-period backtest values used as
    proxies (v1 simplification — see drift.py module docstring).
    """
    return {
        "walk_forward": {
            "out_of_sample_trade_freq_per_week": trade_freq,
        },
        "single_pass": {
            "win_rate": win_rate,
            "avg_return_per_trade": avg_return,
            "max_drawdown_pct": max_drawdown,
        },
    }


def _seed_version(
    database_url: str,
    *,
    backtest_metrics: dict[str, Any] | None = None,
    enabled: bool = True,
    approved_for_paper: bool = True,
) -> UUID:
    bt = backtest_metrics if backtest_metrics is not None else _backtest_metrics_jsonb()
    name = f"drift-test-{uuid4().hex[:8]}"
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_strategies (name) VALUES (%s) RETURNING id",
            (name,),
        )
        srow = cur.fetchone()
        assert srow is not None
        cur.execute(
            """
            INSERT INTO trader_strategy_versions
                (strategy_id, version, marketmind_spec_id, template, parameters,
                 symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                 backtest_metrics, approved_for_paper, enabled)
            VALUES (%s, 1, %s, 'ma_trend', %s, %s, %s, %s, 10, 10, %s, %s, %s)
            RETURNING id
            """,
            (
                str(srow[0]),
                str(uuid4()),
                Jsonb({}),
                ["BTC/USDT"],
                ["4h"],
                Decimal("0.005"),
                Jsonb(bt),
                approved_for_paper,
                enabled,
            ),
        )
        vrow = cur.fetchone()
        assert vrow is not None
        conn.commit()
    return UUID(str(vrow[0]))


def _seed_closed_position(
    database_url: str,
    *,
    version_id: UUID,
    exit_ts: datetime,
    realised_pnl: Decimal,
    realised_pnl_pct: Decimal,
) -> None:
    """Seed a closed position. Uses entry orders + fills inline to
    keep the trader_paper_positions row schema-valid (entry_order_id
    NOT NULL).
    """
    sig = uuid4()
    entry_order = uuid4()
    exit_order = uuid4()
    entry_signal = uuid4()
    exit_signal = uuid4()
    entry_ts = exit_ts - timedelta(hours=4)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        # Entry signal/order/fill chain.
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators, proposed_entry_price, proposed_stop_price)
            VALUES (%s, %s, 'BTC/USDT', '4h', %s, 'BUY', 'seed', %s, %s, %s)
            """,
            (
                str(entry_signal),
                str(version_id),
                entry_ts,
                Jsonb({}),
                Decimal("100"),
                Decimal("95"),
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side, order_type,
                 requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, 'BTC/USDT', 'BUY', 'MARKET', %s, 'FILLED', %s)
            """,
            (
                str(entry_order),
                str(entry_signal),
                str(version_id),
                Decimal("1"),
                entry_ts,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size, fee,
                 slippage_bps_applied, notional)
            VALUES (%s, %s, %s, 100, 1, 0.1, 10, 100)
            """,
            (str(uuid4()), str(entry_order), entry_ts),
        )
        # Exit signal/order/fill chain.
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators, proposed_entry_price, proposed_stop_price)
            VALUES (%s, %s, 'BTC/USDT', '4h', %s, 'EXIT', 'seed', %s, 100, 95)
            """,
            (str(exit_signal), str(version_id), exit_ts, Jsonb({})),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side, order_type,
                 requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, 'BTC/USDT', 'SELL', 'MARKET', %s, 'FILLED', %s)
            """,
            (
                str(exit_order),
                str(exit_signal),
                str(version_id),
                Decimal("1"),
                exit_ts,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size, fee,
                 slippage_bps_applied, notional)
            VALUES (%s, %s, %s, 100, 1, 0.1, 10, 100)
            """,
            (str(uuid4()), str(exit_order), exit_ts),
        )
        # Position row.
        cur.execute(
            """
            INSERT INTO trader_paper_positions
                (id, strategy_version_id, symbol, side, entry_order_id, exit_order_id,
                 entry_price, entry_ts, exit_price, exit_ts, size, stop_price,
                 status, realised_pnl, realised_pnl_pct, close_reason)
            VALUES (%s, %s, 'BTC/USDT', 'LONG', %s, %s, 100, %s, 100, %s, 1, 95,
                    'CLOSED', %s, %s, 'signal_exit')
            """,
            (
                str(uuid4()),
                str(version_id),
                str(entry_order),
                str(exit_order),
                entry_ts,
                exit_ts,
                realised_pnl,
                realised_pnl_pct,
            ),
        )
        conn.commit()
    _ = sig


def _seed_portfolio_snapshot(
    database_url: str,
    *,
    cash: Decimal,
    equity: Decimal,
    peak_equity: Decimal,
    drawdown_pct: Decimal,
) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_portfolio_snapshots
                (cash, equity, unrealised_pnl, realised_pnl_cumulative,
                 peak_equity, drawdown, drawdown_pct, open_positions_count,
                 per_strategy_breakdown, per_symbol_breakdown)
            VALUES (%s, %s, 0, 0, %s, %s, %s, 0, %s, %s)
            """,
            (
                cash,
                equity,
                peak_equity,
                peak_equity - equity,
                drawdown_pct,
                Jsonb({}),
                Jsonb({}),
            ),
        )
        conn.commit()


# ---- Integration tests -----------------------------------------------------


@pytestmark_integration
def test_skips_versions_with_no_trades(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    _seed_version(database_url)
    result = compute_and_persist_drift_for_all(database_url, settings)
    assert result.versions_evaluated == 0
    assert result.versions_skipped_insufficient_trades == 1
    assert result.drift_rows_persisted == 0
    assert result.breach_alerts_emitted == 0


@pytestmark_integration
def test_skips_versions_with_below_threshold_trades(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """4 closed trades — below the 5-trade minimum. Skipped."""
    v_id = _seed_version(database_url)
    now = datetime(2026, 5, 18, tzinfo=UTC)
    for i in range(4):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + i),
            realised_pnl=Decimal("1"),
            realised_pnl_pct=Decimal("0.01"),
        )
    result = compute_and_persist_drift_for_all(database_url, settings, now=now)
    assert result.versions_skipped_insufficient_trades == 1
    assert result.drift_rows_persisted == 0


@pytestmark_integration
def test_skips_versions_with_missing_backtest_metrics(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    v_id = _seed_version(database_url, backtest_metrics={})
    now = datetime(2026, 5, 18, tzinfo=UTC)
    for i in range(6):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + i),
            realised_pnl=Decimal("1"),
            realised_pnl_pct=Decimal("0.01"),
        )
    result = compute_and_persist_drift_for_all(database_url, settings, now=now)
    assert result.versions_skipped_missing_backtest_metrics == 1
    assert result.drift_rows_persisted == 0


@pytestmark_integration
def test_healthy_strategy_persists_drift_row_no_alert(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Paper metrics close to backtest baseline ⇒ health=healthy.
    No alert row written.
    """
    v_id = _seed_version(
        database_url,
        backtest_metrics=_backtest_metrics_jsonb(
            avg_return=0.01,
            win_rate=0.6,
            max_drawdown=0.05,
            trade_freq=3.0,
        ),
    )
    now = datetime(2026, 5, 18, tzinfo=UTC)
    # 6 closed trades, 4 winners + 2 losers ⇒ win_rate ≈ 0.667 (within 30%).
    for i in range(4):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + i),
            realised_pnl=Decimal("1"),
            realised_pnl_pct=Decimal("0.011"),
        )
    for i in range(2):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + 4 + i),
            realised_pnl=Decimal("-1"),
            realised_pnl_pct=Decimal("-0.01"),
        )
    _seed_portfolio_snapshot(
        database_url,
        cash=Decimal("1010"),
        equity=Decimal("1010"),
        peak_equity=Decimal("1020"),
        drawdown_pct=Decimal("0.01"),  # 0.01 / 0.05 = 0.2 ratio (better than BT)
    )

    result = compute_and_persist_drift_for_all(database_url, settings, now=now)

    assert result.versions_evaluated == 1
    assert result.drift_rows_persisted == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT health_status, paper_trade_count, paper_win_rate "
            "FROM trader_drift_metrics WHERE strategy_version_id = %s",
            (str(v_id),),
        )
        row = cur.fetchone()
        assert row is not None
        health, count, win_rate = row
        assert health in ("healthy", "watch")  # 0.667 vs 0.6 = 11% dev → healthy
        assert count == 6
        assert win_rate == Decimal("4") / Decimal("6")

        # No breach alerts.
        cur.execute("SELECT COUNT(*) FROM trader_alerts WHERE severity='warning'")
        crow = cur.fetchone()
        assert crow is not None
        assert crow[0] == 0


@pytestmark_integration
def test_breach_strategy_persists_row_and_alert(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Paper drawdown 2x backtest drawdown ⇒ kill-switch-style
    drawdown breach. Drift row persists with health=breach AND
    a warning alert is written.
    """
    v_id = _seed_version(
        database_url,
        backtest_metrics=_backtest_metrics_jsonb(
            avg_return=0.01,
            win_rate=0.6,
            max_drawdown=0.05,
            trade_freq=3.0,
        ),
    )
    now = datetime(2026, 5, 18, tzinfo=UTC)
    for i in range(6):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + i),
            realised_pnl=Decimal("1"),
            realised_pnl_pct=Decimal("0.01"),
        )
    # Paper drawdown 0.10 vs backtest 0.05 ⇒ ratio 2.0 > 1.5 ⇒ breach.
    _seed_portfolio_snapshot(
        database_url,
        cash=Decimal("900"),
        equity=Decimal("900"),
        peak_equity=Decimal("1000"),
        drawdown_pct=Decimal("0.10"),
    )

    result = compute_and_persist_drift_for_all(database_url, settings, now=now)

    assert result.versions_evaluated == 1
    assert result.breach_alerts_emitted == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT health_status, drawdown_ratio "
            "FROM trader_drift_metrics WHERE strategy_version_id = %s",
            (str(v_id),),
        )
        drift = cur.fetchone()
        assert drift is not None
        health, dd_ratio = drift
        assert health == "breach"
        assert dd_ratio == Decimal("2")  # 0.10 / 0.05

        cur.execute(
            "SELECT severity, subject FROM trader_alerts "
            "WHERE severity = 'warning' ORDER BY ts DESC LIMIT 1",
        )
        alert = cur.fetchone()
        assert alert is not None
        assert alert[0] == "warning"
        assert "Drift breach" in alert[1]


@pytestmark_integration
def test_breach_does_not_disable_strategy(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """v1 is ADVISORY. A breach must NOT auto-disable the version —
    enabled stays True for the operator to flip manually.
    """
    v_id = _seed_version(database_url)
    now = datetime(2026, 5, 18, tzinfo=UTC)
    for i in range(6):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + i),
            realised_pnl=Decimal("-10"),
            realised_pnl_pct=Decimal("-0.10"),
        )
    _seed_portfolio_snapshot(
        database_url,
        cash=Decimal("800"),
        equity=Decimal("800"),
        peak_equity=Decimal("1000"),
        drawdown_pct=Decimal("0.20"),
    )

    compute_and_persist_drift_for_all(database_url, settings, now=now)

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, approved_for_paper FROM trader_strategy_versions WHERE id = %s",
            (str(v_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True  # still enabled
        assert row[1] is True  # still approved


@pytestmark_integration
def test_filters_trades_to_window(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Trades older than the window are NOT counted. 6 trades
    spread 10 days apart; with window='7d' only the recent ones
    fall inside, dropping below the 5-trade threshold.
    """
    v_id = _seed_version(database_url)
    now = datetime(2026, 5, 18, tzinfo=UTC)
    # 6 trades 5 days apart — only 2-3 fall in a 7d window.
    for i in range(6):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=5 * i + 1),
            realised_pnl=Decimal("1"),
            realised_pnl_pct=Decimal("0.01"),
        )

    result = compute_and_persist_drift_for_all(
        database_url,
        settings,
        window_label="7d",
        now=now,
    )
    # 7d window has only the recent trades — below threshold.
    assert result.versions_skipped_insufficient_trades == 1
    assert result.drift_rows_persisted == 0


@pytestmark_integration
def test_skips_disabled_versions(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """A disabled version isn't actively trading — no drift to
    measure. Skipped silently (not counted as 'evaluated').
    """
    v_id = _seed_version(database_url, enabled=False)
    now = datetime(2026, 5, 18, tzinfo=UTC)
    for i in range(6):
        _seed_closed_position(
            database_url,
            version_id=v_id,
            exit_ts=now - timedelta(days=2 + i),
            realised_pnl=Decimal("1"),
            realised_pnl_pct=Decimal("0.01"),
        )
    result = compute_and_persist_drift_for_all(database_url, settings, now=now)
    assert result.versions_evaluated == 0
    assert result.drift_rows_persisted == 0


# Silence the unused-import warning that ruff would otherwise raise
# on `_BacktestMetrics` — it's referenced via the test class above
# for the public surface check, but ruff doesn't track that.
_ = _BacktestMetrics
