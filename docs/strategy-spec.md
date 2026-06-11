# MarketMind AI — Strategy Specification (v1.0)

## Purpose

A Strategy Specification ("spec") is a structured, machine-executable
description of a trading strategy. It is the canonical representation used
throughout MarketMind AI: extracted from source content by the LLM,
validated by the schema, executed by the backtester, displayed by the UI.

The spec is deliberately constrained. It cannot represent every possible
trading strategy — it represents the subset that can be backtested
rigorously. Strategies that cannot fit this schema are not "unsupported by
our tool"; they are strategies that cannot be honestly backtested at all,
because their rules are not precise enough to follow deterministically.

## Scope

### In scope (v1.0)
- Single-instrument, single-direction trades (long-only or short-only, not
  both simultaneously)
- Crypto spot markets via Binance (extensible later to equities, futures)
- Timeframes from 1m to 1d
- Up to 2 timeframes per strategy (primary + filter)
- Indicator-based, threshold-based, and price-pattern strategies
- Up to 1 open position at a time per strategy

### Out of scope (v1.0)
- ICT / SMC / order blocks / fair value gaps / liquidity sweeps
- Harmonic patterns, Elliott Wave, Fibonacci retracements
- Multi-instrument strategies (pairs trading, portfolio rotation)
- Options, futures roll, perpetual funding
- Pyramiding / scaled entries / partial exits
- News and sentiment signals
- Order types other than market and limit
- More than 2 timeframes per strategy

If extraction encounters out-of-scope behavior, the spec is rejected with
a specific message naming the unsupported feature.

## Top-Level Structure

A spec is a JSON object with the following fields:

| Field              | Required | Type                      | Default            |
|--------------------|----------|---------------------------|--------------------|
| schema_version     | yes      | string                    | "1.0"              |
| name               | yes      | string                    |                    |
| description        | no       | string                    | ""                 |
| instrument         | yes      | Instrument                |                    |
| primary_timeframe  | yes      | Timeframe                 |                    |
| filter_timeframe   | no       | Timeframe                 | null               |
| direction          | yes      | "long" \| "short"         |                    |
| entry              | yes      | EntryRules                |                    |
| exit               | yes      | ExitRules                 |                    |
| position_sizing    | no       | PositionSizing            | `{"mode": "fixed_percent_equity", "percent": 1.0}` |
| costs              | no       | CostModel                 | reasonable default |
| filters            | no       | Filter[]                  | []                 |
| metadata           | no       | Metadata                  | {}                 |

## Field Definitions

### Instrument

    {
      "symbol": string,        // e.g. "BTC/USDT", "ETH/USDT"
      "exchange": string,      // "binance" for MVP
      "quote_currency": string // derived from symbol
    }

Only crypto spot pairs are valid in v1.0. Symbol must exist on the
configured exchange. The system maintains a whitelist of supported pairs
(top ~50 by volume) to ensure data quality.

### Timeframe

A string enum from the fixed set:
"1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d"

### Direction

"long" or "short". A v1.0 strategy is one or the other, not both.
"Long/short" strategies in the wild are usually two separate strategies
sharing logic — we model them as two specs.

### EntryRules

    {
      "condition": Condition,         // when to enter
      "order_type": "market" | "limit",
      "limit_offset_pct": number      // see rules below
    }

`limit_offset_pct` is conditionally required by `order_type`:

- **Required** when `order_type == "limit"`. The signed percent offset from
  the close of the signal bar at which to place the limit order. Positive
  values place the limit above the signal close; negative values below.
- **Forbidden** when `order_type == "market"`. Must be absent from the
  payload entirely.
- **Bounds when present**: `-0.05 <= limit_offset_pct <= 0.05` (i.e.,
  within ±5%).

This is enforced as a model-level constraint (cross-field validator), not
just by per-field types — extra-field rejection alone wouldn't catch the
"market order with a limit offset" misuse.

**Sign convention.** `limit_offset_pct` is signed relative to the signal
bar's close. Negative places the limit below close (e.g., `-0.001` =
0.1% below); positive places it above. This convention is
direction-independent — a short strategy with `limit_offset_pct: 0.002`
places the limit 0.2% above close, which would be a more aggressive
short entry.

### ExitRules

    {
      "exits": ExitCondition[]        // ordered list; first to trigger wins
    }

Must contain at least one exit. If multiple exit conditions are armed, the
first one to fire on any bar closes the trade. If two fire on the same
bar, ordering in the list breaks the tie.

### ExitCondition

A discriminated union by `type`:

    { "type": "stop_loss", "method": StopLossMethod }
    { "type": "take_profit", "method": TakeProfitMethod }
    { "type": "condition", "condition": Condition }
    { "type": "time", "max_bars_held": int }

#### StopLossMethod

    { "kind": "percent", "value": 0.05 }
    { "kind": "atr_multiple", "atr_period": 14, "mult": 2 }
    { "kind": "fixed_price", "price": 50000 }
    { "kind": "trailing_percent", "value": 0.03 }
    { "kind": "trailing_atr", "atr_period": 14, "mult": 2 }

**Sign convention for `{ "kind": "percent" }`.** `value` is signed. For
long strategies, positive values place the stop below entry (the expected
case); negative values place it above entry and trigger a
direction-consistency warning. Same logic mirrored for short strategies.

#### TakeProfitMethod

    { "kind": "percent", "value": 0.10 }
    { "kind": "r_multiple", "r": 2 }
    { "kind": "fixed_price", "price": 60000 }

R-multiple TP requires a stop-loss to be defined (otherwise R is
undefined). Validation enforces this.

### PositionSizing

A discriminated union by `mode`:

    { "mode": "fixed_percent_equity", "percent": 0.1 }
    { "mode": "risk_based", "risk_percent": 0.01 }
    { "mode": "fixed_quantity", "quantity": 0.01 }

Default means 100% of available equity per trade; no leverage is supported
in v1.0.

`risk_based` requires a stop-loss to be defined. Position size is computed
as `(equity * risk_percent) / stop_distance`.

### CostModel

    {
      "commission_pct": 0.001,    // 0.1% per side (Binance maker default)
      "slippage_pct": 0.0005      // 0.05% assumed slippage
    }

If omitted, defaults are applied. Backtest reports always show what cost
model was used. Backtests with zero costs are flagged with a prominent
warning.

### Filter[]

Filters gate when the strategy is allowed to trade at all. Each filter is
a condition that must be true for entries to be considered.

    [
      { "type": "session", "hours_utc": [13, 21] },
      { "type": "weekday", "days": [1,2,3,4,5] },
      { "type": "condition", "condition": Condition }
    ]

`hours_utc: [start, end]` means trading is allowed during hour blocks
`start` through `end`, inclusive. e.g. `[13, 21]` = 13:00 through 21:59
UTC. Wrap-around midnight is not supported in v1.0; use two filter
entries instead. Constraints: `0 <= start <= end <= 23`.

### Metadata

    {
      "source_url": string,
      "source_type": "youtube" | "article" | "manual",
      "extracted_by": string,
      "extracted_at": ISO 8601 datetime, timezone-aware, must be UTC,
      "confidence": number,
      "extraction_notes": ExtractionNote[]
    }

`extracted_at` rejects naive datetimes and non-UTC offsets with error code
`metadata_extracted_at_must_be_utc`. Producers should emit UTC; consumers
can be confident they'll never have to convert.

`confidence` is a float in `[0.0, 1.0]` inclusive — the LLM's overall
confidence in the extraction. 1.0 means "this spec exactly captures what
the source described"; 0.0 means "we guessed at everything." Surfaced in
the UI so users can prioritize review of low-confidence extractions.

### ExtractionNote

    {
      "severity": "info" | "warning" | "error",
      "field": string,
      "message": string,
      "confidence": number
    }

`confidence` is a float in `[0.0, 1.0]` inclusive — the LLM's confidence
that this particular note correctly captures the source's intent for the
referenced `field`. Independent of the top-level `Metadata.confidence`.

Notes with severity "error" mean the extraction guessed at something that
may not match user intent and require user review before backtesting.
Notes with severity "warning" are surface-level (defaults applied). Notes
with severity "info" are explanatory.

## The Condition System

Conditions are the heart of the spec. A condition evaluates to a boolean
on each bar and is built recursively from atoms and composites.

Every condition has a `type` field that determines its shape.

### Atomic Conditions

#### compare
Compare two expressions on the current bar.

    {
      "type": "compare",
      "left": Expression,
      "op": ">" | ">=" | "<" | "<=" | "==",
      "right": Expression,
      "timeframe": Timeframe   // optional; defaults to primary
    }

#### crossover
The `series` value crosses `threshold` in the specified direction.
"Crosses above" means: previous bar series ≤ threshold AND current bar
series > threshold.

    {
      "type": "crossover",
      "series": Expression,
      "threshold": Expression,
      "direction": "above" | "below",
      "timeframe": Timeframe
    }

#### within_last_n_bars
A condition was true on at least one of the last N bars (inclusive of
current).

    {
      "type": "within_last_n_bars",
      "condition": Condition,
      "n": int
    }

#### rising / falling
A series is monotonically rising (or falling) over the last N bars, or
simply higher (lower) than N bars ago.

    {
      "type": "rising",
      "series": Expression,
      "lookback": int,
      "strict": bool
    }

#### candle_pattern
A whitelisted candle pattern formed on the current bar.

    {
      "type": "candle_pattern",
      "pattern": "bullish_engulfing" | "bearish_engulfing" | "hammer"
               | "shooting_star" | "doji" | "bullish_pinbar" | "bearish_pinbar",
      "timeframe": Timeframe   // optional; defaults to primary
    }

Pattern detection uses TA-Lib's standard definitions. See
<https://ta-lib.org/function.html> for exact geometric rules.
Disagreements with TA-Lib's definitions are out of scope for v1.0.

Like `compare` and `crossover`, `candle_pattern` may include an optional
`timeframe` field to check the pattern on the filter timeframe rather
than the primary.

### Composite Conditions

#### and, or

    { "type": "and", "conditions": Condition[] }
    { "type": "or", "conditions": Condition[] }

#### not

    { "type": "not", "condition": Condition }

Arbitrary nesting is permitted. Validation rejects empty and/or lists.

## Expressions

Expressions appear on the `left` and `right` of comparisons and as
`series` / `threshold` in crossovers. An expression evaluates to a number
on each bar.

### Price Series

    { "kind": "price", "field": "open" | "high" | "low" | "close" | "volume" }

### Indicator

References a whitelisted indicator with its parameters.

    {
      "kind": "indicator",
      "name": IndicatorName,
      "params": { ... },
      "source": "close",
      "component": "..."   // only for multi-output indicators
    }

#### Whitelisted Indicators

| Name        | Params                                       | Returns                    |
|-------------|----------------------------------------------|----------------------------|
| sma         | period: int                                  | scalar series              |
| ema         | period: int                                  | scalar series              |
| wma         | period: int                                  | scalar series              |
| rsi         | period: int (default 14)                     | scalar series              |
| macd        | fast: int, slow: int, signal: int            | object: line/signal/hist   |
| stochastic  | k: int, d: int, smooth: int                  | object: k/d                |
| atr         | period: int                                  | scalar series              |
| bollinger   | period: int, std_dev: float                  | object: upper/middle/lower |
| stddev      | period: int                                  | scalar series              |
| volume_sma  | period: int                                  | scalar series              |
| obv         | (none)                                       | scalar series              |
| vwap        | session_anchored: bool                       | scalar series              |
| highest     | period: int, source: "high"\|"close"\|...    | scalar series              |
| lowest      | period: int, source: "low"\|"close"\|...     | scalar series              |
| returns     | period: int                                  | scalar series              |
| supertrend  | atr_period: int, multiplier: float           | object: value/direction    |
| adx         | period: int                                  | scalar series              |
| keltner     | period: int, atr_period: int, multiplier: float | object: upper/middle/lower |
| psar        | step: float, max_step: float                 | object: value/direction    |

For object-returning indicators, reference a component:

    { "kind": "indicator", "name": "macd", "params": {...}, "component": "hist" }
    { "kind": "indicator", "name": "bollinger", "params": {...}, "component": "upper" }

**`returns(period=N)`** is computed as `(close[t] - close[t-N]) / close[t-N]`.
The default `period=1` gives bar-over-bar returns.

**`vwap(session_anchored=true)`** resets at the start of each UTC trading
day. **`vwap(session_anchored=false)`** uses a rolling 20-bar window
over the strategy's primary timeframe. (Rolling VWAP without a natural
anchor is unusual; flagged with a warning by the backtester in v1.0.)

### Constant

    { "kind": "constant", "value": 30 }

### Lagged Expression

Useful for "yesterday's close" or "the highest high of the last 20 bars,
not including the current bar".

    { "kind": "lagged", "expression": Expression, "bars_ago": int }

### Scaled Expression

Multiplies an expression by a constant factor. Useful for thresholds
derived from indicators (e.g., "half the 20-bar volume average", "1.5x
ATR", "0.98 of VWAP").

    { "kind": "scaled", "expression": Expression, "factor": number }

Constraints: `factor` is a non-zero float, range `[-1000.0, 1000.0]`.
Factor of `1.0` is permitted but redundant.

## Multi-Timeframe Semantics

A strategy declares a `primary_timeframe` where trades are placed.
Optionally a `filter_timeframe` (must be higher than primary; e.g.,
primary 15m + filter 4h) for trend or regime conditions.

Any condition or expression may include a `timeframe` field. If omitted,
it defaults to the primary timeframe. On the filter timeframe, only the
most recently *closed* bar's value is used at any point on the primary
timeframe — no look-ahead.

Example structure for a 15m strategy with 4h trend filter is shown in the
condition section above.

## Validation Rules

The schema enforces structural validity. The validator additionally
enforces:

1. Required when referenced. r_multiple TP requires a stop-loss.
   risk_based sizing requires a stop-loss.
2. At least one exit. exits must be non-empty.
3. Filter timeframe must be higher than primary. Reject 4h primary with
   15m filter.
4. Indicator parameters within sane bounds. RSI period must be 2–100,
   etc. Hard limits prevent extraction nonsense like RSI(0) or SMA(99999).
5. Composite conditions non-empty. and/or must have ≥1 child.
6. Direction consistency. A direction "long" strategy's exits should
   reference long-position semantics (stop below entry, etc.). Soft
   warning, not hard rejection.
7. No look-ahead. All conditions reference past or current bar only.
   Lagged expressions enforce non-negative bars_ago.

Validation errors are typed (ValidationError with field path) so the UI
can highlight specific fields.

## Example: Golden Cross

    {
      "schema_version": "1.0",
      "name": "Golden Cross",
      "description": "Buy when SMA(50) crosses above SMA(200); exit on opposite cross.",
      "instrument": { "symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT" },
      "primary_timeframe": "1d",
      "direction": "long",
      "entry": {
        "condition": {
          "type": "crossover",
          "series": { "kind": "indicator", "name": "sma", "params": { "period": 50 } },
          "threshold": { "kind": "indicator", "name": "sma", "params": { "period": 200 } },
          "direction": "above"
        },
        "order_type": "market"
      },
      "exit": {
        "exits": [
          {
            "type": "condition",
            "condition": {
              "type": "crossover",
              "series": { "kind": "indicator", "name": "sma", "params": { "period": 50 } },
              "threshold": { "kind": "indicator", "name": "sma", "params": { "period": 200 } },
              "direction": "below"
            }
          }
        ]
      },
      "position_sizing": { "mode": "fixed_percent_equity", "percent": 1.0 }
    }

## Example: 20-Bar Breakout with Volume

    {
      "schema_version": "1.0",
      "name": "20-Bar Breakout with Volume",
      "description": "Long on breakout of 20-bar high with above-average volume; risk 1% per trade; 2R target.",
      "instrument": { "symbol": "SOL/USDT", "exchange": "binance", "quote_currency": "USDT" },
      "primary_timeframe": "1h",
      "direction": "long",
      "entry": {
        "condition": {
          "type": "and",
          "conditions": [
            {
              "type": "compare",
              "left": { "kind": "price", "field": "close" },
              "op": ">",
              "right": {
                "kind": "lagged",
                "expression": { "kind": "indicator", "name": "highest", "params": { "period": 20, "source": "high" } },
                "bars_ago": 1
              }
            },
            {
              "type": "compare",
              "left": { "kind": "price", "field": "volume" },
              "op": ">",
              "right": { "kind": "indicator", "name": "volume_sma", "params": { "period": 20 } }
            }
          ]
        },
        "order_type": "market"
      },
      "exit": {
        "exits": [
          { "type": "stop_loss", "method": { "kind": "atr_multiple", "atr_period": 14, "mult": 1.5 } },
          { "type": "take_profit", "method": { "kind": "r_multiple", "r": 2 } }
        ]
      },
      "position_sizing": { "mode": "risk_based", "risk_percent": 0.01 }
    }

## Indicator Parameter Bounds

| Indicator   | Parameter    | Min  | Max  | Default |
|-------------|--------------|------|------|---------|
| sma         | period       | 2    | 500  | —       |
| ema         | period       | 2    | 500  | —       |
| wma         | period       | 2    | 500  | —       |
| rsi         | period       | 2    | 100  | 14      |
| macd        | fast         | 2    | 100  | 12      |
| macd        | slow         | 3    | 200  | 26      |
| macd        | signal       | 2    | 50   | 9       |
| stochastic  | k            | 2    | 100  | 14      |
| stochastic  | d            | 1    | 20   | 3       |
| stochastic  | smooth       | 1    | 10   | 3       |
| atr         | period       | 2    | 100  | 14      |
| bollinger   | period       | 5    | 200  | 20      |
| bollinger   | std_dev      | 0.5  | 5.0  | 2.0     |
| stddev      | period       | 2    | 200  | 20      |
| volume_sma  | period       | 2    | 200  | 20      |
| vwap        | (none)       | —    | —    | —       |
| obv         | (none)       | —    | —    | —       |
| highest     | period       | 2    | 500  | —       |
| lowest      | period       | 2    | 500  | —       |
| returns     | period       | 1    | 100  | 1       |
| supertrend  | atr_period   | 2    | 100  | 10      |
| supertrend  | multiplier   | 1.0  | 10.0 | 3.0     |
| adx         | period       | 2    | 100  | 14      |
| keltner     | period       | 5    | 200  | 20      |
| keltner     | atr_period   | 2    | 100  | 10      |
| keltner     | multiplier   | 1.0  | 10.0 | 2.0     |
| psar        | step         | 0.01 | 0.1  | 0.02    |
| psar        | max_step     | 0.1  | 1.0  | 0.2     |

Additional cross-parameter constraint for MACD: `slow > fast` (the slow
EMA period must exceed the fast EMA period). Validation enforces this
as a model-level constraint. Bounds are deliberately generous; the goal
is to catch extraction errors, not to enforce "good" parameters.

## Versioning

schema_version is required on every spec. v1.0 specs are immutable —
breaking changes require v2.0, with a migration utility provided.
Backwards-compatible additions (new optional fields, new enum values that
existing specs don't reference) do not require a version bump.

A future ADR will document the v1→v2 migration when needed.

## What this spec deliberately does NOT do

- It does not describe execution mechanics (order routing, retry policy).
  The backtester applies a fixed model; live execution is out of scope.
- It does not describe the user's intent in trading terms. We don't ask
  "what kind of trader are you?" We just need rules.
- It does not represent subjective concepts ("trade only when the market
  feels strong"). If it isn't in the spec, it isn't part of the strategy.
- It does not support stateful strategy logic across trades (e.g., "after
  3 losses in a row, reduce size by half"). v2.0 territory.
