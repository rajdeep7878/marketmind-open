# Fee table — manual refresh procedure

The backtest engine resolves commission per fill through
`workers/src/marketmind_workers/backtest/fee_model.py`. The default
backend is a **static, hand-maintained tier table** — there is no live
API scrape. This file documents how to refresh it.

## Why static

Per the Phase B design Q1 resolution (commit `0223b8e`): exchange fee
schedules change rarely (quarters, not days), and a live API path adds
network failure modes, rate-limit interactions, and a moving target for
backtest reproducibility. Backtests must be byte-stable across re-runs;
the fee table is therefore code, not a live fetch. Revisit if and only
if Phase D introduces multi-exchange portfolios where the manual
overhead becomes painful.

## Cadence

**Quarterly.** Add it to the operator checklist alongside the dependency
refresh:

- Q1 review: late January
- Q2 review: late April
- Q3 review: late July
- Q4 review: late October

If an exchange announces a mid-quarter fee change that materially
affects an in-flight strategy, refresh out of cycle.

## What to refresh

Exactly one constant — `_DEFAULT_FEE_TABLE` in
`workers/src/marketmind_workers/backtest/fee_model.py`. Current shape:

```python
_DEFAULT_FEE_TABLE: FeeTable = {
    "binance_spot": {
        "BTC/USDT": {
            "taker": [FeeTier(volume_30d_usd_min=0.0, bps=10.0)],
            "maker": [FeeTier(volume_30d_usd_min=0.0, bps=10.0)],
        },
    },
}
```

For each `(exchange, symbol, side)` entry, the `tiered list` is
ordered ascending by `volume_30d_usd_min`. The lookup picks the
**highest qualifying tier** for the caller's 30-day notional. If a
caller passes `volume_30d_usd=0.0` (the current default), it always
lands on the first tier.

## The procedure

1. **Pull the published schedule.** For each exchange currently in
   the table, open the official fee schedule page:
   - Binance Spot — <https://www.binance.com/en/fee/schedule>
   - (Add new exchanges as Phase C/D introduces them.)

2. **Verify the "VIP 0" / "Regular User" row.** That is the tier the
   default table represents — it's the conservative assumption for a
   small-volume backtest. If the published `taker` / `maker` bps for
   that tier has moved, update the corresponding `FeeTier(bps=...)`.

3. **(Optional) Add higher tiers.** If you want to support a backtest
   at a higher tier — pass `volume_30d_usd=...` — append additional
   `FeeTier` rows in ascending `volume_30d_usd_min` order. The lookup
   handles the rest. Do NOT replace the VIP 0 row; default backtests
   must continue to land on it.

4. **Verify the bit-identity test still holds.** From the repo root:

   ```bash
   uv run pytest workers/tests/test_fee_model.py -q
   ```

   The first test —
   `test_default_model_returns_10_bps_for_btc_usdt_taker` — asserts
   the default Binance Spot BTC/USDT taker fee is exactly 10 bps.
   **If you change that value, the existing seeded strategies' backtest
   ledgers will shift.** That's a design decision, not a refresh; raise
   it explicitly before proceeding.

5. **Run the full backtest suite.** If the test in step 4 still asserts
   10 bps, the rest of the suite should be untouched. If you changed
   any tier the seeded strategies actually consume, re-seed / re-run
   their gauntlets and re-record the dashboard numbers.

   ```bash
   uv run pytest workers/ -q
   ```

6. **Commit on main** with a message naming the source schedule and the
   review date, e.g.:

   ```text
   chore(fees): Binance Spot fee table refresh — Q2 2026

   Source: binance.com/en/fee/schedule (snapshot 2026-04-30).
   No change to VIP 0 taker/maker (still 10 bps). Added VIP 1–3
   tiers for future high-volume backtests.
   ```

7. **No service restart needed.** The fee table lives in worker code;
   the worker container picks up the new table on its next deliberate
   rebuild. There's no separate fee-config artifact to roll out.

## What about the trader?

The trader has its own authoritative per-version commission via
`trader_strategy_versions.fee_bps` — that is what the live execution
loop charges. The FeeModel backend currently feeds **backtests and the
buy-and-hold benchmark only**. The two paths are intentionally
separate; unifying them (FeeModel default as the floor, per-version
override on top) is deferred to a later phase. If a live exchange fee
change matters to running trader versions, update each version row
manually — this fees.md doc does NOT cover the trader path.

## When to add a new exchange / symbol

Two cases:

- **Phase C ships multi-asset.** Add the new symbol row under
  `binance_spot` (or whichever exchange) with VIP 0 fees and a unit
  test mirroring `test_default_model_returns_10_bps_for_btc_usdt_taker`
  for the new symbol.

- **Phase D / Phase C ships a new exchange.** Add a new top-level key
  (e.g. `"kraken_spot"`) with the relevant symbols. The
  `commission_for_spec()` mapping in `fee_model.py` translates
  `instrument.exchange` strings to fee-table keys — add the mapping
  there too (the only one currently is `"binance"` → `"binance_spot"`).

  Unknown exchange / symbol combinations fall back to the conservative
  10 bps default. That fallback exists so a new-exchange backtest
  doesn't accidentally run cost-free; it is NOT a substitute for
  populating the real schedule.
