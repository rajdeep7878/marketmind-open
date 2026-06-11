# MarketMind strategy audit — 2026-05-19

_Read-only inventory of every extraction / backtest / overfitting row in the local Postgres, written by a one-shot audit script (`/tmp/strategy_audit.py`). For Claude-chat-assisted review before seeding the trader paper bot._

## Headline

- Total strategies extracted: **19**. Viable (have backtest + overfitting): **3**. Pass the gauntlet cleanly: **0**.
- Best candidate (if any): —
- Recommend Claude (chat) review: **—**

---

## Section 1 — Database inventory

- Total rows in `extracted_strategies`: **19**
  - With non-NULL `spec_json` (extraction succeeded): **6**
  - With NULL `spec_json` (refused/failed): **13**
- Total rows in `backtest_results`: **3**
- Total rows in `overfitting_analyses`: **3**
- Of extractions with `spec_json`: **3** have a backtest, **3** have an overfitting analysis.

## Section 2 — Per-extraction summary

| short_id | created | source | status | conf | summary | bt | of | bt_status |
|----------|---------|--------|--------|------|---------|----|----|-----------|
| `b9a3209b` | 2026-05-19 | https://www.youtube.com/watch?v=QH3WN2mnSX8 | REFUSED | 15% | This video is an educational explainer about the Stochastic RSI indicator — how it works,… | N | N | — |
| `64200a13` | 2026-05-19 | https://www.youtube.com/watch?v=NZysv-xr3Ec | PARTIALLY_EXTRACTED | 55% | A 1-minute EMA crossover pullback strategy using a 50/200 EMA cross, entering on a candle… | N | N | — |
| `9895fc2b` | 2026-05-19 | https://www.youtube.com/watch?v=f4fWvGfHxN8 | REFUSED | 0% | Extraction failed to produce a schema-valid StrategySpec. | N | N | — |
| `1288bb35` | 2026-05-19 | https://www.youtube.com/watch?v=icYe2SS3-4M | REFUSED | 42% | A trend-following Donchian Channel strategy on the 30-minute timeframe, filtered by a 200… | N | N | — |
| `58a53b71` | 2026-05-19 | https://www.youtube.com/watch?v=r7gzyEnPTwg | REFUSED | 45% | A well-defined Donchian Channel trend-following breakout system applied to Russell 1000 s… | N | N | — |
| `a99d46cb` | 2026-05-19 | https://www.youtube.com/watch?v=3aTsu23uSlQ | REFUSED | 15% | The strategy uses RSI divergence as its primary entry trigger, which requires visual/subj… | N | N | — |
| `52ead621` | 2026-05-19 | https://www.youtube.com/watch?v=YLQ88JCVYnk | REFUSED | 15% | This video describes a Bollinger Bands + RSI strategy for Bitcoin but introduces a critic… | N | N | — |
| `cdd97e6a` | 2026-05-19 | https://www.youtube.com/watch?v=yGcal8mePGQ | REFUSED | 38% | A Bollinger Bands squeeze-and-breakout strategy for BTC/USDT that enters on band widening… | N | N | — |
| `abf00254` | 2026-05-19 | https://www.youtube.com/watch?v=xaB1GS7X0xA | PARTIALLY_EXTRACTED | 45% | A long-only BTC/USDT 4H strategy combining a fast/slow Hull Moving Average crossover (16/… | Y | Y | succeeded |
| `7c99a8a0` | 2026-05-19 | https://www.youtube.com/watch?v=rf_EQvubKlk | REFUSED | 52% | A MACD crossover strategy filtered by a 200-period EMA trend filter, with a stop below th… | N | N | — |
| `cdb7c329` | 2026-05-19 | https://www.youtube.com/watch?v=c4OQTLhDQWk | REFUSED | 15% | This video is an educational explainer on RSI divergence concepts (regular and hidden, bu… | N | N | — |
| `643f2993` | 2026-05-19 | https://www.youtube.com/watch?v=NojfYk31_xI | REFUSED | 0% | Extraction failed to produce a schema-valid StrategySpec. | N | N | — |
| `b3e81a4f` | 2026-05-18 | https://www.youtube.com/watch?v=9JEmsSItdt4 | REFUSED | 15% | This video describes the 'LW Volatility Breakout Strategy' using Dungeon Channels, LWTI, … | N | N | — |
| `3bb1ff93` | 2026-05-18 | https://www.youtube.com/watch?v=pCmJ8wsAS_w | PARTIALLY_EXTRACTED | 48% | A mean reversion strategy using Bollinger Bands (30-period, 2 std dev) and RSI(13), enter… | N | N | — |
| `f6a1c948` | 2026-05-18 | https://www.youtube.com/watch?v=5s8R_BBwgEk | PARTIALLY_EXTRACTED | 82% | A classic Golden Cross strategy (50 SMA crosses above 200 SMA) on the 4-hour timeframe fo… | N | N | — |
| `1facd855` | 2026-05-16 | https://www.youtube.com/watch?v=pCmJ8wsAS_w | PARTIALLY_EXTRACTED | 45% | A mean reversion strategy using a 30-period Bollinger Band and 13-period RSI, entering lo… | Y | Y | succeeded |
| `ce2ed293` | 2026-05-16 | https://www.youtube.com/watch?v=9JEmsSItdt4 | REFUSED | 15% | This video describes the 'LW Volatility Breakout Strategy' using a proprietary TradingVie… | N | N | — |
| `8045af6b` | 2026-05-15 | https://www.youtube.com/watch?v=5s8R_BBwgEk | PARTIALLY_EXTRACTED | 82% | A classic Golden Cross strategy using a 50-period SMA crossing above/below a 200-period S… | Y | Y | succeeded |
| `ecfadae6` | 2026-05-15 | https://www.youtube.com/watch?v=m2I8Sv4-VcY | REFUSED | 10% | This is a heavily sales-focused video promoting a paid trading room. The actual strategy … | N | N | — |

## Section 3 — Detailed viable strategies

### Strategy: HMA Crossover + RSI + Linear Regression (4H BTC) (`abf00254`)

- **Source:** https://www.youtube.com/watch?v=xaB1GS7X0xA
- **Extraction date:** 2026-05-19
- **Confidence:** 45%
- **Verdict:** PARTIALLY_EXTRACTED

**What the strategy does (plain English):**

Trades BTC/USDT on Binance on the 4h timeframe, long. Enters when WMA(16) crosses above WMA(64) AND RSI(14) is above 52.0. Exits via WMA(16) crosses below WMA(64).

_Partial-extraction caveats (parts of the original strategy couldn't be mechanically encoded; the backtest uses a simplified version):_
- Hull Moving Average (HMA) — not in indicator whitelist; approximated with WMA which produces different values
- Linear Regression indicator — not in indicator whitelist; third entry condition omitted entirely
- Exit rule — not explicitly stated; inferred as reverse MA crossover

**Backtest results:**

- Total return: **+203.08%**
- Buy-and-hold (same period): +966.97%
- Alpha: **-763.89% (underperforms)**
- Win rate: 30.12%, profit factor: 1.18, Sharpe: 0.64, Sortino: 0.92
- Max drawdown: **63.93%** (1090 days)
- Total trades: 166 (expectancy 1.13%)
- Date range: `2020-01-01` → `2026-05-19` on `BTC/USDT` / `4h`
- Initial capital: $10,000.00; final: ≈ $30,307.69

**Overfitting analysis:**

- **Deflated Sharpe ratio:** -1.894 (observed Sharpe 0.637, P(real) 0.00%) — below 1.0 suggests the observed Sharpe could plausibly come from chance + selection bias.
- **Walk-forward OOS/IS ratio:** 6.578 (6 windows) — above 0.6 means OOS performance retains most of the IS edge.
- **Monte Carlo p-value:** 0.270 (100 permutations, percentile rank 73.00%) — at this level, the strategy's edge is plausibly explainable by price-order luck rather than a real edge.
- **Parameter sweep:** skipped (grid would exceed 50 cells; dropped: Indicator period (64))
- **Composite score:** 28.6/100 (verdict: `likely_robust`)

**Author claims vs measured:**

- **return** — author claimed `950%` (instrument: BTC/USDT, period: None); backtest measured **+203.08%**.
  > the total returns are 950%
- **return** — author claimed `1001%` (instrument: BTC/USDT, period: None); backtest measured **+203.08%**.
  > 1001% in returns
- **trade_count** — author claimed `131 trades` (instrument: BTC/USDT, period: None); backtest measured **166**.
  > the total number of trades 131 trades
- **other** — author claimed `Exposure time ~50-51%` (instrument: BTC/USDT, period: None); backtest measured **—**.
  > an exposure time of 51 versus 50%. So they are very close.
- **other** — author claimed `Annual volatility 61% (backtesting.py) or 41% (custom backtester)` (instrument: BTC/USDT, period: None); backtest measured **—**.
  > The annual volatility percentage is 61% as provided by backtesting.py. But it's 41% as per our custom backtester

**Five-point gauntlet:**

- Deflated Sharpe > 1.0: **FAIL** (-1.894)
- Walk-forward OOS/IS > 0.6: **PASS** (6.578)
- Beats buy-and-hold: **FAIL** (alpha -763.89%)
- Monte Carlo p < 0.05: **FAIL** (0.270)
- Parameter sweep robust: **BORDERLINE** (sweep skipped: grid would exceed 50 cells; dropped: Indicator period (64))

**Verdict for seeding:** Don't seed.

_3 fails — overfitting / no-edge profile._

---

### Strategy: Bollinger Bands + RSI Mean Reversion (`1facd855`)

- **Source:** https://www.youtube.com/watch?v=pCmJ8wsAS_w
- **Extraction date:** 2026-05-16
- **Confidence:** 45%
- **Verdict:** PARTIALLY_EXTRACTED

**What the strategy does (plain English):**

Trades BTC/USDT on binance on the 1d timeframe, long. Enters when close is below BOLLINGER(30,2.0σ) AND RSI(13) is below 25.0. Exits via close is at or above BOLLINGER(30,2.0σ).

_Partial-extraction caveats (parts of the original strategy couldn't be mechanically encoded; the backtest uses a simplified version):_
- Bollinger Band squeeze filter: 'skinny bands' has no numeric threshold — cannot be mechanically defined
- RSI divergence confirmation: requires multi-bar pivot detection logic not available in schema
- No stop loss defined — catastrophic in trending/falling-knife scenarios the author himself warns about
- Instrument never stated (examples on AAPL, a US stock, not a crypto asset)
- Timeframe never stated
- Short-side mirror rules (price > upper BB AND RSI > 75, exit at middle band) are described but only the long side is extracted in the spec

**Backtest results:**

- Total return: **+87.73%**
- Buy-and-hold (same period): +996.23%
- Alpha: **-908.50% (underperforms)**
- Win rate: 72.73%, profit factor: 3.40, Sharpe: 0.52, Sortino: 0.85
- Max drawdown: **21.15%** (439 days)
- Total trades: 11 (expectancy 6.60%)
- Date range: `2020-01-01` → `2026-05-16` on `BTC/USDT` / `1d`
- Initial capital: $10,000.00; final: ≈ $18,772.84

**Overfitting analysis:**

- **Deflated Sharpe ratio:** -2.014 (observed Sharpe 0.516, P(real) 0.00%) — below 1.0 suggests the observed Sharpe could plausibly come from chance + selection bias.
- **Walk-forward OOS/IS ratio:** 0.273 (6 windows) — below 0.6 means substantial degradation from IS to OOS.
- **Monte Carlo p-value:** 0.130 (100 permutations, percentile rank 87.00%) — at this level, the strategy's edge is plausibly explainable by price-order luck rather than a real edge.
- **Parameter sweep:** peakiness 0.000, baseline rank 80.00% (best in grid +134.66%, worst +16.05%, neighborhood avg +108.16%). Robust plateau.
- **Composite score:** 48.2/100 (verdict: `mixed_signals`)

**Author claims vs measured:**

- **win_rate** — author claimed `pretty high success rate` (instrument: None, period: None); backtest measured **72.73%**.
  > It's simple, easy to use, and has a pretty high success rate if you use it correctly.
- **other** — author claimed `The divergence filter gives an 'extremely great chance' of price reversal` (instrument: None, period: None); backtest measured **—**.
  > If this happens, there's an extremely great chance the price will reverse and make a new higher high.

**Five-point gauntlet:**

- Deflated Sharpe > 1.0: **FAIL** (-2.014)
- Walk-forward OOS/IS > 0.6: **FAIL** (0.273)
- Beats buy-and-hold: **FAIL** (alpha -908.50%)
- Monte Carlo p < 0.05: **FAIL** (0.130)
- Parameter sweep robust: **PASS** (peakiness 0.00, baseline rank 80th pct)

**Verdict for seeding:** Don't seed.

_4 fails — overfitting / no-edge profile._

---

### Strategy: Golden Cross 4H BTC/ETH/SOL/BNB (50/200 SMA) (`8045af6b`)

- **Source:** https://www.youtube.com/watch?v=5s8R_BBwgEk
- **Extraction date:** 2026-05-15
- **Confidence:** 82%
- **Verdict:** PARTIALLY_EXTRACTED

**What the strategy does (plain English):**

Trades BTC/USDT on Binance on the 4h timeframe, long. Enters when SMA(50) crosses above SMA(200). Exits via SMA(50) crosses below SMA(200); 8.0% stop.

_Partial-extraction caveats (parts of the original strategy couldn't be mechanically encoded; the backtest uses a simplified version):_
- Portfolio-level cap of 8 simultaneous open trades across 4 instruments — requires multi-asset portfolio engine not representable in a single-instrument spec

**Backtest results:**

- Total return: **+684.62%**
- Buy-and-hold (same period): +672.59%
- Alpha: **+12.03% (beats benchmark)**
- Win rate: 37.84%, profit factor: 1.69, Sharpe: 1.08, Sortino: 1.57
- Max drawdown: **51.73%** (999 days)
- Total trades: 37 (expectancy 9.43%)
- Date range: `2020-05-01` → `2026-03-31` on `BTC/USDT` / `4h`
- Initial capital: $10,000.00; final: ≈ $78,461.72

**Overfitting analysis:**

- **Deflated Sharpe ratio:** -1.453 (observed Sharpe 1.078, P(real) 0.00%) — below 1.0 suggests the observed Sharpe could plausibly come from chance + selection bias.
- **Walk-forward OOS/IS ratio:** 0.093 (6 windows) — below 0.6 means substantial degradation from IS to OOS.
- **Monte Carlo p-value:** 0.060 (100 permutations, percentile rank 94.00%) — at this level, the strategy's edge is plausibly explainable by price-order luck rather than a real edge.
- **Parameter sweep:** skipped (grid would exceed 50 cells; dropped: Indicator period (200))
- **Composite score:** 47.2/100 (verdict: `mixed_signals`)

**Author claims vs measured:**

- **return** — author claimed `6,200%+ total profit` (instrument: BTC/ETH/SOL/BNB, period: May 2020 – March 2026); backtest measured **+684.62%**.
  > The 4 hour chart. This setup achieved over 6,200% total profit with only 287 trades and just 14% drawdown.
- **drawdown** — author claimed `14% max drawdown` (instrument: BTC/ETH/SOL/BNB, period: May 2020 – March 2026); backtest measured **51.73%**.
  > The 4 hour chart. This setup achieved over 6,200% total profit with only 287 trades and just 14% drawdown.
- **trade_count** — author claimed `287 trades` (instrument: BTC/ETH/SOL/BNB, period: May 2020 – March 2026); backtest measured **37**.
  > only 287 trades
- **return** — author claimed `2,800%+ total profit on 1h chart` (instrument: BTC/ETH/SOL/BNB, period: May 2020 – March 2026); backtest measured **+684.62%**.
  > It generated over 2,800% total profit
- **return** — author claimed `~2,400% total profit on 2h chart` (instrument: BTC/ETH/SOL/BNB, period: May 2020 – March 2026); backtest measured **+684.62%**.
  > Profit was still strong, around 2,400%.
- **other** — author claimed `Buy and hold returned ~1,643% over the same period` (instrument: BTC/ETH/SOL/BNB, period: May 2020 – March 2026); backtest measured **—**.
  > Buy and hold during the same period returned about 1,643%.

**Five-point gauntlet:**

- Deflated Sharpe > 1.0: **FAIL** (-1.453)
- Walk-forward OOS/IS > 0.6: **FAIL** (0.093)
- Beats buy-and-hold: **PASS** (alpha +12.03%)
- Monte Carlo p < 0.05: **FAIL** (0.060)
- Parameter sweep robust: **BORDERLINE** (sweep skipped: grid would exceed 50 cells; dropped: Indicator period (200))

**Verdict for seeding:** Don't seed.

_3 fails — overfitting / no-edge profile._

---

## Section 4 — Refused / failed extractions log

_Context only — not analysed. Inventory of attempts that didn't yield a usable spec._

| short_id | created | source | reason |
|----------|---------|--------|--------|
| `b9a3209b` | 2026-05-19 | https://www.youtube.com/watch?v=QH3WN2mnSX8 | No backtestable strategy can be extracted. The video is purely educational content explaining how the Stochastic RSI in… |
| `9895fc2b` | 2026-05-19 | https://www.youtube.com/watch?v=f4fWvGfHxN8 | Extraction downgraded due to schema validation failure: ExtractionReport validation failed: 1 validation error for Extr… |
| `1288bb35` | 2026-05-19 | https://www.youtube.com/watch?v=icYe2SS3-4M | Downgraded after spec validation failed. Original reasoning: The strategy has several clearly stated rules: 200 SMA tre… |
| `58a53b71` | 2026-05-19 | https://www.youtube.com/watch?v=r7gzyEnPTwg | Downgraded after spec validation failed. Original reasoning: Every rule in this strategy is explicitly stated and mecha… |
| `a99d46cb` | 2026-05-19 | https://www.youtube.com/watch?v=3aTsu23uSlQ | Downgraded after spec validation failed. Original reasoning: The critical entry condition — RSI divergence — requires a… |
| `52ead621` | 2026-05-19 | https://www.youtube.com/watch?v=YLQ88JCVYnk | The strategy cannot be mechanically extracted for two reasons: (1) The primary entry signal relies on RSI-price diverge… |
| `cdd97e6a` | 2026-05-19 | https://www.youtube.com/watch?v=yGcal8mePGQ | Downgraded after spec validation failed. Original reasoning: The strategy has clear mechanical intent and some quantifi… |
| `7c99a8a0` | 2026-05-19 | https://www.youtube.com/watch?v=rf_EQvubKlk | Downgraded after spec validation failed. Original reasoning: The core mechanical rules are clear: MACD line crosses abo… |
| `cdb7c329` | 2026-05-19 | https://www.youtube.com/watch?v=c4OQTLhDQWk | The strategy cannot be extracted because: (1) No specific instrument or timeframe is given. (2) Entry logic relies on v… |
| `643f2993` | 2026-05-19 | https://www.youtube.com/watch?v=NojfYk31_xI | Extraction downgraded due to schema validation failure: tool_use payload missing or malformed `report` field |
| `b3e81a4f` | 2026-05-18 | https://www.youtube.com/watch?v=9JEmsSItdt4 | The strategy relies on two non-whitelisted proprietary indicators (Dungeon Channels and LWTI/Larry Williams Large Trade… |
| `ce2ed293` | 2026-05-16 | https://www.youtube.com/watch?v=9JEmsSItdt4 | This strategy cannot be mechanically extracted for two reasons. First, it depends on the 'Dungeon Channels' indicator, … |
| `ecfadae6` | 2026-05-15 | https://www.youtube.com/watch?v=m2I8Sv4-VcY | The strategy's entry logic depends on manually drawn support and resistance zones identified by the trader at 6:20 AM e… |

## Section 5 — Database hygiene notes

- No orphaned backtests.
- No strategies have duplicate backtests.
**Strategies tested at timeframe/asset incompatible with paper-bot config** (paper bot is configured for `BTC/USDT, ETH/USDT` on `4h`): 3
- `64200a13` (1-Minute EMA Crossover Pullback Strategy) → `EUR/USDT` on `1m`
- `3bb1ff93` (Bollinger Bands + RSI Mean Reversion) → `BTC/USDT` on `1d`
- `1facd855` (Bollinger Bands + RSI Mean Reversion) → `BTC/USDT` on `1d`


---

_Generated by `/tmp/strategy_audit.py`. Read-only; the script is not committed to the repo._