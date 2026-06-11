# Slippage table — manual refresh procedure

The backtest engine resolves per-fill slippage through
`workers/src/marketmind_workers/backtest/slippage_model.py`. The
default backend is a **static, hand-maintained tier table** — there is
no live L2 order-book scrape. This file documents how to refresh it.

Sibling document to [`fees.md`](fees.md); same shape, same cadence.

## Why static

Per the Phase B design Q2 resolution (commit `0223b8e`):
spread-based static assumption, NOT live L2 data. Live L2 adds network
failure modes, a moving target for backtest reproducibility, and
exchange-specific WebSocket bookkeeping that is not worth the
operational tax at solo-dev scale. Backtests must be byte-stable
across re-runs; the slippage table is therefore code, not a live
fetch. Revisit if and only if Phase D introduces volatility-regime-
aware execution where the manual overhead becomes painful.

## Why a different default value from fees

FeeModel default: **10 bps** (Binance Spot BTC/USDT, taker / maker).
SlippageModel default: **5 bps** (Binance Spot BTC/USDT, taker / maker).

**Half** the fee default. This asymmetry is intentional — spreads on
BTC/USDT majors are tighter than round-trip commission. If you copy a
fee tier into the slippage table without thinking, the unit test
`test_default_model_returns_5_bps_for_btc_usdt_taker` will catch it.

## Cadence

**Quarterly.** Run alongside the fee table refresh — same operator
checklist:

- Q1 review: late January
- Q2 review: late April
- Q3 review: late July
- Q4 review: late October

If a venue experiences a sustained microstructure shift (sustained
spread blowout, depth collapse on a target symbol), refresh out of
cycle.

## What to refresh

Exactly one constant — `_DEFAULT_SLIPPAGE_TABLE` in
`workers/src/marketmind_workers/backtest/slippage_model.py`. Current
shape:

```python
_DEFAULT_SLIPPAGE_TABLE: SlippageTable = {
    "binance_spot": {
        "BTC/USDT": {
            "taker": [SlippageTier(volume_30d_usd_min=0.0, bps=5.0)],
            "maker": [SlippageTier(volume_30d_usd_min=0.0, bps=5.0)],
        },
    },
}
```

For each `(exchange, symbol, side)` entry, the `tiered list` is
ordered ascending by `volume_30d_usd_min`. The lookup picks the
**highest qualifying tier** for the caller's 30-day notional. If a
caller passes `volume_30d_usd=0.0` (the current default), it always
lands on the first tier.

## Where the numbers come from

Slippage is not published on a "schedule" the way fees are — it's an
empirical observation of (your fill price minus the prevailing mid)
averaged across many fills. Two reasonable sources:

- **Exchange-published "execution quality" reports** (some venues
  publish quarterly stats — Coinbase Prime, Binance VIP, etc.).
- **Your own trader's fill log.** Once Phase A's live trader has
  enough fills logged, compute the realised slippage as
  `(fill_price - candle_open) / candle_open * 10000`. This is the
  most honest source — the table values should converge to what the
  trader actually observes. The `trader_fills` table carries
  `slippage_bps_applied` per fill (migration `0008`) — the realised
  slippage is what you compare against.

For the initial 5 bps default, the source is the v1 hardcoded
assumption (preserved verbatim through B.2's bit-identity gate).

## The procedure

1. **Gather observed slippage.** For each `(exchange, symbol, side)`
   in the table, compute the average realised slippage over the
   last 90 days (or whatever observation window your traffic
   justifies). Look at the spread distribution — if you regularly
   take liquidity into wider spreads at volatile open / close
   periods, factor that in.

2. **Update the corresponding `SlippageTier(bps=...)`** in
   `_DEFAULT_SLIPPAGE_TABLE`. Conservative-pessimist bias is
   appropriate — backtests under-estimating slippage misleads more
   dangerously than over-estimating.

3. **(Optional) Add higher tiers.** Volume-tier slippage is the
   future-facing handle: a large-notional caller can pass
   `volume_30d_usd=...` and the lookup picks a tighter tier if you
   populate one (high-volume desks can post pegged orders, achieve
   tighter realised slippage). Append additional `SlippageTier`
   rows in ascending `volume_30d_usd_min` order. The lookup handles
   the rest. Do NOT remove the VIP-0 (`volume_30d_usd_min=0.0`)
   row; default backtests must continue to land on it.

4. **Verify the bit-identity test still holds.** From the repo root:

   ```bash
   uv run pytest workers/tests/test_slippage_model.py -q
   ```

   The first test —
   `test_default_model_returns_5_bps_for_btc_usdt_taker` — asserts
   the default Binance Spot BTC/USDT taker slippage is exactly
   5 bps. **If you change that value, the existing seeded
   strategies' backtest ledgers will shift.** That's a design
   decision, not a refresh; raise it explicitly before proceeding.

5. **Run the full backtest suite.** If the test in step 4 still
   asserts 5 bps, the rest of the suite should be untouched. If you
   changed any tier the seeded strategies actually consume,
   re-seed / re-run their gauntlets and re-record the dashboard
   numbers.

   ```bash
   uv run pytest workers/ -q
   ```

6. **Commit on main** with a message naming the source data and
   the review date, e.g.:

   ```text
   chore(slippage): Binance Spot slippage table refresh — Q2 2026

   Source: trader_fills observation 2026-02-01..2026-04-30 (8,142
   fills, avg realised slippage 4.7 bps taker / 3.2 bps maker).
   Conservative-pessimist round-up: bumped taker tier to 5.5 bps,
   maker unchanged at 5.0 bps. Re-recorded BB Breakout dashboard
   numbers (alpha down 0.4 pp); other strategies untouched.
   ```

7. **No service restart needed.** The slippage table lives in
   worker code; the worker container picks up the new table on its
   next deliberate rebuild. There's no separate slippage-config
   artifact to roll out.

## What about the trader?

The trader has its own authoritative per-version slippage via
`trader_strategy_versions.slippage_bps` — that is what the live
execution loop applies to fill prices. The SlippageModel backend
currently feeds **backtests and the buy-and-hold benchmark only**.
The two paths are intentionally separate; unifying them (SlippageModel
default as the floor, per-version override on top) is deferred to a
later phase. If a live execution slippage change matters to running
trader versions, update each version row manually — this slippage.md
doc does NOT cover the trader path.

## When to add a new exchange / symbol

Two cases:

- **Phase C ships multi-asset.** Add the new symbol row under
  `binance_spot` (or whichever exchange) with a starting slippage
  value (default: copy from a similar-liquidity symbol; conservative
  pessimist if no comparable). Add a unit test mirroring
  `test_default_model_returns_5_bps_for_btc_usdt_taker` for the new
  symbol.

- **Phase D / Phase C ships a new exchange.** Add a new top-level
  key (e.g. `"kraken_spot"`) with the relevant symbols. The
  `slippage_for_spec()` mapping in `slippage_model.py` translates
  `instrument.exchange` strings to slippage-table keys — add the
  mapping there too (the only one currently is `"binance"` →
  `"binance_spot"`).

  Unknown exchange / symbol combinations fall back to the
  conservative 5 bps default. That fallback exists so a
  new-exchange backtest doesn't accidentally run slippage-free; it
  is NOT a substitute for populating the real schedule.

## What about lower timeframes (Phase B.7+)?

The static table currently has no timeframe dimension — the same
slippage applies to 4H, 1H, and 15m backtests for the same
(exchange, symbol, side). The Phase B design Q6 resolution noted
that per-timeframe slippage variation is plausible (slippage on a
15m bar is a larger fraction of the bar's range than on a 4H bar)
but kept the v1 shape simple. If Phase B.7+ shows the 15m drift
analyzer flagging slippage-driven divergence, the SlippageTable
can grow a timeframe key — `exchange → symbol → timeframe → side →
tiers`. Until then, the simpler shape stays.
