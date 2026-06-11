# Strategy fixtures

Reference specs for testing the StrategySpec schema. Each file is a JSON
document conforming to `docs/strategy-spec.md` v1.0. The valid/ fixtures
must round-trip cleanly; the invalid/ fixtures must produce a specific
error code and message documented in their sibling `.expected_error.json`.

## Valid (`valid/`)

**01_golden_cross.json** — The canonical SMA(50)/SMA(200) trend strategy on
BTC/USDT daily. Single condition entry and exit (opposite crossover);
100% equity sizing; market orders. Mirrors the Golden Cross example in
the spec document verbatim.

**02_rsi_mean_reversion.json** — ETH/USDT 4h, long when RSI(14) crosses
below 30 (oversold), exits on RSI crossing above 70 OR a 5% percent
stop. Demonstrates: constant-threshold crossovers, multi-exit ordering,
and a non-default `fixed_percent_equity` percent.

**03_breakout_with_volume.json** — SOL/USDT 1h Donchian breakout with a
volume confirmation filter and an ATR stop / 2R target. Mirrors the
20-Bar Breakout example in the spec document verbatim. Exercises
`lagged` expressions (highest-high excluding current bar),
`risk_based` sizing, and `r_multiple` TP.

**04_mtf_trend_pullback.json** — BTC/USDT 15m + 1h trend filter; an AND
of (rising 1h EMA, recent touch of primary 20-EMA, bullish engulfing
candle) gated by ATR stop and 3R target. Exercises the
multi-timeframe semantics, `rising` condition, `within_last_n_bars`,
and `candle_pattern`.

**05_bollinger_breakout.json** — SOL/USDT 1h Bollinger breakout with a
trailing-percent stop and a time-based exit at 48 bars. Demonstrates
multi-output indicator components (`upper` of Bollinger),
`fixed_quantity` sizing, and the `time` exit type.

**06_short_trend_continuation.json** — BNB/USDT 4h short on EMA(20) <
EMA(50) and price below EMA(20); exit on golden cross or 1.5×ATR
stop. Exercises the `direction: short` path and exit ordering.

**07_macd_with_session_filter.json** — ETH/USDT 1h MACD-histogram cross
above zero, placed as a limit order 0.1% below the signal close.
Trades restricted to weekdays during the 13:00–21:59 UTC window.
Exercises `limit` orders with `limit_offset_pct`, both session and
weekday filters, and component-indexed MACD (`hist`).

**08_vwap_reversion.json** — BTC/USDT 5m: long reversion when the
12-bar return is below −1.5%, close is below session VWAP, and volume
is above half its 20-bar average. Exits at VWAP or a 1% stop. The
canonical exercise of the new `scaled` expression type (half the
volume_sma), plus session-anchored VWAP and a `not` wrapper.

## Invalid (`invalid/`)

Each invalid fixture has a sibling `.expected_error.json` describing the
specific error code, field path, and required message substring.

**01_r_multiple_without_stop.json** — Long strategy with an r_multiple
take-profit but no stop_loss exit. r_multiple's R is undefined without
a stop. Expected error_code `r_multiple_requires_stop_loss`.

**02_risk_based_without_stop.json** — Long strategy with `risk_based`
position sizing but no stop_loss exit. Risk-based sizing needs
stop_distance, which requires a stop. Expected error_code
`risk_based_requires_stop_loss`.

**03_filter_tf_lower_than_primary.json** — `primary_timeframe=4h`,
`filter_timeframe=15m`. The filter timeframe must be strictly higher.
Expected error_code `filter_tf_must_be_higher`.

**04_indicator_param_out_of_bounds.json** — An RSI indicator with
`period=1`, below the [2, 100] valid range from the Indicator
Parameter Bounds table. Expected error_code
`indicator_param_out_of_bounds` with a message naming the offending
parameter.
