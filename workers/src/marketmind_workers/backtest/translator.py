"""StrategySpec -> vectorbt-shaped signals.

Public entry point: `build_signals(spec, data)` walks the spec's
Condition / Expression tree, evaluates each piece against the right
timeframe's OHLCV DataFrame, applies entry filters, and returns a
`SignalSet`:

  - entries:        bool Series indexed on the primary timeframe
  - exits:          bool Series of condition-type exits (e.g. RSI > 70)
                    OR'd together; the FIRST-true wins
  - stop_loss:      the spec's stop-loss config, or None
  - take_profit:    ditto for take-profit
  - max_bars_held:  the spec's time exit, or None
  - direction:      long / short — vbt needs to know which side to take

No look-ahead invariants enforced:

  * crossover compares bar [t-1] vs bar [t]
  * lagged respects `bars_ago` (shift by exactly that many bars)
  * multi-timeframe alignment is "asof backward" by filter-bar CLOSE
    time, not open time. A filter bar [filter_t, filter_t + filter_tf)
    only becomes available to primary bars whose open time is
    >= filter_t + filter_tf. Implemented via merge_asof on a closes-
    timeline.
  * the indicator engine itself produces a NaN warmup at the start of
    each series; we leave those NaNs in place so signal evaluations
    on warmup bars default to False.

Phase 3.1 contract: a spec that this translator accepts must be
executable by `engine.run_backtest`. If the translator can't produce
a clean SignalSet — e.g. a candle_pattern with a filter timeframe
that doesn't have OHLCV data — it raises `TranslationError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, cast

import pandas as pd
import structlog
from marketmind_shared.schemas import (
    SignalDiagnostics,
    SignalDiagnosticsFailureMode,
)
from marketmind_shared.schemas.strategy_spec import (
    AndCondition,
    BollingerBandsCondition,
    CandlePatternCondition,
    CompareCondition,
    Condition,
    ConditionExit,
    ConditionFilter,
    ConstantExpr,
    CrossoverCondition,
    DayOfWeekCondition,
    Direction,
    EntryRules,
    ExitRules,
    Expression,
    FallingCondition,
    Filter,
    IndicatorExpr,
    IndicatorName,
    LaggedExpr,
    NotCondition,
    OrCondition,
    PercentileExpr,
    PriceExpr,
    PriorSignalCondition,
    PriorTradeCondition,
    RatchetExpr,
    RegimeStateCondition,
    RisingCondition,
    RMultipleExit,
    RSICondition,
    SessionFilter,
    StopLossExit,
    StopLossMethod,
    StrategySpec,
    TakeProfitExit,
    TakeProfitMethod,
    TimeExit,
    Timeframe,
    TimeOfDayCondition,
    WeekdayFilter,
    WithinLastNBarsCondition,
    ZScoreCondition,
    decompose_r_multiple,
    timeframe_rank,
)
from marketmind_shared.schemas.strategy_spec.introspection import (
    condition_uses_tier3,
    iter_all_expressions,
    iter_conditions,
)
from marketmind_shared.schemas.trader import RatchetState, RegimeState, StrategyState

from marketmind_workers.backtest import indicators as ind

log = structlog.get_logger(__name__)

# Fraction of post-warmup bars whose entry-condition evaluation
# produced NaN (either side of the expression) that flips the
# diagnostic verdict from CONDITIONS_NEVER_MET to EVALUATION_DEGRADED.
# 0.5 was chosen to match the spec in docs/operations/v1.1-todos.md
# ("If >50% of bars had condition-eval errors, mark as degraded").
_EVAL_DEGRADED_NAN_FRACTION: Final[float] = 0.5

# Map Timeframe enum -> pd.Timedelta — used to compute filter-bar close
# times for the asof-backward alignment.
# pandas-stubs types `pd.Timedelta(minutes=n)` as `Timedelta | NaTType`
# because the constructor allows nan inputs at runtime. None of these
# expressions are nan, so we ignore the union narrowing.
_TIMEFRAME_TO_TIMEDELTA: Final[dict[Timeframe, pd.Timedelta]] = {
    Timeframe.M1: pd.Timedelta(minutes=1),
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
    Timeframe.M30: pd.Timedelta(minutes=30),
    Timeframe.H1: pd.Timedelta(hours=1),
    Timeframe.H4: pd.Timedelta(hours=4),
    Timeframe.D1: pd.Timedelta(days=1),
}  # type: ignore[dict-item]


# ---- Errors ----------------------------------------------------------------


class TranslationError(Exception):
    """Raised when a spec passes Phase 1 validation but isn't executable
    by Phase 3.1's backtest engine (e.g. requesting a timeframe whose
    OHLCV data wasn't supplied, or a candle pattern outside the
    whitelisted set).
    """


# ---- Output dataclass ------------------------------------------------------


@dataclass(frozen=True)
class SignalSet:
    """The compiled signal output for one StrategySpec.

    `entries` and `exits` are boolean Series aligned on the primary
    timeframe's DatetimeIndex. NaN values (from indicator warmups)
    become False after the explicit `.fillna(False)` in the eval path.

    The exit-method fields (stop_loss / take_profit / max_bars_held)
    pass through to vectorbt's exit configuration; they're not turned
    into boolean Series here because vbt handles them natively (and
    much more accurately than a Python-level approximation would).

    `entry_diagnostics` captures the NaN/True/False breakdown of the
    entry-condition Series BEFORE the fillna step. Surfaced on the
    persisted BacktestRun so a reviewer can distinguish "strategy is
    too restrictive" (deterministic-False everywhere) from "spec is
    silently mis-extracted" (NaN everywhere post-warmup). v1.1 fix
    for the silent-zero-trades failure mode documented in
    docs/operations/v1.1-silent-zero-trades.md.
    """

    entries: pd.Series
    exits: pd.Series
    stop_loss: StopLossMethod | None
    take_profit: TakeProfitMethod | None
    max_bars_held: int | None
    direction: Direction
    entry_diagnostics: SignalDiagnostics


# ---- Context --------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    """Shared evaluation state passed to every recursive helper.

    `data` holds OHLCV for the primary timeframe and (if used) the
    filter timeframe. `primary_index` is the eventual home for all
    signal series — anything computed on a filter timeframe gets
    re-aligned here.
    """

    spec: StrategySpec
    data: dict[Timeframe, pd.DataFrame]
    primary_index: pd.DatetimeIndex
    # A.5b state seeding (design doc §6B). `*_seed` maps id(node) -> the
    # seed latch / extremum; `*_out` collects id(node) -> the final value
    # at the window's last bar. All four are empty in the non-stateful
    # `build_signals` path, so seeded helpers fall back to `initial`.
    regime_seed: dict[int, bool] = field(default_factory=dict)
    ratchet_seed: dict[int, float] = field(default_factory=dict)
    regime_out: dict[int, bool] = field(default_factory=dict)
    ratchet_out: dict[int, float] = field(default_factory=dict)


# ---- Public entry ---------------------------------------------------------


def _make_context(spec: StrategySpec, data: dict[Timeframe, pd.DataFrame]) -> _Context:
    """Build the evaluation context, validating that the primary
    timeframe's OHLCV is present and DatetimeIndex-ed.
    """
    if spec.primary_timeframe not in data:
        raise TranslationError(
            f"missing OHLCV data for primary_timeframe={spec.primary_timeframe.value}",
        )
    primary_df = data[spec.primary_timeframe]
    primary_idx = primary_df.index
    if not isinstance(primary_idx, pd.DatetimeIndex):
        raise TranslationError("primary OHLCV index must be a DatetimeIndex")
    return _Context(spec=spec, data=data, primary_index=primary_idx)


def build_signals(
    spec: StrategySpec,
    data: dict[Timeframe, pd.DataFrame],
) -> SignalSet:
    """Compile a validated StrategySpec into a SignalSet.

    `data` must contain at minimum `spec.primary_timeframe`. If the
    spec has `filter_timeframe` or any condition with an explicit
    `timeframe` attribute pointing elsewhere, that timeframe's data
    must also be present.

    Stateless: every Tier-2 recurrence (regime latch, ratchet extremum)
    starts from its `initial` value. The trader's live, history-seeded
    path is `build_signals_stateful`.
    """
    return _compile_signal_set(_make_context(spec, data))


def build_signals_stateful(
    spec: StrategySpec,
    data: dict[Timeframe, pd.DataFrame],
    prior_state: StrategyState | None,
) -> tuple[SignalSet, StrategyState]:
    """Compile a StrategySpec into a SignalSet, seeding every Tier-2
    recurrence from `prior_state` — the state as of the candle before the
    loaded window — and returning the state advanced to the window's last
    bar (design doc §6B).

    `prior_state=None` is a cold start: each recurrence falls back to its
    `initial` value, so the resulting SignalSet is identical to plain
    `build_signals`. The seed is what makes a live regime latch
    full-history-exact rather than window-truncated.
    """
    ctx = _make_context(spec, data)
    regimes, ratchets = _enumerate_stateful_nodes(spec)
    if prior_state is not None:
        for node, regime in zip(regimes, prior_state.regimes, strict=False):
            ctx.regime_seed[id(node)] = regime.latched
        for node, ratchet in zip(ratchets, prior_state.ratchets, strict=False):
            ctx.ratchet_seed[id(node)] = ratchet.extremum
    signal_set = _compile_signal_set(ctx)
    next_state = StrategyState(
        regimes=[RegimeState(latched=ctx.regime_out[id(n)]) for n in regimes],
        ratchets=[RatchetState(extremum=ctx.ratchet_out[id(n)]) for n in ratchets],
    )
    return signal_set, next_state


def _enumerate_stateful_nodes(
    spec: StrategySpec,
) -> tuple[list[RegimeStateCondition], list[RatchetExpr]]:
    """A spec's regime_state conditions and ratchet expressions, in a
    deterministic depth-first order — entry, then condition-exits, then
    condition-filters. The i-th element is `StrategyState` slot i (design
    doc §6B.5); a trader version's spec is immutable, so the mapping is
    stable for the version's lifetime.
    """
    conditions: list[Condition] = [spec.entry.condition]
    conditions.extend(e.condition for e in spec.exit.exits if isinstance(e, ConditionExit))
    conditions.extend(f.condition for f in spec.filters if isinstance(f, ConditionFilter))
    regimes: list[RegimeStateCondition] = []
    ratchets: list[RatchetExpr] = []
    for cond in conditions:
        regimes.extend(c for c in iter_conditions(cond) if isinstance(c, RegimeStateCondition))
        ratchets.extend(e for e in iter_all_expressions(cond) if isinstance(e, RatchetExpr))
    return regimes, ratchets


def _compile_signal_set(ctx: _Context) -> SignalSet:
    """Run entry/filter/exit compilation over a prepared context. Shared
    by `build_signals` and `build_signals_stateful` — the only difference
    between them is whether `ctx`'s seed dicts are populated.
    """
    spec = ctx.spec
    # Refuse Tier-3 specs cleanly before any evaluation — the A.3a
    # vectorised engine cannot evaluate them (see _reject_tier3).
    _reject_tier3(spec)

    entries = _eval_condition(spec.entry.condition, ctx)
    entries = _apply_filters(entries, spec.filters, ctx)
    entries = _apply_entry_order_type(entries, spec.entry, ctx)

    # Capture diagnostics on the entry series BEFORE the fillna step —
    # we need to distinguish NaN bars (eval couldn't decide) from
    # deterministic False (eval decided no). Both collapse to `False`
    # after fillna, so this is the only place we can tell them apart.
    warmup_bars = _estimate_warmup_bars(spec)
    entry_diagnostics = _classify_entry_diagnostics(entries, warmup_bars=warmup_bars)
    log.info(
        "signal_diagnostics_computed",
        bars_evaluated=entry_diagnostics.bars_evaluated,
        warmup_bars=entry_diagnostics.warmup_bars,
        nan_warmup_count=entry_diagnostics.nan_warmup_count,
        nan_post_warmup_count=entry_diagnostics.nan_post_warmup_count,
        true_count=entry_diagnostics.true_count,
        deterministic_false_count=entry_diagnostics.deterministic_false_count,
        failure_mode=entry_diagnostics.failure_mode.value,
        spec_name=spec.name,
    )

    exits, stop_loss, take_profit, max_bars_held = _compile_exits(spec.exit, ctx)

    return SignalSet(
        entries=entries.fillna(value=False).astype(bool),
        exits=exits.fillna(value=False).astype(bool),
        stop_loss=stop_loss,
        take_profit=take_profit,
        max_bars_held=max_bars_held,
        direction=spec.direction,
        entry_diagnostics=entry_diagnostics,
    )


def _reject_tier3(spec: StrategySpec) -> None:
    """Raise TranslationError if the spec uses a Tier-3 stateful element.

    Tier 3 = prior_trade conditions, and ratchet expressions with
    reset='per_trade'. Both depend on trade outcomes / trade-entry
    boundaries that do not exist until the backtest has run, so they
    cannot be precomputed as a vectorbt signal series. They are handled
    by the custom (non-vectorbt) backtest path delivered in A.3b. A.3a
    refuses them cleanly here rather than mis-evaluating silently.
    """
    conditions: list[Condition] = [spec.entry.condition]
    conditions.extend(e.condition for e in spec.exit.exits if isinstance(e, ConditionExit))
    conditions.extend(f.condition for f in spec.filters if isinstance(f, ConditionFilter))
    if any(condition_uses_tier3(c) for c in conditions):
        raise TranslationError(
            "spec uses a Tier-3 stateful element (a prior_trade condition or "
            "a ratchet with reset='per_trade'); the Tier-3 custom backtest "
            "path is delivered in A.3b, not the A.3a vectorised engine",
        )


def _classify_entry_diagnostics(
    entries: pd.Series,
    *,
    warmup_bars: int,
) -> SignalDiagnostics:
    """Compute the SignalDiagnostics for an entry-condition Series.

    `entries` is the raw output of `_eval_condition` BEFORE the
    `.fillna(False).astype(bool)` step. Values are `True`, `False`,
    or NaN. We bucket each bar into one of four bins:

      - bars 0..warmup_bars-1 with NaN  -> nan_warmup_count (expected;
        indicator series start NaN until they've consumed enough bars)
      - bars warmup_bars.. with NaN     -> nan_post_warmup_count
        (surprising; the indicator should have warmed up by now)
      - True                            -> true_count
      - False                           -> deterministic_false_count

    Classification:
      - `nan_post_warmup_count / post_warmup_bars > _EVAL_DEGRADED_NAN_FRACTION`
        ⇒ EVALUATION_DEGRADED (something is producing NaN systematically)
      - else `true_count == 0`
        ⇒ CONDITIONS_NEVER_MET (deterministic No on every bar)
      - else NONE (at least one True signal fired)
    """
    bars_evaluated = len(entries)
    # Empty result — unusual but guard against it.
    if bars_evaluated == 0:
        return SignalDiagnostics(
            bars_evaluated=0,
            nan_warmup_count=0,
            nan_post_warmup_count=0,
            true_count=0,
            deterministic_false_count=0,
            failure_mode=SignalDiagnosticsFailureMode.EVALUATION_DEGRADED,
            warmup_bars=warmup_bars,
        )

    is_nan = entries.isna()
    # Slice via position to keep this resilient to non-default indexes
    # (the entries Series is on the primary-timeframe DatetimeIndex).
    warmup_slice_end = min(warmup_bars, bars_evaluated)
    nan_warmup = int(is_nan.iloc[:warmup_slice_end].sum())
    nan_post_warmup = int(is_nan.iloc[warmup_slice_end:].sum())

    non_nan = ~is_nan
    true_count = int((entries[non_nan] == True).sum())  # noqa: E712 — explicit bool match
    false_count = int((entries[non_nan] == False).sum())  # noqa: E712

    post_warmup_bars = max(0, bars_evaluated - warmup_slice_end)
    # Pathological case (entire window inside warmup) collapses to a
    # synthetic 1.0 NaN fraction so it lands in EVALUATION_DEGRADED —
    # we can't actually diagnose anything in that window.
    nan_post_fraction = nan_post_warmup / post_warmup_bars if post_warmup_bars > 0 else 1.0

    if nan_post_fraction > _EVAL_DEGRADED_NAN_FRACTION:
        mode = SignalDiagnosticsFailureMode.EVALUATION_DEGRADED
    elif true_count == 0:
        mode = SignalDiagnosticsFailureMode.CONDITIONS_NEVER_MET
    else:
        mode = SignalDiagnosticsFailureMode.NONE

    return SignalDiagnostics(
        bars_evaluated=bars_evaluated,
        nan_warmup_count=nan_warmup,
        nan_post_warmup_count=nan_post_warmup,
        true_count=true_count,
        deterministic_false_count=false_count,
        failure_mode=mode,
        warmup_bars=warmup_bars,
    )


def _estimate_warmup_bars(spec: StrategySpec) -> int:
    """Best-effort estimate of the longest indicator warmup window.

    Walks the spec dict for any `params.period` / `params.slow` /
    `params.fast` field and takes the max. Indicators like Bollinger
    (period=20) need ~20 bars before producing non-NaN values; a 200-
    period EMA needs ~200. We use this to slice the entries Series
    into warmup vs post-warmup for the NaN-rate diagnostic.

    Returns 0 if nothing detectable is in the spec. Bumped by +1 so
    a 1-period indicator still excludes its first bar.
    """
    max_period = 0
    spec_dict = spec.model_dump()

    def _walk(node: Any) -> None:
        nonlocal max_period
        if isinstance(node, dict):
            params = node.get("params")
            if isinstance(params, dict):
                for key in ("period", "slow", "fast", "signal", "smooth"):
                    val = params.get(key)
                    if isinstance(val, int | float):
                        max_period = max(max_period, int(val))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(spec_dict)
    return max_period + 1 if max_period > 0 else 0


# ---- Expression evaluation ------------------------------------------------


def _eval_expression(expr: Expression, ctx: _Context, *, timeframe: Timeframe) -> pd.Series:
    """Evaluate an Expression to a numeric Series on the given timeframe.

    `timeframe` is the timeframe the EXPRESSION should be computed on —
    either the spec's primary or a filter timeframe specified on the
    surrounding condition. Multi-timeframe alignment happens at the
    condition level, not here.
    """
    df = _get_df(ctx, timeframe)
    if isinstance(expr, PriceExpr):
        return ind.column(df, expr.field)
    if isinstance(expr, ConstantExpr):
        return pd.Series(expr.value, index=df.index, dtype="float64")
    if isinstance(expr, IndicatorExpr):
        return _eval_indicator(expr, df)
    if isinstance(expr, LaggedExpr):
        inner = _eval_expression(expr.expression, ctx, timeframe=timeframe)
        return cast("pd.Series", inner.shift(expr.bars_ago))
    if isinstance(expr, RatchetExpr):
        return _eval_ratchet(expr, ctx, timeframe=timeframe)
    if isinstance(expr, PercentileExpr):
        # Pure rolling reduction; not stateful in the Tier-2/Tier-3
        # sense. Same helper used identically by the vbt path (here)
        # and the iterative engine (which reuses this dispatcher) —
        # bit-identity by construction.
        inner = _eval_expression(expr.expression, ctx, timeframe=timeframe)
        return ind.percentile_rolling(inner, expr.window)
    # The remaining Expression variant is ScaledExpr.
    inner = _eval_expression(expr.expression, ctx, timeframe=timeframe)
    return cast("pd.Series", inner * expr.factor)


def _eval_ratchet(expr: RatchetExpr, ctx: _Context, *, timeframe: Timeframe) -> pd.Series:
    """Evaluate a v2.0 ratchet expression — a running favorable extremum.

    `reset="never"` — the only mode the A.3a engine supports — is the
    running max (extremum="max") or min ("min") over the whole series:
    a vectorised pandas cummax / cummin, no per-bar scan. NaN warmup
    bars in the source stay NaN (cummax/cummin skip them) until the
    first real value, after which the extremum accumulates.

    `reset="per_trade"` is Tier 3 (it resets at each trade entry — a
    boundary that does not exist before the backtest runs). `_reject_tier3`
    rejects such specs in build_signals; the guard below is defensive.
    """
    inner = _eval_expression(expr.source, ctx, timeframe=timeframe)
    if expr.reset == "never":
        running = inner.cummax() if expr.extremum == "max" else inner.cummin()
        # A.5b seeding (design doc §6B.3): fold the seed extremum into the
        # running max/min so the live extremum reflects pre-window
        # history. max/min is idempotent, so re-including window bars the
        # seed already covers is harmless. The final extremum is recorded
        # into ctx.ratchet_out.
        seed = ctx.ratchet_seed.get(id(expr))
        if seed is not None:
            running = (
                running.clip(lower=seed)
                if expr.extremum == "max"
                else running.clip(upper=seed)
            )
        ctx.ratchet_out[id(expr)] = float(running.iloc[-1])
        return ind.as_series(running)
    raise TranslationError(
        "ratchet reset='per_trade' is Tier 3 (trade-boundary dependent); "
        "the A.3b custom backtest path handles it, not the A.3a engine",
    )


def _eval_indicator(expr: IndicatorExpr, df: pd.DataFrame) -> pd.Series:
    """Dispatch an IndicatorExpr to the indicators module."""
    name = expr.name
    params = expr.params.model_dump(exclude_none=True)
    source = expr.source
    if name is IndicatorName.SMA:
        return ind.sma(df, int(params["period"]), source=source)
    if name is IndicatorName.EMA:
        return ind.ema(df, int(params["period"]), source=source)
    if name is IndicatorName.WMA:
        return ind.wma(df, int(params["period"]), source=source)
    if name is IndicatorName.RSI:
        return ind.rsi(df, int(params["period"]), source=source)
    if name is IndicatorName.MACD:
        macd_df = ind.macd(
            df,
            fast=int(params["fast"]),
            slow=int(params["slow"]),
            signal=int(params["signal"]),
            source=source,
        )
        return _select_component(macd_df, expr.component, name.value)
    if name is IndicatorName.STOCHASTIC:
        stoch_df = ind.stochastic(
            df,
            k=int(params["k"]),
            d=int(params["d"]),
            smooth=int(params["smooth"]),
        )
        return _select_component(stoch_df, expr.component, name.value)
    if name is IndicatorName.ATR:
        return ind.atr(df, int(params["period"]))
    if name is IndicatorName.BOLLINGER:
        bb_df = ind.bollinger(
            df,
            period=int(params["period"]),
            std_dev=float(params["std_dev"]),
            source=source,
        )
        return _select_component(bb_df, expr.component, name.value)
    if name is IndicatorName.STDDEV:
        return ind.stddev(df, int(params["period"]), source=source)
    if name is IndicatorName.VOLUME_SMA:
        return ind.volume_sma(df, int(params["period"]))
    if name is IndicatorName.OBV:
        return ind.obv(df)
    if name is IndicatorName.VWAP:
        return ind.vwap(df, session_anchored=bool(params["session_anchored"]))
    if name is IndicatorName.HIGHEST:
        return ind.highest(df, int(params["period"]), source=params["source"])
    if name is IndicatorName.LOWEST:
        return ind.lowest(df, int(params["period"]), source=params["source"])
    if name is IndicatorName.RETURNS:
        period = int(params.get("period", 1))
        return ind.returns(df, period=period, source=source)
    if name is IndicatorName.SUPERTREND:
        st_df = ind.supertrend(
            df,
            atr_period=int(params["atr_period"]),
            multiplier=float(params["multiplier"]),
        )
        return _select_component(st_df, expr.component, name.value)
    if name is IndicatorName.ADX:
        return ind.adx(df, int(params["period"]))
    if name is IndicatorName.KELTNER:
        kc_df = ind.keltner(
            df,
            period=int(params["period"]),
            atr_period=int(params["atr_period"]),
            multiplier=float(params["multiplier"]),
        )
        return _select_component(kc_df, expr.component, name.value)
    if name is IndicatorName.PSAR:
        psar_df = ind.psar(
            df,
            step=float(params["step"]),
            max_step=float(params["max_step"]),
        )
        return _select_component(psar_df, expr.component, name.value)
    raise TranslationError(f"unhandled indicator: {name.value}")


def _select_component(multi: pd.DataFrame, component: str | None, name: str) -> pd.Series:
    if component is None:
        raise TranslationError(f"{name} requires a component selector")
    if component not in multi.columns:
        raise TranslationError(
            f"unknown component {component!r} for {name}; have {list(multi.columns)}",
        )
    return ind.as_series(multi[component])


# ---- Condition evaluation -------------------------------------------------


def _eval_condition(cond: Condition, ctx: _Context) -> pd.Series:
    """Evaluate a Condition to a bool Series on the primary timeframe.

    If the condition has an explicit `timeframe` (filter timeframe),
    we evaluate it on that timeframe's data and align the result to
    primary using `_align_to_primary` (asof-backward by filter close).
    """
    tf = _condition_timeframe(cond, ctx)
    raw = _eval_condition_on_tf(cond, ctx, timeframe=tf)
    return _align_to_primary(raw, source_tf=tf, ctx=ctx)


def _condition_timeframe(cond: Condition, ctx: _Context) -> Timeframe:
    """Resolve the timeframe a condition should be evaluated on."""
    if isinstance(
        cond,
        AndCondition
        | OrCondition
        | NotCondition
        | WithinLastNBarsCondition
        | RegimeStateCondition,
    ):
        # Composite conditions are always evaluated on the primary;
        # their sub-conditions resolve their own timeframes inside.
        return ctx.spec.primary_timeframe
    if isinstance(cond, CandlePatternCondition):
        if cond.timeframe is not None:
            return cond.timeframe
        return ctx.spec.primary_timeframe
    # Compare/Crossover/Rising/Falling all have an explicit .timeframe
    # field (None => primary).
    explicit_tf = getattr(cond, "timeframe", None)
    return explicit_tf if explicit_tf is not None else ctx.spec.primary_timeframe


def _eval_condition_on_tf(
    cond: Condition,
    ctx: _Context,
    *,
    timeframe: Timeframe,
) -> pd.Series:
    """Evaluate the condition on its native timeframe, returning a
    bool Series indexed on THAT timeframe (alignment to primary
    happens upstream).
    """
    if isinstance(cond, CompareCondition):
        return _eval_compare(cond, ctx, timeframe)
    if isinstance(cond, CrossoverCondition):
        return _eval_crossover(cond, ctx, timeframe)
    if isinstance(cond, WithinLastNBarsCondition):
        return _eval_within_last_n_bars(cond, ctx)
    if isinstance(cond, RisingCondition):
        return _eval_monotonic(cond.series, cond.lookback, cond.strict, "up", ctx, timeframe)
    if isinstance(cond, FallingCondition):
        return _eval_monotonic(cond.series, cond.lookback, cond.strict, "down", ctx, timeframe)
    if isinstance(cond, CandlePatternCondition):
        return _eval_candle_pattern(cond, ctx, timeframe)
    if isinstance(cond, AndCondition):
        parts = [_eval_condition(c, ctx) for c in cond.conditions]
        result = parts[0]
        for p in parts[1:]:
            result = cast("pd.Series", result & p)
        return result
    if isinstance(cond, OrCondition):
        parts = [_eval_condition(c, ctx) for c in cond.conditions]
        result = parts[0]
        for p in parts[1:]:
            result = cast("pd.Series", result | p)
        return result
    if isinstance(cond, RegimeStateCondition):
        return _eval_regime_state(cond, ctx)
    if isinstance(cond, PriorTradeCondition | PriorSignalCondition):
        # Tier 3 — unreachable in practice; _reject_tier3 catches these
        # in build_signals before evaluation begins. Defensive only.
        raise TranslationError(
            f"{cond.type} is a Tier-3 condition handled by the iterative "
            "backtest path, not the A.3a vectorised engine",
        )
    if isinstance(cond, TimeOfDayCondition):
        # v1.2.C: stateless hour-of-day gate. Same helper shared by the
        # vbt path (here) and the iterative engine (which reuses this
        # dispatcher via translator._eval_condition) — bit-identity by
        # construction. The bar index is tz-aware UTC throughout the
        # codebase, so .hour returns the right value without an
        # explicit tz-conversion step.
        df = _get_df(ctx, timeframe)
        return _eval_time_of_day(cond, df)
    if isinstance(cond, DayOfWeekCondition):
        # v1.2.D: stateless day-of-week gate. Same dispatcher-shared
        # pattern as TimeOfDayCondition.
        df = _get_df(ctx, timeframe)
        return _eval_day_of_week(cond, df)
    if isinstance(cond, RSICondition):
        # v1.3: stateless RSI oscillator gate. Reuses ind.rsi (Wilder),
        # the exact function both engines already use for
        # indicator(name="rsi") expressions — so the RSI series is
        # bit-identical and this dispatcher is the single implementation
        # shared by the vbt path (here) and the iterative engine (via
        # translator._eval_condition).
        df = _get_df(ctx, timeframe)
        return _eval_rsi(cond, df)
    if isinstance(cond, BollingerBandsCondition):
        # v1.3: stateless Bollinger-band gate (below_lower / above_upper /
        # squeeze). Same dispatcher-shared pattern — the iterative engine
        # reaches this branch via translator._eval_condition for any
        # non-Tier3 condition, so the band math is bit-identical across
        # both engines by construction.
        df = _get_df(ctx, timeframe)
        return _eval_bollinger_bands(cond, df)
    if isinstance(cond, ZScoreCondition):
        # v1.3: stateless statistical mean-reversion gate. z computed
        # inline from ind.sma + ind.stddev — no new indicator. Shared
        # helper, so the vbt path (here) and the iterative engine
        # (via translator._eval_condition) are bit-identical.
        df = _get_df(ctx, timeframe)
        return _eval_zscore(cond, df)
    # The remaining Condition variant is NotCondition.
    inner = _eval_condition(cond.condition, ctx)
    return ind.as_series(~inner.astype(bool))


def _eval_rsi(cond: RSICondition, df: pd.DataFrame) -> pd.Series:
    """Boolean mask from Wilder's RSI vs a fixed threshold.

    Semantics (no-lookahead — crosses compare the previous bar to the
    current bar only):
      - below:         RSI[t]   < threshold
      - above:         RSI[t]   > threshold
      - crosses_above: RSI[t-1] <= threshold  AND  RSI[t] > threshold
      - crosses_below: RSI[t-1] >= threshold  AND  RSI[t] < threshold

    Warmup bars where RSI is NaN evaluate to False (NaN comparisons are
    False, and the crossover's prev-bar NaN also yields False), matching
    every other indicator condition in this module.
    """
    rsi = ind.rsi(df, cond.period, cond.source)
    thr = cond.threshold
    if cond.comparison == "below":
        mask = rsi < thr
    elif cond.comparison == "above":
        mask = rsi > thr
    elif cond.comparison == "crosses_above":
        prev = rsi.shift(1)
        mask = (prev <= thr) & (rsi > thr)
    else:  # crosses_below
        prev = rsi.shift(1)
        mask = (prev >= thr) & (rsi < thr)
    return pd.Series(mask.to_numpy(dtype=bool), index=df.index, dtype=bool)
def _eval_bollinger_bands(cond: BollingerBandsCondition, df: pd.DataFrame) -> pd.Series:
    """Boolean mask for a BollingerBandsCondition.

    below_lower:  close < lower band.
    above_upper:  close > upper band.
    squeeze:      percentile_rolling(upper - lower, squeeze_window)
                  <= squeeze_percentile  (the low-volatility coil).

    Bands come from ``ind.bollinger`` (SMA middle ± num_std × stddev);
    the squeeze percentile reuses ``ind.percentile_rolling`` — the same
    helper backing PercentileExpr (v1.2.A). Warmup bars where any input
    is NaN compare False (NaN comparisons are False), so the condition
    simply doesn't fire during the band / percentile warmup.
    """
    bands = ind.bollinger(df, cond.period, cond.num_std, cond.source)
    close = ind.column(df, "close")
    if cond.form == "below_lower":
        lower = ind.column(bands, "lower")
        mask = close < lower
    elif cond.form == "above_upper":
        upper = ind.column(bands, "upper")
        mask = close > upper
    else:
        # squeeze: bandwidth percentile in the low tail. The validator
        # guarantees both squeeze params are present when form=='squeeze'.
        assert cond.squeeze_window is not None
        assert cond.squeeze_percentile is not None
        bandwidth = ind.as_series(
            ind.column(bands, "upper") - ind.column(bands, "lower"),
        )
        pct = ind.percentile_rolling(bandwidth, cond.squeeze_window)
        mask = pct <= cond.squeeze_percentile
    return pd.Series(mask, index=df.index, dtype=bool)


def _eval_zscore(cond: ZScoreCondition, df: pd.DataFrame) -> pd.Series:
    """Boolean mask for the rolling-z-score mean-reversion gate.

    z[t] = (source[t] - SMA(source, period)[t]) / StdDev(source, period)[t]

    StdDev is the rolling sample std (ddof=1) from ind.stddev — the same
    convention as the stddev indicator. Where the rolling std is zero
    (a perfectly flat window) the division yields inf/NaN; we coerce the
    zero-std bars to NaN explicitly so z is NaN there, and NaN comparisons
    evaluate False downstream (no spurious signal on a dead-flat series).

    The first ``period - 1`` bars are NaN (strict min_periods on sma /
    stddev), so the gate simply doesn't fire during warmup.

    Forms:
      - below_neg          : z < -threshold
      - above_pos          : z > +threshold
      - cross_toward_zero  : z[t-1] beyond ±threshold AND z[t] moved
                             toward zero (recovering from the extreme).
    """
    mean = ind.sma(df, cond.period, cond.source)
    std = ind.stddev(df, cond.period, cond.source)
    src = ind.column(df, cond.source)
    # Guard divide-by-zero: a zero rolling std -> NaN z (condition False).
    safe_std = std.where(std != 0.0)
    z = ind.as_series((src - mean) / safe_std)

    thr = cond.threshold
    if cond.form == "below_neg":
        mask = z < -thr
    elif cond.form == "above_pos":
        mask = z > thr
    else:
        # cross_toward_zero: previous bar beyond the band, this bar moved
        # toward zero. NaN on either bar -> the comparisons are False, so
        # warmup and dead-flat windows never trigger.
        prev = z.shift(1)
        recovering_from_oversold = (prev <= -thr) & (z > prev)
        recovering_from_overbought = (prev >= thr) & (z < prev)
        mask = recovering_from_oversold | recovering_from_overbought
    return pd.Series(mask, index=df.index, dtype=bool)


def _eval_day_of_week(cond: DayOfWeekCondition, df: pd.DataFrame) -> pd.Series:
    """Boolean mask: True on bars whose open_ts (UTC) weekday is in
    cond.weekdays. Pandas convention: 0=Monday, 6=Sunday.

    Same defensive-assert + dispatcher-shared pattern as
    _eval_time_of_day. The bar index is tz-aware UTC throughout the
    codebase, so .weekday returns the right value without explicit
    tz-conversion.
    """
    assert isinstance(df.index, pd.DatetimeIndex), (
        "DayOfWeekCondition requires a DatetimeIndex; got "
        f"{type(df.index).__name__}"
    )
    weekdays = df.index.weekday  # type: ignore[attr-defined]
    mask = weekdays.isin(cond.weekdays)
    return pd.Series(mask, index=df.index, dtype=bool)


def _eval_time_of_day(cond: TimeOfDayCondition, df: pd.DataFrame) -> pd.Series:
    """Boolean mask: True on bars whose open_ts (UTC) hour falls in
    [start_hour_utc, end_hour_utc] (inclusive or exclusive on the end
    per cond.inclusive_end).

    Wrap-around windows (start > end) span midnight:
      - start=22, end=2, inclusive_end=False -> hours 22, 23, 0, 1
      - start=22, end=2, inclusive_end=True  -> hours 22, 23, 0, 1, 2

    pandas note: ``df.index.hour`` returns int per bar without DST
    surprises because every DataFrame in the codebase is tz-aware UTC
    (no DST). If a future ingestion path produced naive timestamps,
    this would silently return whatever local hour pandas inferred —
    we rely on the upstream "always UTC" invariant.
    """
    assert isinstance(df.index, pd.DatetimeIndex), (
        "TimeOfDayCondition requires a DatetimeIndex; got "
        f"{type(df.index).__name__}"
    )
    hours = df.index.hour  # type: ignore[attr-defined]
    s = cond.start_hour_utc
    e = cond.end_hour_utc
    if s <= e:
        # Non-wrap window: a single contiguous interval inside [0, 23].
        mask = (
            (hours >= s) & (hours <= e)
            if cond.inclusive_end
            else (hours >= s) & (hours < e)
        )
    else:
        # Wrap-around: [start, 23] ∪ [0, end (inclusive/exclusive)].
        mask = (
            (hours >= s) | (hours <= e)
            if cond.inclusive_end
            else (hours >= s) | (hours < e)
        )
    return pd.Series(mask, index=df.index, dtype=bool)


def _eval_regime_state(cond: RegimeStateCondition, ctx: _Context) -> pd.Series:
    """Evaluate a v2.0 regime_state condition — a latched boolean.

    TRUE from the bar `enter_when` first fires until `exit_when` fires,
    then FALSE until `enter_when` re-fires. Vectorised, no per-bar scan:
    build a marker series (+1 where enter fires, -1 where exit fires)
    and forward-fill it, so each bar carries the most recent enter/exit
    decision. The -1 is applied after the +1, so when both triggers
    fire on the same bar EXIT wins (the conservative choice). Leading
    bars before any trigger take `cond.initial`.

    A NaN sub-condition value (indicator warmup, pre-alignment) counts
    as "did not fire" — the regime sits at the leading value until a
    real trigger lands. The result is a clean bool Series with no NaN, so
    the entry-diagnostics classifier reads it without special-casing.

    A.5b seeding (design doc §6B.3): bars before the first in-window
    trigger take `ctx.regime_seed` for this node when present — the latch
    as of the last evaluated candle — else `cond.initial` (the
    non-stateful path and the cold start). This is exact because the
    latch is "the value set by the most recent trigger": if a trigger
    fires in-window `ffill` wins regardless of the seed; if none does the
    latch has been constant across the window, and the seed *is* that
    constant. The final latch is recorded into `ctx.regime_out`.
    """
    enter = _eval_condition(cond.enter_when, ctx).fillna(value=False).astype(bool)
    exit_when = _eval_condition(cond.exit_when, ctx).fillna(value=False).astype(bool)
    blank = pd.Series(float("nan"), index=ctx.primary_index, dtype="float64")
    marker = blank.mask(enter, other=1.0).mask(exit_when, other=-1.0)
    seed = ctx.regime_seed.get(id(cond))
    leading = cond.initial if seed is None else seed
    latched = marker.ffill().fillna(value=1.0 if leading else -1.0)
    result = latched > 0.0
    ctx.regime_out[id(cond)] = bool(result.iloc[-1])
    return ind.as_series(result)


def _eval_compare(cond: CompareCondition, ctx: _Context, tf: Timeframe) -> pd.Series:
    left = _eval_expression(cond.left, ctx, timeframe=tf)
    right = _eval_expression(cond.right, ctx, timeframe=tf)
    op = cond.op
    if op == ">":
        return ind.as_series(left > right)
    if op == ">=":
        return ind.as_series(left >= right)
    if op == "<":
        return ind.as_series(left < right)
    if op == "<=":
        return ind.as_series(left <= right)
    if op == "==":
        return ind.as_series(left == right)
    raise TranslationError(f"unhandled compare op: {op!r}")


def _eval_crossover(cond: CrossoverCondition, ctx: _Context, tf: Timeframe) -> pd.Series:
    """Crossover at bar t: prev relationship flipped, current relationship
    new.  direction='above': series[t-1] <= threshold[t-1] AND
    series[t] > threshold[t]. Mirror for 'below'.
    """
    series = _eval_expression(cond.series, ctx, timeframe=tf)
    threshold = _eval_expression(cond.threshold, ctx, timeframe=tf)
    prev_s = series.shift(1)
    prev_t = threshold.shift(1)
    if cond.direction == "above":
        return ind.as_series((prev_s <= prev_t) & (series > threshold))
    if cond.direction == "below":
        return ind.as_series((prev_s >= prev_t) & (series < threshold))
    raise TranslationError(f"unhandled crossover direction: {cond.direction!r}")


def _eval_within_last_n_bars(
    cond: WithinLastNBarsCondition,
    ctx: _Context,
) -> pd.Series:
    """True at bar t iff the inner condition was True on at least one of
    bars [t-n+1 .. t]. Implementation: rolling-any over the inner
    condition's bool series.
    """
    inner = _eval_condition(cond.condition, ctx)
    # Rolling boolean OR via max() on float-coerced values.
    rolled = inner.astype(float).rolling(window=cond.n, min_periods=1).max()
    return ind.as_series(rolled > 0)


def _eval_monotonic(
    series_expr: Expression,
    lookback: int,
    strict: bool,
    direction: str,
    ctx: _Context,
    tf: Timeframe,
) -> pd.Series:
    """Rising/Falling: series[t] vs series[t-lookback]."""
    series = _eval_expression(series_expr, ctx, timeframe=tf)
    earlier = series.shift(lookback)
    if direction == "up":
        return ind.as_series(series > earlier if strict else series >= earlier)
    # direction == "down"
    return ind.as_series(series < earlier if strict else series <= earlier)


def _eval_candle_pattern(
    cond: CandlePatternCondition,
    ctx: _Context,
    tf: Timeframe,
) -> pd.Series:
    df = _get_df(ctx, tf)
    pattern = cond.pattern
    if pattern == "bullish_engulfing":
        return ind.bullish_engulfing(df)
    if pattern == "bearish_engulfing":
        return ind.bearish_engulfing(df)
    if pattern == "hammer":
        return ind.hammer(df)
    if pattern == "shooting_star":
        return ind.shooting_star(df)
    if pattern == "doji":
        return ind.doji(df)
    if pattern == "bullish_pinbar":
        return ind.bullish_pinbar(df)
    if pattern == "bearish_pinbar":
        return ind.bearish_pinbar(df)
    raise TranslationError(f"unknown candle pattern: {pattern!r}")


# ---- Multi-timeframe alignment --------------------------------------------


def _align_to_primary(
    series: pd.Series,
    *,
    source_tf: Timeframe,
    ctx: _Context,
) -> pd.Series:
    """Align a series computed on `source_tf` to the primary timeframe.

    For same-tf inputs this is identity. For higher (or different)
    source_tf, we do an asof-backward merge using each filter bar's
    CLOSE TIME, so the value only becomes available to primary bars
    whose open time is >= the filter bar's close.

    Why close-time and not open-time: a 1h filter bar with open_time
    13:00 covers [13:00, 14:00). The bar's OHLC values are only
    knowable at 14:00 exactly. A 15m primary bar with open_time 13:30
    must NOT be allowed to peek at the 13:00 1h bar's close — that
    would be classic forward-looking. Open_time 14:00 is the first
    primary bar that may legitimately consume it.
    """
    if source_tf == ctx.spec.primary_timeframe:
        return series
    # Different timeframe -> alignment needed.
    if timeframe_rank(source_tf) < timeframe_rank(ctx.spec.primary_timeframe):
        raise TranslationError(
            f"cannot align lower-timeframe ({source_tf.value}) condition onto "
            f"higher primary ({ctx.spec.primary_timeframe.value}); the spec "
            f"validator should have prevented this",
        )
    bar_td = _TIMEFRAME_TO_TIMEDELTA[source_tf]
    # Build a 2-column DataFrame: (filter_close_time, value).
    filter_idx = series.index
    assert isinstance(filter_idx, pd.DatetimeIndex)
    close_times = filter_idx + bar_td
    filter_df = pd.DataFrame({"value": series.to_numpy()}, index=close_times).sort_index()
    primary_df = pd.DataFrame(index=ctx.primary_index)
    merged = pd.merge_asof(
        primary_df.reset_index(),
        filter_df.reset_index().rename(columns={"index": "filter_close"}),
        left_on=primary_df.index.name or "index",
        right_on="filter_close",
        direction="backward",
    )
    aligned = merged.set_index(primary_df.index.name or "index")["value"]
    return ind.as_series(aligned)


# ---- Filters --------------------------------------------------------------


def _apply_filters(entries: pd.Series, filters: list[Filter], ctx: _Context) -> pd.Series:
    """Mask entries with each filter ANDed together."""
    if not filters:
        return entries
    mask = pd.Series(True, index=entries.index, dtype=bool)
    for f in filters:
        mask = cast("pd.Series", mask & _filter_mask(f, ctx))
    return cast("pd.Series", entries & mask)


def _filter_mask(f: Filter, ctx: _Context) -> pd.Series:
    if isinstance(f, SessionFilter):
        idx = ctx.primary_index
        start, end = f.hours_utc
        # pandas-stubs doesn't expose DatetimeIndex.hour; reach it via
        # the typed-out PythonDatetimeIndex API.
        hours = pd.Series(idx, index=idx).dt.hour
        return ind.as_series((hours >= start) & (hours <= end))
    if isinstance(f, WeekdayFilter):
        idx = ctx.primary_index
        # pandas weekday: Monday=0 .. Sunday=6. Spec uses ISO 1..7.
        # Convert spec list to pandas-style.
        spec_days = {d - 1 for d in f.days}
        weekdays = pd.Series(idx, index=idx).dt.weekday
        return ind.as_series(weekdays.isin(spec_days))
    # The discriminated Filter union is exhaustive; ConditionFilter
    # is the last variant.
    return _eval_condition(f.condition, ctx)


# ---- Entry order type -----------------------------------------------------


def _apply_entry_order_type(
    entries: pd.Series,
    entry: EntryRules,
    ctx: _Context,
) -> pd.Series:
    """For Phase 3.1, market orders pass through unchanged; limit orders
    are also represented as "signal on this bar" — vectorbt then fills
    at the next bar's open. The limit_offset_pct is recorded for
    Phase 3.2's slippage modelling but not yet applied here.

    Documented intentionally so a Phase 3.2 reader knows that limit
    fills currently get the same fill-at-next-open treatment as
    market orders. Refinement is on the Phase 3.2 list.
    """
    _ = ctx  # signature parity for future refinement
    if entry.order_type.value == "limit":
        # Future: build a separate "limit-fill" series that fires only
        # when the next bar's price hits entry_close * (1 + offset).
        # Phase 3.1 ships the simpler model.
        pass
    return entries


# ---- Exits ----------------------------------------------------------------


def _compile_exits(
    exit_rules: ExitRules,
    ctx: _Context,
) -> tuple[pd.Series, StopLossMethod | None, TakeProfitMethod | None, int | None]:
    """Reduce the spec's `exits` list to:
    - a combined bool exit series (OR of all condition-type exits)
    - a single stop_loss method (last-wins if multiple specified;
      Phase 1 validators normally prevent this)
    - a single take_profit method
    - max bars held (from TimeExit)
    """
    condition_exits: list[pd.Series] = []
    stop_loss: StopLossMethod | None = None
    take_profit: TakeProfitMethod | None = None
    max_bars_held: int | None = None
    for ex in exit_rules.exits:
        if isinstance(ex, ConditionExit):
            condition_exits.append(_eval_condition(ex.condition, ctx))
        elif isinstance(ex, StopLossExit):
            stop_loss = ex.method
        elif isinstance(ex, TakeProfitExit):
            take_profit = ex.method
        elif isinstance(ex, TimeExit):
            max_bars_held = ex.max_bars_held
        else:
            # The remaining ExitCondition variant is RMultipleExit
            # (primitive-4). It is a PRIMARY exit: synthesize the ATR-multiple
            # stop + take-profit it composes (R = atr_multiple × ATR), so the
            # same _vbt_stop_loss / _vbt_take_profit percent-of-close paths
            # drive it. Sets BOTH legs at once (an RMultipleExit IS a
            # stop+target pair) — last-wins applies if combined with explicit
            # stop_loss / take_profit exits, which is a degenerate spec.
            assert isinstance(ex, RMultipleExit)
            stop_loss, take_profit = decompose_r_multiple(ex)
    if condition_exits:
        combined = condition_exits[0]
        for c in condition_exits[1:]:
            combined = cast("pd.Series", combined | c)
    else:
        combined = pd.Series(False, index=ctx.primary_index, dtype=bool)
    return combined, stop_loss, take_profit, max_bars_held


# ---- Helpers --------------------------------------------------------------


def _get_df(ctx: _Context, tf: Timeframe) -> pd.DataFrame:
    if tf not in ctx.data:
        raise TranslationError(
            f"missing OHLCV data for timeframe={tf.value}; "
            f"supply it via the `data` dict when calling build_signals",
        )
    return ctx.data[tf]


__all__ = ["SignalSet", "TranslationError", "build_signals", "build_signals_stateful"]
