"""Pure-function implementations of every indicator on the Phase 1 whitelist.

Each function takes an OHLCV DataFrame plus the indicator's parameters
and returns either a pandas Series (single-output indicators) or a
DataFrame (multi-output indicators like MACD / Bollinger / Stochastic).

We mix three strategies:

  - pandas direct (SMA, WMA, stddev, volume_sma, highest, lowest,
    returns) — the math is short and unambiguous; an extra dependency
    would obscure rather than clarify.
  - `ta` library (EMA, RSI, MACD, Stochastic, ATR, Bollinger, OBV) —
    these need Wilder's smoothing or other definition-sensitive math.
    `ta` matches the conventions our extracted strategies expect.
  - Hand-rolled (VWAP, candle patterns) — VWAP has session-anchor
    semantics the `ta` library doesn't model the same way; candle
    patterns aren't in `ta` at all (TA-Lib has them but its C
    dependency is painful in Docker — flagged in the spec doc).

All functions assume a clean OHLCV DataFrame from market_data.get_market_data:
  - DatetimeIndex (tz-aware UTC, sorted, no gaps)
  - columns: open, high, low, close, volume (all float64)

No look-ahead protection here — that's the translator's job. These
functions just compute the indicator across the full series; the
translator is responsible for shifting / alignment when stitching them
into signals.
"""

from __future__ import annotations

from typing import Literal, cast

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, ADXIndicator, EMAIndicator, PSARIndicator
from ta.volatility import AverageTrueRange, BollingerBands, KeltnerChannel
from ta.volume import OnBalanceVolumeIndicator

PriceSource = Literal["open", "high", "low", "close", "volume"]


def column(data: pd.DataFrame, name: str) -> pd.Series:
    """Type-narrowed column accessor.

    pandas-stubs types `df[name]` as the union `DataFrame | Series` (a
    list-valued `name` returns a DataFrame). All our indicator code
    passes a single string column name, so the runtime type is always
    a Series. The cast is the simplest way to tell pyright that.
    """
    return cast("pd.Series", data[name])


def as_series(x: object) -> pd.Series:
    """Cast an indicator-builder return to Series.

    Many pandas operations (`Series.rolling(...).mean()`,
    `Series.pct_change(...)`, library wrappers) are typed by pandas-
    stubs as a union of DataFrame | Series | Unknown. At runtime every
    indicator in this module produces a Series. Wrapping returns in
    `as_series(...)` keeps pyright happy without sprinkling casts
    inline. Exported (not module-private) so the translator and the
    engine can reuse the same narrowing helper.
    """
    return cast("pd.Series", x)


# Module-private aliases — most callers in this file use these for brevity.
_col = column
_s = as_series


# ---- pandas-direct indicators -----------------------------------------------


def sma(data: pd.DataFrame, period: int, source: PriceSource = "close") -> pd.Series:
    """Simple Moving Average — arithmetic mean over the lookback window."""
    return _s(_col(data, source).rolling(window=period, min_periods=period).mean())


def wma(data: pd.DataFrame, period: int, source: PriceSource = "close") -> pd.Series:
    """Weighted Moving Average: weight i runs 1..period (older to newer).

    WMA(t) = (1*x[t-n+1] + 2*x[t-n+2] + ... + n*x[t]) / (1+2+...+n).
    Implementation uses rolling().apply with raw=True for a NumPy-array
    callback — substantially faster than the default Python-object path.
    """
    weights = np.arange(1, period + 1, dtype=float)
    weight_sum = float(weights.sum())

    def _w(window: np.ndarray) -> float:
        return float((window * weights).sum() / weight_sum)

    return _s(_col(data, source).rolling(window=period, min_periods=period).apply(_w, raw=True))


def stddev(data: pd.DataFrame, period: int, source: PriceSource = "close") -> pd.Series:
    """Rolling sample standard deviation."""
    return _s(_col(data, source).rolling(window=period, min_periods=period).std(ddof=1))


def volume_sma(data: pd.DataFrame, period: int) -> pd.Series:
    """SMA of the volume column."""
    return _s(_col(data, "volume").rolling(window=period, min_periods=period).mean())


def highest(data: pd.DataFrame, period: int, source: PriceSource = "high") -> pd.Series:
    """Rolling max over the lookback. For breakout strategies, callers
    usually source from `high` (the highest swing point in the window).
    """
    return _s(_col(data, source).rolling(window=period, min_periods=period).max())


def lowest(data: pd.DataFrame, period: int, source: PriceSource = "low") -> pd.Series:
    """Rolling min. Symmetric counterpart to `highest`."""
    return _s(_col(data, source).rolling(window=period, min_periods=period).min())


def returns(data: pd.DataFrame, period: int = 1, source: PriceSource = "close") -> pd.Series:
    """N-period percentage return: (close[t] / close[t-n]) - 1."""
    return _s(_col(data, source).pct_change(periods=period, fill_method=None))


def percentile_rolling(series: pd.Series, window: int) -> pd.Series:
    """Rolling empirical percentile (0..1) of ``series`` at each bar.

    For each bar ``t``, returns the rank-as-fraction of ``series[t]``
    within the trailing window ``series[t-window+1 .. t]``. A value of
    1.0 means "highest in the window", 0.0 means "lowest", 0.5 means
    "median". Ties resolve via pandas' default ``rank()`` average rule.

    The first ``window - 1`` bars produce NaN — strict
    ``min_periods=window`` matches the convention every other rolling
    indicator in this module uses (sma, atr, highest, lowest). NaN
    comparisons evaluate to False downstream, so a strategy using
    percentile simply doesn't fire during its warmup.

    Pure functional reduction; not stateful in the Tier-2/Tier-3 sense.
    Evaluated identically by the vbt translator path and the iterative
    engine via the shared ``_eval_expression`` dispatcher — bit-identity
    by construction (one helper, two call sites).

    v1.2.A primitive for the PercentileExpr Expression variant. See
    docs/design/v1.2-schema-additions.md §4.
    """
    return _s(
        series.rolling(window=window, min_periods=window).rank(pct=True),
    )


# ---- `ta`-backed indicators ------------------------------------------------


def ema(data: pd.DataFrame, period: int, source: PriceSource = "close") -> pd.Series:
    """Exponential Moving Average using Wilder's recursive form."""
    indicator = EMAIndicator(close=_col(data, source), window=period, fillna=False)
    return indicator.ema_indicator()


def rsi(data: pd.DataFrame, period: int, source: PriceSource = "close") -> pd.Series:
    """Wilder's RSI."""
    indicator = RSIIndicator(close=_col(data, source), window=period, fillna=False)
    return indicator.rsi()


def macd(
    data: pd.DataFrame,
    fast: int,
    slow: int,
    signal: int,
    source: PriceSource = "close",
) -> pd.DataFrame:
    """MACD: returns DataFrame with columns line/signal/hist.

    line = EMA(close, fast) - EMA(close, slow)
    signal = EMA(line, signal_period)
    hist = line - signal
    """
    indicator = MACD(
        close=_col(data, source),
        window_slow=slow,
        window_fast=fast,
        window_sign=signal,
        fillna=False,
    )
    return pd.DataFrame(
        {
            "line": indicator.macd(),
            "signal": indicator.macd_signal(),
            "hist": indicator.macd_diff(),
        },
    )


def stochastic(
    data: pd.DataFrame,
    k: int,
    d: int,
    smooth: int,
) -> pd.DataFrame:
    """Stochastic %K / %D. Returns columns k / d.

    %K = 100 * (close - lowest_low) / (highest_high - lowest_low) over k periods
    %K is then smoothed by `smooth`; %D is an SMA of smoothed %K over `d` periods.
    """
    indicator = StochasticOscillator(
        high=_col(data, "high"),
        low=_col(data, "low"),
        close=_col(data, "close"),
        window=k,
        smooth_window=smooth,
        fillna=False,
    )
    raw_k = indicator.stoch()
    smoothed_d = raw_k.rolling(window=d, min_periods=d).mean()
    return pd.DataFrame({"k": raw_k, "d": smoothed_d})


def atr(data: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    indicator = AverageTrueRange(
        high=_col(data, "high"),
        low=_col(data, "low"),
        close=_col(data, "close"),
        window=period,
        fillna=False,
    )
    return indicator.average_true_range()


def bollinger(
    data: pd.DataFrame,
    period: int,
    std_dev: float,
    source: PriceSource = "close",
) -> pd.DataFrame:
    """Bollinger bands. Returns columns upper / middle / lower.

    middle = SMA(close, period); upper = middle + std_dev * stddev;
    lower = middle - std_dev * stddev.
    """
    indicator = BollingerBands(
        close=_col(data, source),
        window=period,
        window_dev=std_dev,  # type: ignore[arg-type]  # ta's stub types this as int; floats work in practice
        fillna=False,
    )
    return pd.DataFrame(
        {
            "upper": indicator.bollinger_hband(),
            "middle": indicator.bollinger_mavg(),
            "lower": indicator.bollinger_lband(),
        },
    )


def psar(
    data: pd.DataFrame,
    step: float,
    max_step: float,
) -> pd.DataFrame:
    """Parabolic SAR — Wilder's trend-following trailing-stop indicator.

    Returns a DataFrame with two columns:
      - ``value``: the SAR price at each bar (a trailing stop level).
      - ``direction``: ``+1.0`` when SAR is below price (uptrend), ``-1.0``
        when above (downtrend). NaN during the ATR-style warmup.

    Stateful at the indicator level — the acceleration factor accumulates
    while a trend persists. The recursion lives inside `ta`'s
    implementation, exactly like Supertrend's lives inside our hand-rolled
    function; nothing threads through the translator.
    """
    indicator = PSARIndicator(
        high=_col(data, "high"),
        low=_col(data, "low"),
        close=_col(data, "close"),
        step=step,
        max_step=max_step,
        fillna=False,
    )
    sar = indicator.psar()
    close = _col(data, "close")
    # Direction from the canonical PSAR semantic: SAR below price = uptrend,
    # above price = downtrend. NaN where the SAR itself is NaN (warmup) or
    # exactly equal to close (a flip-boundary edge).
    direction = np.where(
        sar.notna() & (sar < close),
        1.0,
        np.where(sar.notna() & (sar > close), -1.0, np.nan),
    )
    return pd.DataFrame(
        {"value": sar, "direction": pd.Series(direction, index=data.index)},
    )


def keltner(
    data: pd.DataFrame,
    period: int,
    atr_period: int,
    multiplier: float,
) -> pd.DataFrame:
    """Keltner Channels — EMA-based middle band ± multiplier × ATR.

    Returns a DataFrame with columns upper / middle / lower (mirrors
    bollinger). `original_version=False` selects the modern Raschke
    EMA-based middle band; ta's default True is the 1960 SMA original,
    essentially never used in practice (see docs/design/v1.1-indicators-
    adx-keltner-psar.md Q2).
    """
    indicator = KeltnerChannel(
        high=_col(data, "high"),
        low=_col(data, "low"),
        close=_col(data, "close"),
        window=period,
        window_atr=atr_period,
        multiplier=multiplier,  # type: ignore[arg-type]  # ta types as int; floats work in practice (same as bollinger.std_dev)
        fillna=False,
        original_version=False,
    )
    return pd.DataFrame(
        {
            "upper": indicator.keltner_channel_hband(),
            "middle": indicator.keltner_channel_mband(),
            "lower": indicator.keltner_channel_lband(),
        },
    )


def adx(data: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's ADX (Average Directional Index).

    Trend-strength indicator on a 0–100 scale; the convention is ADX > 25
    is "trending". Direction (+DI / -DI) is intentionally not exposed —
    this is the single-output scalar most ADX strategies actually use.
    Backed by `ta.trend.ADXIndicator` (Wilder's smoothing); reference
    test asserts bit-identical match.
    """
    indicator = ADXIndicator(
        high=_col(data, "high"),
        low=_col(data, "low"),
        close=_col(data, "close"),
        window=period,
        fillna=False,
    )
    return indicator.adx()


def obv(data: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — cumulative volume signed by close-vs-prev-close."""
    indicator = OnBalanceVolumeIndicator(
        close=_col(data, "close"),
        volume=_col(data, "volume"),
        fillna=False,
    )
    return indicator.on_balance_volume()


# ---- Supertrend (hand-rolled, ATR-based, recursive) ------------------------


def supertrend(
    data: pd.DataFrame,
    atr_period: int,
    multiplier: float,
) -> pd.DataFrame:
    """Supertrend — a trailing trend band plus its direction.

    Returns a DataFrame with two columns:
      - ``value``: the active Supertrend line (the trailing band price) —
        the lower band in an uptrend, the upper band in a downtrend.
      - ``direction``: ``+1.0`` in an uptrend (line below price),
        ``-1.0`` in a downtrend (line above price).

    Stateful at the indicator level: the band and direction at bar *t*
    depend on bar *t-1*. The recursion is sequential — like `ema`, it is
    inherently a loop — and lives entirely inside this function; nothing
    threads through the translator. No library in the stack ships a
    Supertrend (see docs/design/v1.1-indicator-supertrend.md), so this is
    a hand-rolled implementation of the canonical algorithm.

    Warmup bars (where ATR is undefined) emit NaN for both columns. The
    first valid bar seeds direction from close vs the (high+low)/2 basis.
    """
    high = _col(data, "high")
    low = _col(data, "low")
    hl2 = ((high + low) / 2.0).to_numpy()
    close_arr = _col(data, "close").to_numpy()
    atr_arr = atr(data, atr_period).to_numpy()

    basic_upper = hl2 + multiplier * atr_arr
    basic_lower = hl2 - multiplier * atr_arr

    n = len(data)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    value = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    prev: int | None = None  # index of the previous non-warmup bar
    for t in range(n):
        if np.isnan(atr_arr[t]):
            continue
        if prev is None:
            # First valid bar — seed: above the hl2 basis ⇒ uptrend.
            final_upper[t] = basic_upper[t]
            final_lower[t] = basic_lower[t]
            up = close_arr[t] > hl2[t]
        else:
            # Final bands ratchet — they only loosen when price breaks them.
            final_upper[t] = (
                basic_upper[t]
                if basic_upper[t] < final_upper[prev]
                or close_arr[prev] > final_upper[prev]
                else final_upper[prev]
            )
            final_lower[t] = (
                basic_lower[t]
                if basic_lower[t] > final_lower[prev]
                or close_arr[prev] < final_lower[prev]
                else final_lower[prev]
            )
            # Direction flips when close breaks the band it was tracking.
            if direction[prev] == -1.0:
                up = close_arr[t] > final_upper[t]
            else:
                up = close_arr[t] >= final_lower[t]
        direction[t] = 1.0 if up else -1.0
        value[t] = final_lower[t] if up else final_upper[t]
        prev = t

    return pd.DataFrame({"value": value, "direction": direction}, index=data.index)


# ---- VWAP (hand-rolled, session-anchored) ----------------------------------


def vwap(data: pd.DataFrame, session_anchored: bool = True) -> pd.Series:
    """Volume-Weighted Average Price.

    session_anchored=True: VWAP resets at UTC midnight each day, matching
    the most common "session VWAP" semantics for crypto on the daily
    session boundary. Computed as the cumulative sum of (typical_price *
    volume) divided by cumulative volume, within each UTC date group.

    session_anchored=False: a single cumulative VWAP over the whole
    series. Rarely useful (the value just drifts towards the mean over
    long histories) but supported for completeness.

    typical_price = (high + low + close) / 3 — the standard "TP" used in
    the textbook VWAP formula.
    """
    high = _col(data, "high")
    low = _col(data, "low")
    close = _col(data, "close")
    volume = _col(data, "volume")
    tp = (high + low + close) / 3.0
    tp_vol = tp * volume
    if session_anchored:
        # Group by UTC date. Each group's cumsum resets at midnight.
        idx = data.index
        assert isinstance(idx, pd.DatetimeIndex)
        # `.date` exists on DatetimeIndex at runtime; pandas-stubs
        # doesn't expose it, so reach via the underlying ndarray.
        tz_idx = idx.tz_convert("UTC") if idx.tz is not None else idx
        # `.normalize()` exists on DatetimeIndex but pandas-stubs lacks it.
        normalized = cast("pd.DatetimeIndex", tz_idx.normalize())  # type: ignore[attr-defined]
        date_series = pd.Series(normalized.to_numpy(), index=idx)
        cum_tp_vol = _s(tp_vol.groupby(date_series).cumsum())
        cum_vol = _s(volume.groupby(date_series).cumsum())
    else:
        cum_tp_vol = tp_vol.cumsum()
        cum_vol = volume.cumsum()
    # Avoid div-by-zero for a fresh-session first bar where volume might
    # be zero. NaN there is the honest answer.
    return cum_tp_vol / cum_vol.where(cum_vol != 0)


# ---- Candle patterns (hand-rolled) -----------------------------------------
#
# Definitions intentionally simple. The TA-Lib equivalents are more
# baroque; for v1.0 these capture the gross shape and avoid TA-Lib's C
# dependency. Documented as such in the strategy-spec doc.

_DOJI_BODY_FRACTION = 0.05  # body ≤ 5% of range qualifies as doji
_PIN_TAIL_TO_BODY = 2.0  # pin bar: tail length >= 2x body


def _body(o: pd.Series, c: pd.Series) -> pd.Series:
    return (c - o).abs()


def _ohlc(data: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    return _col(data, "open"), _col(data, "high"), _col(data, "low"), _col(data, "close")


def doji(data: pd.DataFrame) -> pd.Series:
    """Doji: open ≈ close, body fraction below threshold."""
    o, h, low, c = _ohlc(data)
    body = _body(o, c)
    rng = h - low
    # rng == 0 (all four prices identical) is rare but possible — treat
    # as non-doji rather than divide-by-zero.
    return (body / rng.where(rng != 0)).fillna(1.0) <= _DOJI_BODY_FRACTION


def hammer(data: pd.DataFrame) -> pd.Series:
    """Bullish hammer: small body near top, long lower wick (>= 2x body),
    little/no upper wick. Position-of-bar in a downtrend not enforced
    here — that's the strategy's job to combine with a trend filter.
    """
    o, h, low, c = _ohlc(data)
    body = _body(o, c)
    body_top = pd.concat([o, c], axis=1).max(axis=1)
    body_bot = pd.concat([o, c], axis=1).min(axis=1)
    upper_wick = h - body_top
    lower_wick = body_bot - low
    return (lower_wick >= _PIN_TAIL_TO_BODY * body) & (upper_wick <= body)


def shooting_star(data: pd.DataFrame) -> pd.Series:
    """Bearish shooting star: small body near bottom, long upper wick,
    little/no lower wick. Mirror of hammer.
    """
    o, h, low, c = _ohlc(data)
    body = _body(o, c)
    body_top = pd.concat([o, c], axis=1).max(axis=1)
    body_bot = pd.concat([o, c], axis=1).min(axis=1)
    upper_wick = h - body_top
    lower_wick = body_bot - low
    return (upper_wick >= _PIN_TAIL_TO_BODY * body) & (lower_wick <= body)


def bullish_engulfing(data: pd.DataFrame) -> pd.Series:
    """Previous bar bearish (close < open), current bar bullish AND
    current body fully engulfs previous body.
    """
    o, _h, _low, c = _ohlc(data)
    prev_o = o.shift(1)
    prev_c = c.shift(1)
    prev_bearish = prev_c < prev_o
    cur_bullish = c > o
    engulfs = (c >= prev_o) & (o <= prev_c)
    return prev_bearish & cur_bullish & engulfs


def bearish_engulfing(data: pd.DataFrame) -> pd.Series:
    """Mirror of bullish_engulfing."""
    o, _h, _low, c = _ohlc(data)
    prev_o = o.shift(1)
    prev_c = c.shift(1)
    prev_bullish = prev_c > prev_o
    cur_bearish = c < o
    engulfs = (o >= prev_c) & (c <= prev_o)
    return prev_bullish & cur_bearish & engulfs


def bullish_pinbar(data: pd.DataFrame) -> pd.Series:
    """Bullish pin bar: same shape as hammer (long lower wick), not
    requiring a specific trend context. v1.0 makes these synonyms.
    """
    return hammer(data)


def bearish_pinbar(data: pd.DataFrame) -> pd.Series:
    """Bearish pin bar — same shape as shooting_star."""
    return shooting_star(data)


__all__ = [
    "PriceSource",
    "adx",
    "atr",
    "bearish_engulfing",
    "bearish_pinbar",
    "bollinger",
    "bullish_engulfing",
    "bullish_pinbar",
    "doji",
    "ema",
    "hammer",
    "highest",
    "keltner",
    "lowest",
    "macd",
    "obv",
    "psar",
    "returns",
    "rsi",
    "shooting_star",
    "sma",
    "stddev",
    "stochastic",
    "supertrend",
    "volume_sma",
    "vwap",
    "wma",
]
