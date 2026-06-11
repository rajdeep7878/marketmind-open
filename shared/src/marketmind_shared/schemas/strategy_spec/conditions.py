"""Condition union: compare/crossover/within_last_n_bars/rising/falling/
candle_pattern/and/or/not, plus the v2.0 stateful conditions regime_state/
prior_trade/prior_signal.

Recursive: and/or/not/within_last_n_bars/regime_state wrap Conditions.
Resolved via forward refs + model_rebuild() at the bottom of the module.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import Timeframe, _StrictModel
from marketmind_shared.schemas.strategy_spec.expressions import Expression


class CompareCondition(_StrictModel):
    type: Literal["compare"] = "compare"
    left: Expression
    op: Literal[">", ">=", "<", "<=", "=="]
    right: Expression
    timeframe: Timeframe | None = None


class CrossoverCondition(_StrictModel):
    type: Literal["crossover"] = "crossover"
    series: Expression
    threshold: Expression
    direction: Literal["above", "below"]
    timeframe: Timeframe | None = None


class WithinLastNBarsCondition(_StrictModel):
    type: Literal["within_last_n_bars"] = "within_last_n_bars"
    condition: Condition
    # 1..1000 is generous; longer lookbacks usually indicate a different
    # design (state-based, see v2 territory).
    n: int = Field(ge=1, le=1000)


class RisingCondition(_StrictModel):
    type: Literal["rising"] = "rising"
    series: Expression
    lookback: int = Field(ge=1, le=1000)
    strict: bool = False
    timeframe: Timeframe | None = None


class FallingCondition(_StrictModel):
    type: Literal["falling"] = "falling"
    series: Expression
    lookback: int = Field(ge=1, le=1000)
    strict: bool = False
    timeframe: Timeframe | None = None


class CandlePatternCondition(_StrictModel):
    type: Literal["candle_pattern"] = "candle_pattern"
    pattern: Literal[
        "bullish_engulfing",
        "bearish_engulfing",
        "hammer",
        "shooting_star",
        "doji",
        "bullish_pinbar",
        "bearish_pinbar",
    ]
    timeframe: Timeframe | None = None


class AndCondition(_StrictModel):
    type: Literal["and"] = "and"
    conditions: list[Condition] = Field(min_length=1)


class OrCondition(_StrictModel):
    type: Literal["or"] = "or"
    conditions: list[Condition] = Field(min_length=1)


class NotCondition(_StrictModel):
    type: Literal["not"] = "not"
    condition: Condition


class RegimeStateCondition(_StrictModel):
    """v2.0 stateful condition: a latched boolean regime flag.

    Evaluates TRUE from the bar ``enter_when`` first fires until the bar
    ``exit_when`` fires, then FALSE until ``enter_when`` re-fires. Models
    e.g. a Supertrend direction regime. Path-dependent on price/indicator
    inputs only (Tier 2 — a numba scan over the whole series). See
    docs/design/v2-phase-a-stateful-conditions.md section 1.1.
    """

    type: Literal["regime_state"] = "regime_state"
    enter_when: Condition = Field(
        description="Condition that latches the regime ON when it first becomes true.",
    )
    exit_when: Condition = Field(
        description="Condition that latches the regime OFF. Must differ from enter_when.",
    )
    initial: bool = Field(
        default=False,
        description="Regime state before enter_when has ever fired (usually false).",
    )

    @model_validator(mode="after")
    def _triggers_differ(self) -> Self:
        # Identical enter/exit triggers make the latch ill-defined: on a
        # bar where the shared trigger fires, the regime would both enter
        # and exit. Reject at the schema boundary.
        if self.enter_when == self.exit_when:
            raise PydanticCustomError(
                "regime_state_triggers_identical",
                "regime_state.enter_when and exit_when must differ "
                "(identical triggers make the latch ill-defined)",
            )
        return self


class PriorTradeCondition(_StrictModel):
    """v2.0 stateful condition: gates on the outcome of prior trades.

    Tier 3 (outcome-dependent): trade results do not exist until the
    backtest has run, so this is evaluated only by the custom backtest
    path, never vectorbt's from_signals. ``n`` applies to the
    ``consecutive_*`` predicates; the ``last_*`` predicates ignore it
    (validator emits a soft warning if ``n`` is set anyway). See
    docs/design/v2-phase-a-stateful-conditions.md section 1.1.
    """

    type: Literal["prior_trade"] = "prior_trade"
    predicate: Literal[
        "last_won",
        "last_lost",
        "consecutive_losses_at_least",
        "consecutive_wins_at_least",
        # v1.2.B (2026-05-24): time-based throttle. True when at least n
        # bars have elapsed since the last completed trade's exit_index.
        # Distinct from the outcome-based predicates above — it gates on
        # ELAPSED TIME, not on trade results. Surfaced by Hunt 5
        # (Mean-reversion + Tier-3 throttle) where the LLM tried to
        # express "wait at least 24 bars after last trade" and had to
        # approximate with a win-gating proxy.
        "bars_since_last_at_least",
    ] = Field(
        description=(
            "Which prior-trade property to gate on. last_won / last_lost "
            "test the single most recent closed trade. "
            "consecutive_losses_at_least / consecutive_wins_at_least test "
            "a run of at least n trades. "
            "bars_since_last_at_least is a time-based throttle — true when "
            "the most recent completed trade closed at least n bars ago."
        ),
    )
    n: int = Field(
        default=1,
        ge=1,
        le=100_000,
        description=(
            "For consecutive_* predicates: required run length. "
            "For bars_since_last_at_least: required minimum bar count "
            "since the last trade's exit bar. "
            "Ignored by last_won / last_lost. "
            "Upper bound raised from 100 to 100_000 in v1.2.B to "
            "accommodate bars_since values — at 15m, 24 hours = 96 bars, "
            "a week = 672 bars, a month = 2_880 bars, all common throttle "
            "windows that exceeded the original consecutive-trade bound."
        ),
    )


class PriorSignalCondition(_StrictModel):
    """v2.0 stateful condition: gates on the most recent evaluated entry signal.

    Where prior_trade sees only completed *trades*, prior_signal sees every
    evaluated entry *signal* — whether it became a trade or was skipped by a
    gate. A skipped signal is scored by a *phantom outcome*: the iterative
    backtest path simulates the trade the entry would have produced and
    records its win/loss. This is what Turtle System 1 needs — a
    skip-after-winner rule built on prior_trade latches shut after the first
    win (a skipped breakout opens no trade, so "the last trade" never
    advances), whereas prior_signal keeps tracking each new breakout.

    Tier 3 (outcome-dependent): evaluated only by the iterative backtest
    path, never vectorbt's from_signals. See
    docs/design/v2-phase-a-stateful-conditions.md section 4.7.
    """

    type: Literal["prior_signal"] = "prior_signal"
    predicate: Literal[
        "last_would_have_won",
        "last_would_have_lost",
        "last_fired",
    ] = Field(
        description=(
            "Which property of the most recent resolved entry signal to gate "
            "on. last_would_have_won / last_would_have_lost test that signal's "
            "outcome — its real trade result if it fired, or a simulated "
            "phantom result if a gate skipped it. last_fired tests whether "
            "that signal became a real trade (true) or was skipped (false)."
        ),
    )


class DayOfWeekCondition(_StrictModel):
    """v1.2.D (2026-05-25) — true only when the current bar's open_ts
    (UTC) weekday is in the configured set.

    Stateless boolean primitive. Weekdays follow pandas convention:
    0 = Monday, 6 = Sunday. ISO calendar UTC throughout. Used for
    weekend-effect strategies (e.g. weekdays=[5,6] for crypto
    weekend-only), Monday-effect patterns (weekdays=[0]), or
    weekdays-only strategies excluding crypto weekends.

    Surfaced as a v1.2 design-pass primitive (design doc §4 v1.2.D)
    in the same family as TimeOfDayCondition (v1.2.C) — near-zero
    marginal cost given C's implementation pattern.

    NOT stateful in the v2 Tier-2 / Tier-3 sense — depends only on
    the current bar's index timestamp.
    """

    type: Literal["day_of_week"] = "day_of_week"
    weekdays: list[int] = Field(
        min_length=1,
        max_length=7,
        description=(
            "Set of allowed weekdays (pandas convention: 0=Monday, "
            "6=Sunday). At least one element required; the engine "
            "evaluates True on any bar whose open_ts.weekday() is in "
            "this list. Validator enforces non-empty, all in [0,6], "
            "and no duplicates."
        ),
    )

    @model_validator(mode="after")
    def _validate_weekdays(self) -> Self:
        for w in self.weekdays:
            if w < 0 or w > 6:
                raise PydanticCustomError(
                    "day_of_week_weekday_out_of_range",
                    "day_of_week.weekdays must each be in [0, 6] "
                    "(0=Monday, 6=Sunday); got {weekday}",
                    {"weekday": w},
                )
        if len(set(self.weekdays)) != len(self.weekdays):
            raise PydanticCustomError(
                "day_of_week_duplicate_weekdays",
                "day_of_week.weekdays must not contain duplicates; "
                "got {weekdays}",
                {"weekdays": self.weekdays},
            )
        return self


class TimeOfDayCondition(_StrictModel):
    """v1.2.C (2026-05-24) — true only when the current bar's open_ts
    (UTC) falls within the configured hour range.

    Stateless boolean primitive. Hours are integer 0..23 UTC; if the
    source describes a strategy in local time (e.g. "5pm-7pm ET"), the
    extractor must convert to UTC before constructing this condition.
    Wrap-around windows (start_hour_utc > end_hour_utc) span midnight,
    e.g. start=22, end=2 means 22, 23, 0, 1, (2 if inclusive_end).

    Surfaced by Hunt 6B (Intraday seasonality, 2026-05-24) — the
    Quantpedia "Hold long during 22:00-23:00 UTC" strategy that the
    extractor couldn't express because the v2 Condition union had no
    hour-of-day variant. The LLM-constructed spec failed strict
    validation and the verdict was downgraded to `not_extractable`.
    Now expressible faithfully.

    NOT stateful in the v2 Tier-2 / Tier-3 sense — depends only on the
    current bar's index timestamp. spec_uses_stateful_v2 stays False
    for any spec that uses TimeOfDayCondition without also using a
    regime_state / prior_trade / prior_signal / ratchet element.
    """

    type: Literal["time_of_day"] = "time_of_day"
    start_hour_utc: int = Field(
        ge=0,
        le=23,
        description="UTC hour at which the window opens (inclusive). 0..23.",
    )
    end_hour_utc: int = Field(
        ge=0,
        le=23,
        description="UTC hour at which the window closes. Inclusive or exclusive per inclusive_end.",
    )
    inclusive_end: bool = Field(
        default=True,
        description=(
            "If True, end_hour_utc is included in the window (start=22, "
            "end=23 -> hours 22 AND 23 fire). If False, end_hour_utc is "
            "excluded (start=22, end=23 -> only hour 22 fires). The "
            "convention defaults to inclusive to match how humans usually "
            "describe time windows ('from 22 to 23 UTC' typically means "
            "BOTH hours)."
        ),
    )


class RSICondition(_StrictModel):
    """v1.3 (2026-06-04) — Wilder's RSI oscillator mean-reversion gate.

    Stateless boolean primitive: evaluates Wilder's RSI(period) on the
    chosen price source and compares it to a fixed threshold using one
    of four comparison modes. Closes the oscillator mean-reversion gap
    where the LLM previously had to hand-build a compare against an
    `indicator(name="rsi")` expression — a faithful but verbose shape.
    RSICondition is the ergonomic, first-class form.

    Comparison semantics (RSI[t] = the bar's RSI value):
      - below:         RSI[t] < threshold
      - above:         RSI[t] > threshold
      - crosses_above: RSI[t-1] <= threshold AND RSI[t] > threshold
                       (RSI crossed UP through the level THIS bar)
      - crosses_below: RSI[t-1] >= threshold AND RSI[t] < threshold
                       (RSI crossed DOWN through the level THIS bar)

    The RSI itself is computed by the shared `ind.rsi(df, period, source)`
    (Wilder, the exact function both backtest engines already use), so a
    spec using RSICondition produces bit-identical RSI to one using an
    `indicator(name="rsi")` compare. NOT stateful in the v2 Tier-2 /
    Tier-3 sense — the RSI recursion lives inside the indicator function
    (just like EMA's); the condition is a stateless compare over its
    output. spec_uses_stateful_v2 stays False for a spec that uses
    RSICondition without a regime_state / prior_trade / prior_signal /
    ratchet element.

    Typical uses: RSI < 30 long entry (oversold mean reversion), RSI > 70
    short-side gate or long exit, RSI crossing 50 as a momentum-regime
    flip.
    """

    type: Literal["rsi"] = "rsi"
    period: int = Field(
        default=14,
        ge=2,
        le=100,
        description=(
            "RSI lookback (Wilder smoothing window). Default 14 is the "
            "classic Wilder setting. Bounds 2..100."
        ),
    )
    threshold: float = Field(
        ge=0,
        le=100,
        description=(
            "RSI level to compare against. RSI is bounded 0..100, so the "
            "threshold must be too. Common levels: 30 (oversold), 70 "
            "(overbought), 50 (momentum midline)."
        ),
    )
    comparison: Literal["below", "above", "crosses_above", "crosses_below"] = Field(
        description=(
            "How RSI relates to threshold. below/above are level tests on "
            "the current bar; crosses_above/crosses_below fire only on the "
            "bar where RSI transitions through the level (no-lookahead: "
            "prev bar vs current bar)."
        ),
    )
    source: Literal["open", "high", "low", "close"] = Field(
        default="close",
        description=(
            "Price series the RSI is computed on. Defaults to close — the "
            "standard convention. Other sources are rare for RSI."
        ),
    )
    timeframe: Timeframe | None = None


class BollingerBandsCondition(_StrictModel):
    """v1.3 — volatility-band mean-reversion + squeeze breakout gate.

    A stateless boolean primitive built on Bollinger Bands. Three
    mutually-exclusive forms select what the condition tests:

      - ``below_lower``: true when ``close`` is strictly below the lower
        band — the classic oversold / mean-reversion-long trigger.
      - ``above_upper``: true when ``close`` is strictly above the upper
        band — overbought / mean-reversion-short (or breakout) trigger.
      - ``squeeze``: true when the band *bandwidth* (upper − lower) sits
        in the LOW tail of its own recent distribution — i.e.
        ``percentile_rolling(bandwidth, squeeze_window) <=
        squeeze_percentile``. A low-volatility coil that often precedes
        an expansion / breakout.

    Bands are computed from ``ind.bollinger(df, period, num_std,
    source)``; the squeeze percentile reuses ``ind.percentile_rolling``,
    the same helper backing PercentileExpr (v1.2.A). Stateless: the value
    at bar *t* depends only on the trailing window ending at *t*, never on
    trade outcomes — so this is NOT a v2 Tier-2 / Tier-3 condition and is
    evaluated bit-identically by the vbt translator and the iterative
    engine via the shared dispatcher.

    The ``squeeze_window`` / ``squeeze_percentile`` pair is required iff
    ``form == "squeeze"`` and forbidden otherwise — the validator pins
    this so a ``below_lower`` / ``above_upper`` condition can never carry
    dangling squeeze params (and vice versa).
    """

    type: Literal["bollinger_bands"] = "bollinger_bands"
    period: int = Field(
        default=20,
        ge=2,
        le=100,
        description="Bollinger Band lookback (SMA + stddev window). 2..100.",
    )
    num_std: float = Field(
        default=2.0,
        gt=0,
        le=5,
        description=(
            "Standard-deviation multiplier for the band width. Classic "
            "Bollinger uses 2.0; (0, 5]."
        ),
    )
    source: Literal["open", "high", "low", "close", "volume"] = Field(
        default="close",
        description="Price column the bands are computed on. Almost always 'close'.",
    )
    form: Literal["below_lower", "above_upper", "squeeze"] = Field(
        description=(
            "Which band test to evaluate. below_lower: close < lower band. "
            "above_upper: close > upper band. squeeze: bandwidth in the low "
            "percentile of its trailing window (a low-volatility coil)."
        ),
    )
    squeeze_window: int | None = Field(
        default=None,
        ge=2,
        le=10_000,
        description=(
            "Trailing window over which the bandwidth percentile is ranked. "
            "Required iff form == 'squeeze'; must be None otherwise. 2..10_000."
        ),
    )
    squeeze_percentile: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "Bandwidth percentile threshold in [0, 1]. The squeeze fires when "
            "the rolling bandwidth percentile is <= this value (e.g. 0.1 = the "
            "narrowest 10% of recent bandwidths). Required iff form == "
            "'squeeze'; must be None otherwise."
        ),
    )

    @model_validator(mode="after")
    def _validate_squeeze_params(self) -> Self:
        is_squeeze = self.form == "squeeze"
        have_window = self.squeeze_window is not None
        have_pct = self.squeeze_percentile is not None
        if is_squeeze and not (have_window and have_pct):
            raise PydanticCustomError(
                "bollinger_bands_squeeze_params_missing",
                "bollinger_bands form='squeeze' requires both "
                "squeeze_window and squeeze_percentile to be set",
            )
        if not is_squeeze and (have_window or have_pct):
            raise PydanticCustomError(
                "bollinger_bands_squeeze_params_forbidden",
                "bollinger_bands squeeze_window / squeeze_percentile are "
                "only valid when form='squeeze'; got form={form}",
                {"form": self.form},
            )
        return self


class ZScoreCondition(_StrictModel):
    """v1.3 (2026-06-04) — statistical mean-reversion gate on the rolling
    z-score of a price source.

    Stateless boolean primitive. The z-score is computed inline from two
    existing whitelist indicators — no new indicator function:

        z[t] = (source[t] - SMA(source, period)[t]) / StdDev(source, period)[t]

    where StdDev is the rolling sample standard deviation (ddof=1, the
    same convention as the ``stddev`` indicator). When the rolling std is
    zero (a perfectly flat window), z is undefined; the engine emits NaN
    there and the condition evaluates False — no spurious signal on a
    dead-flat series.

    The ``form`` selects the mean-reversion trigger shape:

      - ``below_neg``  — z < -threshold. Oversold; the classic long
        mean-reversion entry (price is statistically cheap vs its
        rolling mean).
      - ``above_pos``  — z > +threshold. Overbought; the short
        mean-reversion entry (or a long exit).
      - ``cross_toward_zero`` — z was beyond the ±threshold band on the
        PREVIOUS bar and moved toward zero on THIS bar. The reversion
        TRIGGER: rather than firing on every oversold bar, it fires the
        instant price starts snapping back. True when either
        (z[t-1] <= -threshold and z[t] > z[t-1]) — recovering from
        oversold — or (z[t-1] >= +threshold and z[t] < z[t-1]) —
        recovering from overbought.

    NOT stateful in the v2 Tier-2 / Tier-3 sense — z[t] depends only on a
    trailing window of price, exactly like SMA / StdDev. Evaluated by the
    shared translator dispatcher, so the vbt and iterative engines compute
    bit-identical masks (one helper, two call sites).
    """

    type: Literal["zscore"] = "zscore"
    period: int = Field(
        default=20,
        ge=2,
        le=100,
        description=(
            "Rolling window for the mean and standard deviation. "
            "Minimum 2 (a 1-bar window has zero variance). Typical "
            "mean-reversion lookbacks are 14-50."
        ),
    )
    threshold: float = Field(
        default=2.0,
        gt=0,
        le=20,
        description=(
            "Z-score band edge (in standard deviations). below_neg fires "
            "when z < -threshold; above_pos when z > +threshold; "
            "cross_toward_zero when z was beyond ±threshold last bar and "
            "moved toward zero this bar. Common values: 1.5-2.5."
        ),
    )
    source: Literal["open", "high", "low", "close", "volume"] = Field(
        default="close",
        description="Price column the z-score is computed on (default close).",
    )
    form: Literal["below_neg", "above_pos", "cross_toward_zero"] = Field(
        description=(
            "Trigger shape. below_neg = oversold (z < -threshold, long "
            "mean-reversion entry). above_pos = overbought (z > +threshold). "
            "cross_toward_zero = z was beyond ±threshold last bar and moved "
            "toward zero this bar (the reversion trigger — fires when price "
            "starts snapping back, not on every extreme bar)."
        ),
    )


Condition = Annotated[
    CompareCondition
    | CrossoverCondition
    | WithinLastNBarsCondition
    | RisingCondition
    | FallingCondition
    | CandlePatternCondition
    | AndCondition
    | OrCondition
    | NotCondition
    | RegimeStateCondition
    | PriorTradeCondition
    | PriorSignalCondition
    | TimeOfDayCondition
    | DayOfWeekCondition
    | RSICondition
    | BollingerBandsCondition
    | ZScoreCondition,
    Field(discriminator="type"),
]


# Resolve the recursive references.
WithinLastNBarsCondition.model_rebuild()
AndCondition.model_rebuild()
OrCondition.model_rebuild()
NotCondition.model_rebuild()
RegimeStateCondition.model_rebuild()


__all__ = [
    "AndCondition",
    "BollingerBandsCondition",
    "CandlePatternCondition",
    "CompareCondition",
    "Condition",
    "CrossoverCondition",
    "FallingCondition",
    "NotCondition",
    "OrCondition",
    "PriorSignalCondition",
    "PriorTradeCondition",
    "RSICondition",
    "RegimeStateCondition",
    "RisingCondition",
    "WithinLastNBarsCondition",
    "ZScoreCondition",
]
