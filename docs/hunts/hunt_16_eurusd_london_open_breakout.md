# Hunt 16 — EUR/USD London Open Breakout (1H)

**Phase:** C.7 (first FX strategy seed)
**Symbol:** EUR/USD
**Timeframe:** 1H
**Asset class:** fx_spot
**Source type:** raw_text (operator-written)
**Date:** 2026-05-26
**Expected primitives:** `TimeOfDayCondition`, `Highest`, `ATR`, `TimeExit` (or `TimeOfDayCondition` as exit)

## Why this hunt

C.7 is the proof-of-life gate for Phase C — the milestone sub-phase
that proves the entire FX path (extraction → backtest → gauntlet →
seed) composes end-to-end on a real strategy. London-Open Breakout is
the canonical "first FX strategy" in academic FX literature: a clear
mechanism (overnight news flow gets digested at the largest FX
session-open of the day), a clean window (single bar at 08:00 UTC, ~1
trade/day), and a known-to-be-gauntlet-rejectable shape (the edge is
small and frequently fee/slippage-eaten on raw data).

The seed-or-no-seed decision is the test of the **pipeline**, not the
**strategy**. If the strategy passes the gauntlet's seed threshold and
seeds into paper, we've validated C.1.1 → C.6 compose correctly. If
the strategy fails the gauntlet, we've validated the gauntlet
correctly refuses cost-eaten FX strategies. Either outcome ships the
phase.

## Cost-sanity (pre-extraction back-of-envelope per Hunt 6B
retrospective rule)

- Implied trade frequency: 1 trade/day × 252 weekdays/year (FX 24/5,
  weekends closed) = ~252 trades/year
- Per-side cost (FX defaults from C.2):
  commission 0 bps + slippage 5 bps = 5 bps per side
- Round-trip cost: 5 bps × 2 = 10 bps
- Annual cost drag: 252 × 10 bps = 2520 bps = **25.2% / year**
- Source-claimed effect size (per academic literature on session-open
  breakouts in FX majors): ~0.10-0.20% per trade gross before costs,
  i.e. ~25-50% annual gross — same order of magnitude as the cost
  drag

**Verdict on extraction:** marginal-but-viable. Net edge after costs
will land roughly at zero on raw data — exactly the "gauntlet should
refuse this" zone. Extracting is **justified** because (a) the test of
the pipeline is the seed-or-no-seed decision, not the seed outcome;
(b) the strategy is the canonical FX shape from the design doc, not
something the operator would invent in isolation; (c) extraction cost
~$0.15 is within budget.

If the strategy approves seed despite the cost ceiling, that itself is
a finding to investigate (gauntlet calibration on FX vs crypto). If it
rejects, the pipeline has done its job.

## Source text

> The London FX session opens at 08:00 UTC and is the highest-liquidity
> event in the FX trading day. Before London opens, the Asian session
> (which began at 00:00 UTC) has been ranging in a relatively quiet
> window with limited news flow. A common institutional strategy in
> major FX pairs is to trade the London-open breakout: when the first
> London-session bar closes above the Asian session's high, go long;
> the breakout is interpreted as a signal that European institutional
> order flow is leaning toward EUR-buying / USD-selling, and the
> position is held into the New York session and closed before the NY
> session-close at 16:00 UTC.
>
> The mechanism is well-documented in retail FX literature (e.g. "FX
> Bootcamp" by Wayne McDonell, "Day Trading the Currency Markets" by
> Kathy Lien) and in academic studies of intraday FX seasonality (e.g.
> Andersen & Bollerslev's work on FX intraday volatility patterns,
> which show distinct volatility regimes at the session boundaries).
>
> The exact rules:
>
> **Instrument:** EUR/USD spot, the most liquid FX pair globally.
>
> **Timeframe:** 1-hour bars. The Asian session (00:00-07:00 UTC)
> occupies 8 bars; the London/NY overlap (08:00-15:00 UTC) occupies
> the next 8 bars and is where the strategy is exposed.
>
> **Session:** FX 24/5 — markets open Sunday 22:00 UTC and close
> Friday 22:00 UTC. Weekends are closed (no Saturday / Sunday-before-
> 22:00 bars per Oanda's market data feed).
>
> **Entry:** Long EUR/USD when the bar that opens at 08:00 UTC closes
> above the highest high of the preceding 8 bars (i.e., the Asian
> session range high, 00:00-07:00 UTC). Entry fires on the close of
> the 08:00 UTC bar (signal); fill on the next-bar open (09:00 UTC) at
> the realised slippage.
>
> **Exit:** Two paths, whichever fires first:
> 1. Close the position at the bar that opens at 16:00 UTC (the bar
>    BEFORE the NY-session close at 17:00 UTC ET = ~22:00 UTC GMT),
>    via a time-of-day exit. This caps the holding period at 8 bars
>    maximum.
> 2. Trailing ATR-multiple stop: 2 × ATR(14) below the highest close
>    since entry. This protects against intraday reversals.
>
> **Risk:** 1% of equity per trade (standard retail-FX position
> sizing).
>
> **Costs:** EUR/USD on Oanda demo: zero explicit commission, ~1 pip
> spread = 5 bps slippage per side (10 bps round-trip).
>
> **Expected edge per the literature:** the strategy's gross win rate
> is roughly 52-55%, with winners averaging ~0.30% and losers
> averaging ~0.20% (giving a small positive expectancy gross of
> ~0.05-0.10% per trade). Net of costs (~0.10% per round-trip) the
> edge is barely positive or zero on out-of-sample data, which is why
> it's a classic gauntlet-rejection candidate.
>
> **Long-only:** the symmetric short version (London-open breakdown
> below Asian low) is intentionally not extracted here. EUR/USD has a
> mild long-term upward drift in the 2025 sample, which would flatter
> the long-only version; the gauntlet's walk-forward + monte-carlo
> tests are exactly what would expose that.

## Expected schema shape

- `instrument.asset_class = "fx_spot"`
- `instrument.symbol = "EUR/USD"`
- `instrument.session_hours.weekend_closed = true`
- `entry.condition` = AND of: `TimeOfDayCondition(start=8, end=8, inclusive_end=true)` AND a breakout test like `Crossover(close, Highest(high, period=8))` or `Compare(close > Highest(high, period=8))`
- `exit.method` = some combination of TimeExit / TimeOfDayCondition exit + StopLossAtrMultiple
- `sizing.method = "fixed_percent"`, `risk_pct = 0.01`

## What "passes" looks like

- Extracted spec validates against StrategySpec v2 + has asset_class = fx_spot
- Backtest fires on EUR/USD 1H data (the perf-regression fixture covers 2025-01-01 → 2025-12-31)
- vbt + iterative both succeed; cross-engine trade-count parity within ±2× (per v1.2.A envelope)
- C.2 FX cost dispatch fires: slippage = 5 bps, commission = 0 (NOT crypto defaults)
- C.5 weekend-drop dispatch fires: 138 Sunday-evening rows dropped from the input
- Gauntlet completes all four legs (walk-forward + parameter-sweep + monte-carlo + DSR)
- DSR `prob_real_v2` produces a meaningful value (post-frequency-fix; would have been ≈0 on the pre-fix code)
- Composite score lands in `likely_robust` / `mixed_signals` / `likely_overfit`
- Seed-or-no-seed decision based on numeric verdict; either way, pipeline is proven

## What "stops" looks like (per brief's STOP CONDITIONS)

- Extraction misses `asset_class = fx_spot` or `session_hours` → extraction-prompt teaching gap
- Backtest crashes on FX data → C.5 or C.2 bug surfaced
- Drift parity blows out (trade counts differ by >2×) → engines disagree on FX semantics
- Gauntlet produces nonsense numbers → DSR fix not fully propagated
- Seeding triggers MT warmup state loss → C.6 restart-safety invariant broken

## Attribution

Operator-written source text composing the canonical London-Open Breakout
strategy shape from the FX-trading literature. No specific paste from a
copyrighted source; the mechanism is folkloric in the FX community and
described in dozens of public sources. The strategy shape mirrors the
design doc's §C.7 spec ("EUR/USD London-Open Breakout").

## Extraction history — convention-divergence finding (2026-05-26)

**First extraction attempt** (extraction_id
`64dac8ca-f0f2-42a7-8116-1bae07e8b5a6`, cost $0.166): the LLM
extracted every field correctly per the report's `extracted_rules`
(asset_class fx_spot, session_hours, TimeOfDayCondition entry gate,
trailing ATR stop, time-of-day exit, 1% risk sizing). Spec validation
then **rejected** with:

```
[weekday_day_invalid] filters[0].weekday: weekday.days entries must
be in [1,7] (ISO 8601), got 0
```

The source text included the redundant sentence "The strategy
generates no signals on weekends." The LLM mapped this to a
`filters[0].weekday` entry with `days = [0..4]` (pandas convention —
the same convention v1.2.D's `DayOfWeekCondition.days` uses). But the
older `filters[0].weekday.days` field expects ISO 8601 (Mon=1..Sun=7).
The two related primitives use **different conventions for the same
concept**, and the LLM understandably picked the pandas one.

**Finding for follow-up sub-phase:** a teaching-prompt addition is
warranted to disambiguate the two day-of-week numbering conventions:
`filters[0].weekday.days` is ISO 8601 (Mon=1..Sun=7) — distinct from
v1.2.D's `DayOfWeekCondition.days` which uses pandas (Mon=0..Sun=6).
Alternatively the schema can be normalised so both primitives use one
convention. Logged as a Phase C / v1.3 follow-up — not blocking for
C.7 because C.5/C.6 handle weekend session-skip structurally; the
in-spec weekday filter was redundant.

**Second extraction attempt** (after removing the redundant "no
signals on weekends" sentence from this source text): the LLM should
omit the unnecessary filter and produce a valid spec. C.7 continues
from there.
