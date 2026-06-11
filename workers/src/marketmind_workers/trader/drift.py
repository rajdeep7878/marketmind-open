"""Trader v1 drift analyzer.

`compute_and_persist_drift_for_all(database_url, settings, *,
window_label, now)` runs daily. For every enabled + paper-approved
strategy version with at least `_MIN_PAPER_TRADES` CLOSED paper
trades in the window, compute four paper metrics, compare against
the backtest's WALK-FORWARD OUT-OF-SAMPLE equivalents loaded from
`trader_strategy_versions.backtest_metrics`, classify a health
status, and insert a `trader_drift_metrics` row. Breach health
emits a warning-severity `trader_alerts` row (Step 10 delivers to
Telegram).

V1 IS ADVISORY ONLY: a breach does NOT auto-disable the strategy.
The operator manually disables via the admin API (Step 11).

TRADE-COUNTING PARITY (load-bearing — must match backtest exactly)
==================================================================
Both sides count trades as ROUND-TRIPS — entry+exit = one trade.
Verified at Step 8 verification:

  Backtest (`workers/backtest/metrics.py`):
    trades come from `vbt.Portfolio.trades.records_readable` — vbt's
    round-trip records, not individual fills.
    `num_trades = len(trades)`
    `win_rate  = sum(t.return_pct > 0 for t in trades) / num_trades`

  Trader (this module):
    trades = CLOSED rows in `trader_paper_positions` — one row per
    round-trip (the entry order opens the position; the exit order
    or stop-hit closes it).
    `paper_trade_count = COUNT(*) WHERE status='CLOSED' AND exit_ts >= window_start`
    `paper_win_rate    = COUNT(realised_pnl > 0) / paper_trade_count`

A units mismatch here (fills vs round-trips) would be ~2x off and
flag every healthy strategy as decaying. The convention is
re-confirmed in this docstring so any future refactor confronts it.

EXPECTED `backtest_metrics` SHAPE
---------------------------------
The snapshot a strategy version carries on approval. Step 14's
seed script composes this from two MarketMind sources because
the walk-forward worker discards per-trade-level OOS data (see
the V1 SIMPLIFICATION block below)::

    {
      "walk_forward": {
        "out_of_sample_trade_freq_per_week": float,   # truly OOS, derived from windows
      },
      "single_pass": {
        "win_rate":              float,  # whole-period backtest, 0..1
        "avg_return_per_trade":  float,  # whole-period mean trade return %
        "max_drawdown_pct":      float,  # whole-period max DD, 0..1
      }
    }

Missing keys ⇒ the version is skipped (`versions_skipped_missing_backtest_metrics`
stat bumps; no row inserted; no alert). The Step 11 admin
endpoint `POST /trader/strategies/{id}/approve_paper` validates
this subtree's presence at approval time, so a paper-approved
version should always have it. The skip path is a defensive guard.

STEP 14 SEED-SCRIPT TRANSLATION (load-bearing — pin this)
---------------------------------------------------------
The seed script composes the JSONB from two MarketMind sources::

    from overfitting_analysis.walk_forward.windows:
        total_oos_trades = sum(w.out_of_sample_num_trades for w in windows)
        total_oos_days   = sum(
            (w.out_of_sample_end - w.out_of_sample_start).total_seconds() / 86400.0
            for w in windows
        )
        oos_trade_freq_per_week = (
            total_oos_trades / (total_oos_days / 7.0)
            if total_oos_days > 0 else 0.0
        )

    from backtest_result.metrics (single-pass, whole-period):
        win_rate             = metrics.win_rate
        avg_return_per_trade = metrics.expectancy
        max_drawdown_pct     = metrics.max_drawdown_pct

V1 SIMPLIFICATION (and why):
The walk-forward worker discards per-segment trade-level data —
`_run_segment` returns only `(total_return_pct, sharpe, num_trades)`
per OOS window. To get OOS-only win_rate / avg_return_per_trade /
max_drawdown, the seed script would need to re-run per-OOS-window
backtests in a custom mode. Out of scope for v1.

The trade-frequency comparison stays truly OOS (the seed script
derives it from per-window OOS dates and trade counts). The other
three comparisons use whole-period (single-pass) backtest stats
as a proxy. This is a known approximation: a strategy that
overfit-decayed badly between in-sample and out-of-sample will
have win_rate / avg_return / max_drawdown numbers that
flatter the strategy compared to what live paper sees. The drift
analyzer is therefore more LENIENT on these three than a strict
OOS comparison would be — drift breaches that fire are confident
breaches; drift health=healthy on a poorly-walk-forward-tested
strategy could still be hiding decay. v2 should run per-OOS-window
trade-level extraction in the seed script.

HEALTH CLASSIFICATION
---------------------
trade_freq, win_rate, avg_return — TWO-SIDED deviation:

    deviation = |paper - backtest| / max(|backtest|, ε)

  A paper value blowing past backtest in either direction is
  notable. Suspiciously HIGHER win_rate / avg_return than backtest
  usually means noise, overfitting reveal, or regime change —
  worth investigating regardless of sign.

drawdown — ONE-SIDED deviation:

    deviation = max(0, drawdown_ratio - 1)

  Paper drawdown LOWER than backtest is unambiguously good and
  contributes 0 to the deviation surface. Paper drawdown higher
  than backtest scales linearly until the hard `> 1.5` breach
  short-circuits the bands entirely.

    healthy: all metrics within 30% deviation
    watch:   any metric in (30%, 60%]
    breach:  any metric > 60%  OR  drawdown_ratio > 1.5

The boundary is `>` not `>=`: a metric exactly at 30% (or 60%)
stays in the lower band. Symmetric with the kill-switch
boundary semantics in Step 6's risk manager.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import psycopg
import structlog
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.trader import HealthStatus
from marketmind_shared.trader.money import to_decimal
from marketmind_shared.trader.time import now_utc

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.heartbeat import touch_heartbeat

log = structlog.get_logger(__name__)


# ---- Tunables --------------------------------------------------------------

_MIN_PAPER_TRADES: Final[int] = 5
# The base deviation thresholds were tuned for 4H. Phase B.6 (2026-05-23)
# adds per-timeframe scaling via ``sqrt_n_scaling_factor()`` so 1H / 15m
# / 1d strategies don't get unfairly classified by 4H-tuned bands. At 4H
# the factor is exactly 1.0, so existing 4H behaviour is bit-identical.
_HEALTHY_DEVIATION_THRESHOLD: Final[Decimal] = Decimal("0.30")
_WATCH_DEVIATION_THRESHOLD: Final[Decimal] = Decimal("0.60")
_DRAWDOWN_BREACH_RATIO: Final[Decimal] = Decimal("1.5")
# Epsilon for the denominator in `_two_sided_deviation`. Guards
# against div-by-zero when a backtest metric is 0 (e.g.,
# avg_return_per_trade for a strategy that broke even). 0.0001
# means a paper value of 0.001 against backtest 0 reads as
# deviation 10 (definitely a breach), which is the correct
# semantic. Not timeframe-scaled — purely numerical.
_DEVIATION_EPSILON: Final[Decimal] = Decimal("0.0001")

# ---- Phase B.6: per-timeframe threshold scaling ---------------------------
#
# Approximate "bars per day" for the timeframes the trader supports. Used
# as the input to a sqrt(N) Brownian scaling of the deviation thresholds
# (see ``sqrt_n_scaling_factor`` and ``scaled_thresholds`` below). 4H is
# the baseline — every factor is relative to it.
_BARS_PER_DAY: Final[dict[str, float]] = {
    "1m": 60.0 * 24.0,
    "5m": 12.0 * 24.0,
    "15m": 4.0 * 24.0,
    "30m": 2.0 * 24.0,
    "1h": 24.0,
    "4h": 6.0,
    "1d": 1.0,
}

# 4H is the baseline TF the drift bands were originally tuned for. Any
# new strategy at 4H gets the same thresholds as the existing seeded
# Phase A strategies — bit-identical regression guarantee.
_BASELINE_TIMEFRAME: Final[str] = "4h"


def sqrt_n_scaling_factor(timeframe: str) -> Decimal:
    """Return the sqrt(N) Brownian scaling factor for ``timeframe``,
    relative to the 4H baseline.

    factor(tf) = sqrt( bars_per_day(tf) / bars_per_day(4h) )

    Examples (4H baseline = 6 bars/day)::

        sqrt_n_scaling_factor("4h")  == 1.0      (identity)
        sqrt_n_scaling_factor("1h")  == 2.0      (sqrt(24/6))
        sqrt_n_scaling_factor("15m") == 4.0      (sqrt(96/6))
        sqrt_n_scaling_factor("1d")  ≈ 0.4082    (sqrt(1/6))

    The factor is multiplied into the base deviation thresholds — so
    higher-cadence strategies get *wider* tolerance bands. The
    reasoning is a Brownian-motion analogy: a cumulative quantity
    (e.g., 30-day P&L drift) accumulates std-dev as ~sqrt(N), so the
    same statistical-significance band scales the same way.

    The drift analyzer's metrics (win_rate, avg_return) are
    per-trade averages, not cumulative quantities — so for those the
    Brownian scaling is intentionally CONSERVATIVE (a 1H strategy
    has ~half the per-trade-mean noise of a 4H strategy with the
    same trade count, so the sqrt(N)-widened threshold is roomier
    than statistically necessary). This is deliberate: we'd rather
    have a quiet drift analyzer at first deployment of a new
    timeframe and tighten empirically (Phase B.7+) than false-fire
    on noisy initial samples.

    Unknown timeframes return 1.0 (4H baseline) — a defensive default
    that keeps a misspelled timeframe from accidentally widening or
    tightening thresholds invisibly.
    """
    if timeframe == _BASELINE_TIMEFRAME:
        return Decimal("1")
    tf_bpd = _BARS_PER_DAY.get(timeframe)
    if tf_bpd is None:
        log.warning(
            "drift_unknown_timeframe_defaulting_to_4h_factor",
            timeframe=timeframe,
        )
        return Decimal("1")
    baseline_bpd = _BARS_PER_DAY[_BASELINE_TIMEFRAME]
    raw = math.sqrt(tf_bpd / baseline_bpd)
    return to_decimal(raw)


@dataclass(frozen=True)
class _ScaledThresholds:
    healthy: Decimal
    watch: Decimal
    drawdown_breach: Decimal


def scaled_thresholds(timeframe: str) -> _ScaledThresholds:
    """Return the deviation-band thresholds scaled for ``timeframe``.

    At 4H the returned values are exactly the module constants — the
    4H bit-identity gate. At higher cadences (1h, 15m, ...) the
    bands widen by ``sqrt_n_scaling_factor(tf)``; at 1d they
    narrow by the same factor inverted. ``_MIN_PAPER_TRADES`` and
    ``_DEVIATION_EPSILON`` do NOT scale — the former is a
    sample-size floor (statistically meaningful regardless of how
    long it took to accumulate), the latter is a numerical guard.
    """
    factor = sqrt_n_scaling_factor(timeframe)
    return _ScaledThresholds(
        healthy=_HEALTHY_DEVIATION_THRESHOLD * factor,
        watch=_WATCH_DEVIATION_THRESHOLD * factor,
        drawdown_breach=_DRAWDOWN_BREACH_RATIO * factor,
    )

_DEFAULT_WINDOW_LABEL: Final[str] = "30d"
_WINDOW_LABEL_TO_DAYS: Final[dict[str, int | None]] = {
    "7d": 7,
    "30d": 30,
    "all": None,
}


# ---- Value types -----------------------------------------------------------


@dataclass(frozen=True)
class _PaperMetrics:
    trade_count: int
    win_rate: Decimal
    avg_return_per_trade: Decimal
    trade_freq_per_week: Decimal
    current_drawdown_pct: Decimal


@dataclass(frozen=True)
class _BacktestMetrics:
    """The four OOS values the drift analyzer needs."""

    avg_return_per_trade: Decimal
    win_rate: Decimal
    max_drawdown_pct: Decimal
    trade_freq_per_week: Decimal


class DriftCycleResult(_StrictModel):
    """Aggregate stats from one drift-cycle run."""

    versions_evaluated: int = 0
    versions_skipped_insufficient_trades: int = 0
    versions_skipped_missing_backtest_metrics: int = 0
    drift_rows_persisted: int = 0
    breach_alerts_emitted: int = 0


# ---- Pure helpers ----------------------------------------------------------


def _two_sided_deviation(paper: Decimal, backtest: Decimal) -> Decimal:
    """``|paper - backtest| / max(|backtest|, ε)``.

    Used for trade_freq, win_rate, avg_return — metrics where
    BOTH sides of the baseline are notable. A paper value that
    blows past backtest in either direction is suspicious (the
    upside case is usually overfit-driven or regime-change noise).
    """
    abs_bt = abs(backtest)
    denom = abs_bt if abs_bt > _DEVIATION_EPSILON else _DEVIATION_EPSILON
    return abs(paper - backtest) / denom


def _classify_health(
    *,
    trade_freq_ratio: Decimal,
    paper_win_rate: Decimal,
    backtest_win_rate: Decimal,
    paper_avg_return: Decimal,
    backtest_avg_return: Decimal,
    drawdown_ratio: Decimal,
    healthy_threshold: Decimal = _HEALTHY_DEVIATION_THRESHOLD,
    watch_threshold: Decimal = _WATCH_DEVIATION_THRESHOLD,
    drawdown_breach_ratio: Decimal = _DRAWDOWN_BREACH_RATIO,
) -> HealthStatus:
    """Classify drift health per the prompt's bands.

    Short-circuits on the one-sided drawdown breach (paper >
    1.5x backtest at the 4H baseline). Otherwise, takes the max of
    two-sided deviations across the four metrics and bands at
    30% / 60% (4H baseline).

    Drawdown is also included in the general-deviation surface
    via `abs(ratio - 1)` so a ratio of 1.4 (paper 40% worse than
    backtest) still trips `watch` even though it doesn't reach
    the 1.5x breach threshold.

    Phase B.6 (2026-05-23): the three thresholds are kwargs with
    defaults at the 4H base values. ``compute_and_persist_drift_for_all``
    passes per-timeframe-scaled values via ``scaled_thresholds()`` —
    a 4H version gets the defaults (bit-identity gate), a 1H version
    gets 0.60 / 1.20 / 3.0 (2× wider), etc. Existing call sites that
    omit the kwargs continue to behave identically to pre-B.6.
    """
    # Hard breach: paper drawdown >> backtest drawdown.
    if drawdown_ratio > drawdown_breach_ratio:
        return HealthStatus.BREACH

    # Drawdown lower than backtest is GOOD — only positive
    # deviations count. `max(0, ratio - 1)` clips at 0 for any
    # ratio in [0, 1].
    drawdown_dev = max(Decimal("0"), drawdown_ratio - Decimal("1"))
    deviations = [
        # trade_freq_ratio: paper/backtest. deviation = |ratio - 1|
        # (two-sided — under-firing AND over-firing both matter).
        abs(trade_freq_ratio - Decimal("1")),
        # drawdown: one-sided per the module docstring.
        drawdown_dev,
        # win_rate, avg_return: two-sided around backtest baseline.
        _two_sided_deviation(paper_win_rate, backtest_win_rate),
        _two_sided_deviation(paper_avg_return, backtest_avg_return),
    ]
    max_dev = max(deviations)

    if max_dev > watch_threshold:
        return HealthStatus.BREACH
    if max_dev > healthy_threshold:
        return HealthStatus.WATCH
    return HealthStatus.HEALTHY


def _extract_backtest_metrics(
    backtest_metrics_jsonb: dict[str, Any] | None,
) -> _BacktestMetrics | None:
    """Parse the four required values from the version's
    `backtest_metrics` JSONB blob.

    Reads two subtrees:
      - `walk_forward.out_of_sample_trade_freq_per_week` (truly OOS,
        derived from per-window dates + trade counts).
      - `single_pass.{win_rate, avg_return_per_trade, max_drawdown_pct}`
        (whole-period backtest — v1 simplification, see module
        docstring).

    Returns None on any missing / mistyped value so the caller can
    skip with a `versions_skipped_missing_backtest_metrics` stat
    bump rather than crashing the cycle.
    """
    if not backtest_metrics_jsonb:
        return None
    wf = backtest_metrics_jsonb.get("walk_forward")
    sp = backtest_metrics_jsonb.get("single_pass")
    if not isinstance(wf, dict) or not isinstance(sp, dict):
        return None
    try:
        return _BacktestMetrics(
            trade_freq_per_week=to_decimal(wf["out_of_sample_trade_freq_per_week"]),
            win_rate=to_decimal(sp["win_rate"]),
            avg_return_per_trade=to_decimal(sp["avg_return_per_trade"]),
            max_drawdown_pct=to_decimal(sp["max_drawdown_pct"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _window_to_start(window_label: str, now: datetime) -> datetime | None:
    """Resolve the window's start instant. Returns None for the
    'all' window (no lower bound).
    """
    days = _WINDOW_LABEL_TO_DAYS.get(window_label)
    if days is None:
        return None
    return now - timedelta(days=days)


# ---- DB-touching helpers ---------------------------------------------------


def _load_active_versions(
    conn: psycopg.Connection[Any],
) -> list[tuple[UUID, dict[str, Any], str]]:
    """Load `(id, backtest_metrics, primary_timeframe)` for enabled +
    paper-approved versions. The drift analyzer skips disabled /
    unapproved versions — they aren't actively trading so a drift
    comparison is uninformative.

    ``primary_timeframe`` is the version's first declared timeframe.
    Per Phase B design Q3, each version is single-timeframe in v2,
    so ``timeframes[0]`` is unambiguous; an empty list (defensively
    handled) falls back to the 4H baseline.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, backtest_metrics, timeframes
            FROM trader_strategy_versions
            WHERE enabled = TRUE AND approved_for_paper = TRUE
            ORDER BY id
            """,
        )
        rows = cur.fetchall()
    out: list[tuple[UUID, dict[str, Any], str]] = []
    for row in rows:
        timeframes = list(row[2]) if row[2] else []
        primary_tf = timeframes[0] if timeframes else _BASELINE_TIMEFRAME
        out.append((UUID(str(row[0])), dict(row[1] or {}), primary_tf))
    return out


def _compute_paper_metrics(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    window_start: datetime | None,
    window_days: int | None,
) -> _PaperMetrics:
    """Compute trade_count, win_rate, avg_return_per_trade,
    trade_freq_per_week, current_drawdown_pct for one version.

    `window_start` is None for the 'all' window. `window_days`
    drives the trades/week calculation when bounded; for 'all',
    we infer the duration from the earliest exit_ts in the data.

    current_drawdown_pct: v1 uses the OVERALL portfolio
    drawdown_pct from the latest snapshot, not per-strategy.
    Documented in the module docstring as a v1 simplification —
    per-strategy drawdown trajectory would require walking
    snapshot history.
    """
    where_clauses = ["strategy_version_id = %s", "status = 'CLOSED'"]
    params: list[Any] = [str(version_id)]
    if window_start is not None:
        where_clauses.append("exit_ts >= %s")
        params.append(window_start)

    with conn.cursor() as cur:
        query = (
            "SELECT realised_pnl, realised_pnl_pct, exit_ts "
            "FROM trader_paper_positions WHERE " + " AND ".join(where_clauses)
        )
        cur.execute(query, params)  # type: ignore[arg-type]
        rows = cur.fetchall()

    trade_count = len(rows)
    if trade_count == 0:
        empty = Decimal("0")
        return _PaperMetrics(
            trade_count=0,
            win_rate=empty,
            avg_return_per_trade=empty,
            trade_freq_per_week=empty,
            current_drawdown_pct=_portfolio_drawdown(conn),
        )

    pnls: list[Decimal] = []
    returns: list[Decimal] = []
    exit_times: list[datetime] = []
    for realised_pnl, realised_pnl_pct, exit_ts in rows:
        pnls.append(to_decimal(realised_pnl if realised_pnl is not None else 0))
        returns.append(
            to_decimal(realised_pnl_pct if realised_pnl_pct is not None else 0),
        )
        if exit_ts is not None:
            exit_times.append(exit_ts)

    wins = sum(1 for p in pnls if p > Decimal("0"))
    win_rate = Decimal(wins) / Decimal(trade_count)

    return_sum = sum(returns, start=Decimal("0"))
    avg_return = return_sum / Decimal(trade_count)

    # trade_freq_per_week:
    # - bounded window: trade_count / (window_days / 7)
    # - 'all' window:   trade_count / (span_days / 7), where span_days
    #                   is from earliest exit_ts to latest exit_ts (or 1)
    if window_days is not None:
        weeks = Decimal(window_days) / Decimal("7")
    else:
        if exit_times:
            span_days = max(
                (max(exit_times) - min(exit_times)).total_seconds() / 86400.0,
                1.0,
            )
        else:
            span_days = 1.0
        weeks = to_decimal(span_days / 7.0)
    trade_freq_per_week = Decimal(trade_count) / weeks if weeks > Decimal("0") else Decimal("0")

    return _PaperMetrics(
        trade_count=trade_count,
        win_rate=win_rate,
        avg_return_per_trade=avg_return,
        trade_freq_per_week=trade_freq_per_week,
        current_drawdown_pct=_portfolio_drawdown(conn),
    )


def _portfolio_drawdown(conn: psycopg.Connection[Any]) -> Decimal:
    """V1 simplification: portfolio-wide drawdown_pct from the
    latest snapshot. Used as a proxy for per-strategy drawdown.

    This proxy is imperfect by construction:
      - It OVERSTATES drawdown for a strategy that is flat while
        another strategy is losing (the flat strategy gets blamed
        for someone else's drawdown).
      - It UNDERSTATES drawdown for a strategy that is losing
        while others are profiting (gains elsewhere mask the
        strategy's own bleed).

    Per-strategy drawdown requires reconstructing each strategy's
    equity-curve trajectory from
    `trader_portfolio_snapshots.per_strategy_breakdown` history
    (Step 8 stores per-strategy realised + unrealised PnL per
    snapshot; walking those gives a per-strategy peak + current
    timeline). Deferred to v2.

    At v1 scale (1-3 strategies running concurrently), the proxy's
    error is bounded: when only one strategy is active, the proxy
    is exact; the error grows with the number of co-active
    strategies. The drift analyzer's tolerance (30%/60% bands)
    absorbs the proxy noise for typical 1-3 strategy v1 deployments.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT drawdown_pct FROM trader_portfolio_snapshots ORDER BY ts DESC LIMIT 1",
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return Decimal("0")
    return to_decimal(row[0])


def _emit_breach_alert(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    paper: _PaperMetrics,
    backtest: _BacktestMetrics,
    window_label: str,
) -> None:
    """Write a warning-severity `trader_alerts` row. Step 10's
    dispatcher delivers to Telegram if configured.
    """
    subject = f"Drift breach: strategy {version_id}"
    body = (
        f"Strategy version {version_id} has drifted past the breach "
        f"threshold ({window_label} window).\n"
        f"Paper:    trades={paper.trade_count}, win_rate={paper.win_rate:.3f}, "
        f"avg_return={paper.avg_return_per_trade:.4f}, "
        f"trade_freq/wk={paper.trade_freq_per_week:.2f}, "
        f"drawdown={paper.current_drawdown_pct:.3f}\n"
        f"Backtest: win_rate={backtest.win_rate:.3f}, "
        f"avg_return={backtest.avg_return_per_trade:.4f}, "
        f"trade_freq/wk={backtest.trade_freq_per_week:.2f}, "
        f"max_drawdown={backtest.max_drawdown_pct:.3f}\n"
        f"Review the strategy and consider manual disable via "
        f"POST /trader/strategies/{version_id}/disable. (v1 is advisory; "
        f"the trader does NOT auto-disable.)"
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_alerts
                (channel, severity, subject, body, delivered)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("telegram", "warning", subject, body, False),
        )


def _persist_drift_metric(
    conn: psycopg.Connection[Any],
    *,
    version_id: UUID,
    window_label: str,
    paper: _PaperMetrics,
    backtest: _BacktestMetrics,
    trade_freq_ratio: Decimal,
    win_rate_delta: Decimal,
    avg_return_delta: Decimal,
    drawdown_ratio: Decimal,
    health: HealthStatus,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_drift_metrics
                (strategy_version_id, window_label,
                 paper_trade_count, paper_win_rate, paper_avg_return_per_trade,
                 paper_current_drawdown_pct,
                 backtest_trade_freq_per_week, backtest_win_rate,
                 backtest_avg_return_per_trade, backtest_max_drawdown_pct,
                 trade_freq_ratio, win_rate_delta, avg_return_delta,
                 drawdown_ratio, health_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(version_id),
                window_label,
                paper.trade_count,
                paper.win_rate,
                paper.avg_return_per_trade,
                paper.current_drawdown_pct,
                backtest.trade_freq_per_week,
                backtest.win_rate,
                backtest.avg_return_per_trade,
                backtest.max_drawdown_pct,
                trade_freq_ratio,
                win_rate_delta,
                avg_return_delta,
                drawdown_ratio,
                health.value,
            ),
        )


# ---- Public orchestrator ---------------------------------------------------


def compute_and_persist_drift_for_all(
    database_url: str,
    settings: TraderSettings,  # reserved for future config hooks
    *,
    window_label: str = _DEFAULT_WINDOW_LABEL,
    now: datetime | None = None,
    run_id: UUID | None = None,
) -> DriftCycleResult:
    """Run one drift-analysis pass.

    For each enabled + paper-approved version: compute paper
    metrics in the window, compare against the version's
    walk-forward OOS metrics, persist a `trader_drift_metrics`
    row, and emit a warning alert on breach.

    The runner (Step 12) calls this daily — typical cadence is
    24h, not per signal-execution cycle. Health classification is
    deterministic given the same data; running it more often is
    safe (just costlier) but adds no information.
    """
    state = DriftCycleResult()
    actual_now = now if now is not None else now_utc()
    window_start = _window_to_start(window_label, actual_now)
    window_days = _WINDOW_LABEL_TO_DAYS[window_label]

    log.info(
        "drift_cycle_starting",
        window_label=window_label,
        window_start=window_start.isoformat() if window_start is not None else None,
    )

    with psycopg.connect(database_url) as conn:
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="drift")
        versions = _load_active_versions(conn)
        for version_id, backtest_jsonb, primary_tf in versions:
            # Phase B.6: per-timeframe sqrt(N) scaling of the deviation
            # thresholds. At 4H ``thresholds`` is exactly the module
            # constants (bit-identity guarantee for the existing Phase A
            # strategies); at 1H / 15m the bands widen 2× / 4×.
            thresholds = scaled_thresholds(primary_tf)

            backtest = _extract_backtest_metrics(backtest_jsonb)
            if backtest is None:
                state = state.model_copy(
                    update={
                        "versions_skipped_missing_backtest_metrics": state.versions_skipped_missing_backtest_metrics
                        + 1,
                    },
                )
                log.warning(
                    "drift_skip_missing_backtest_metrics",
                    version_id=str(version_id),
                )
                continue

            paper = _compute_paper_metrics(conn, version_id, window_start, window_days)
            if paper.trade_count < _MIN_PAPER_TRADES:
                state = state.model_copy(
                    update={
                        "versions_skipped_insufficient_trades": state.versions_skipped_insufficient_trades
                        + 1,
                    },
                )
                log.info(
                    "drift_skip_insufficient_trades",
                    version_id=str(version_id),
                    trade_count=paper.trade_count,
                    threshold=_MIN_PAPER_TRADES,
                )
                continue

            # Ratios + deltas.
            trade_freq_ratio = (
                paper.trade_freq_per_week / backtest.trade_freq_per_week
                if backtest.trade_freq_per_week != Decimal("0")
                else Decimal("0")
            )
            win_rate_delta = paper.win_rate - backtest.win_rate
            avg_return_delta = paper.avg_return_per_trade - backtest.avg_return_per_trade
            drawdown_ratio = (
                paper.current_drawdown_pct / backtest.max_drawdown_pct
                if backtest.max_drawdown_pct != Decimal("0")
                else Decimal("0")
            )

            health = _classify_health(
                trade_freq_ratio=trade_freq_ratio,
                paper_win_rate=paper.win_rate,
                backtest_win_rate=backtest.win_rate,
                paper_avg_return=paper.avg_return_per_trade,
                backtest_avg_return=backtest.avg_return_per_trade,
                drawdown_ratio=drawdown_ratio,
                healthy_threshold=thresholds.healthy,
                watch_threshold=thresholds.watch,
                drawdown_breach_ratio=thresholds.drawdown_breach,
            )

            with conn.transaction():
                _persist_drift_metric(
                    conn,
                    version_id=version_id,
                    window_label=window_label,
                    paper=paper,
                    backtest=backtest,
                    trade_freq_ratio=trade_freq_ratio,
                    win_rate_delta=win_rate_delta,
                    avg_return_delta=avg_return_delta,
                    drawdown_ratio=drawdown_ratio,
                    health=health,
                )
                if health is HealthStatus.BREACH:
                    _emit_breach_alert(conn, version_id, paper, backtest, window_label)
                    state = state.model_copy(
                        update={
                            "breach_alerts_emitted": state.breach_alerts_emitted + 1,
                        },
                    )

            state = state.model_copy(
                update={
                    "versions_evaluated": state.versions_evaluated + 1,
                    "drift_rows_persisted": state.drift_rows_persisted + 1,
                },
            )
            log.info(
                "drift_persisted",
                version_id=str(version_id),
                health=health.value,
                trade_count=paper.trade_count,
            )

    log.info("drift_cycle_complete", **state.model_dump())
    return state


__all__ = [
    "DriftCycleResult",
    "compute_and_persist_drift_for_all",
    "scaled_thresholds",
    "sqrt_n_scaling_factor",
]
