# Parallel Hunt Cluster — Mean-Reversion at Fast Cadence (2026-06-04)

First **parallel** hunt cluster: two fully artifact-isolated mean-reversion hunts
run concurrently against committed ref `87b930e` (the 4-primitive commit:
RSI / BollingerBands / ZScore conditions + RMultipleExit). Both **REJECT**. The
two results, read together, answer a structural question the trend-following era
left open.

## Migrations
Applied **0015–0018** to the shared `marketmind` DB via the canonical worker
code path (`apply_migrations`, pointed at the host migration dir). All four are
**no-op `SELECT 1` markers** — zero DDL, zero trader-table mutation. Verified
before/after: `trader_strategy_versions`=7, `trader_strategy_state`=6,
`trader_candles`=3,644 — **unchanged**. `_schema_migrations` gained exactly 4
rows. trader_worker (Up 8 days) was never restarted or touched. STOP-on-trader-side
condition: not triggered.

## Per-hunt results

| | Hunt 23 (lane A) | Hunt 24 (lane B) |
|---|---|---|
| shape | 15m ZScore(20)<−2σ + 4H EMA50 + ATR vol-band | 1H RSI(14)<30 + 4H EMA50 |
| exit | RMultipleExit 1:2 | RMultipleExit 1:2 |
| trades / yr | 1,052 (**268/yr**) | 112 (**28.6/yr**) |
| total return | −2.58% | −0.38% |
| Sharpe | **−5.28** | −1.07 |
| profit factor | 0.46 | 0.63 |
| cost drag (per-notional) | **~80%/yr** | ~8.6%/yr |
| cost-sanity ratio | NEGATIVE → FAIL | NEGATIVE → FAIL |
| **MC p-value** | **0.46 (random)** | **0.02 (faint signal)** |
| DSR prob_real_v2 | 2.3e-04 | 3.3e-07 |
| WF OOS-positive | 0/6 | 0/6 |
| sweep peakiness | 0.0 | 0.0 |
| **composite / verdict** | **57.81 / mixed_signals** | **43.25 / mixed_signals** |
| drift parity (vbt:itr) | 1.58× ✓ | 1.35× ✓ |
| **decision** | **REJECT** | **REJECT** |

Full diagnostics in `hunt_23_*.md` / `hunt_24_*.md`; raw spec + gauntlet JSON in
`docs/hunts/data/`.

## Project finding — does mean-reversion have edge at fast cadence?

**No — and the *reason* differs by cadence, which is the valuable part.**

- **15m (Hunt 23): no edge AND cost-eaten.** Monte Carlo p=0.46 — the ZScore
  oversold entry, even trend- and volatility-gated, is **statistically
  indistinguishable from random**. There is no signal to eat; the 80%/yr cost
  drag is just the final insult. Sharpe −5.28 is the worst of the hunt era.
- **1H (Hunt 24): faint edge, fully cost-eaten.** Monte Carlo p=0.02 — RSI
  oversold-in-uptrend *does* pick slightly-better-than-random entries. But a 33%
  win rate at 1:2 R:R is right at breakeven (0.33×2 ≈ 0.66 vs 0.67 loss-side),
  and the 8.6%/yr drag tips it negative.

**Dominant driver:** Monte-Carlo / no-edge at 15m; cost-eating at 1H. DSR binds
in both (negative Sharpe ⇒ prob_real ≈ 0) but it's a *consequence* of the above,
not the root. The parameter sweep clears both (peakiness 0) — **neither is an
overfit**; they are genuinely, structurally unprofitable.

**Versus the trend-following cadence boundary (4H strong / 1H edge / 15m
dead-for-trend):** mean-reversion behaves **differently and worse**. Trend at
least *strengthens* as you go up in cadence to 4H. Mean-reversion here is dead at
15m (literally random) and merely-faint-then-cost-killed at 1H — we never reached
a cadence where it turned positive. The provisional **mean-reversion boundary is
tighter than trend's**: there is no fast-cadence regime where trend-gated MR
survives 30 bps on BTC. The natural next test is whether MR has a home at 4H
(where costs drop to ~9–18 trades/yr) — i.e. MR may need the *same* slow cadence
trend does, contradicting the folk intuition that MR is a "fast" style.

**Caveat — a latent engine bug surfaced (documented, not fixed):** these are the
first hunts to use a genuine *cross-timeframe* filter (4H-on-15m / 4H-on-1h).
The engine's `_align_to_primary` (translator.py) requires an **unnamed**
DatetimeIndex — it does `rename(columns={"index": "filter_close"})`, which
silently no-ops on `get_market_data`'s `'timestamp'`-named index, breaking
`merge_asof`. Prior hunts (17–22) used same-timeframe filters only, so this was
never hit. Worked around with a **process-local, compute-neutral shim** (strip the
index *label* only; values byte-identical) applied identically to backtest +
gauntlet — **no engine file edited**. **Recommend a one-line engine fix**
(`right_on` should derive from the index name like `left_on` does) before any
cross-TF strategy is seeded for real.

## Parallel-hunt meta

- **Artifact isolation held — zero merge conflicts.** Each lane wrote only its own
  `docs/hunts/hunt_NN_*.md` + `docs/hunts/data/*` + its own tmp JSON. No shared
  schema/dispatcher/engine source was edited (the cross-TF bug was shimmed
  in-process, not patched in-tree). The two lanes never touched a common file.
- **Committed-ref gauntlet fixed the blind-verifier problem.** The gauntlet ran
  **in-process on the exact validated `StrategySpec` object** the backtest used
  (the artifact judges itself), with the source tree pinned at `87b930e` (verified
  `git rev-parse` clean before launch). No blind `git checkout` — the verifier saw
  precisely what it judged.
- **Cost / model split — the $42 problem did NOT recur.** Total LLM spend
  ≈ **$0.29** (Hunt 23 extraction $0.185 fail + $0.058 capture-retry = $0.24;
  Hunt 24 extraction $0.044). Structure:
  - **Extraction** → Sonnet 4.6 (service-hardcoded), the only paid LLM calls.
  - **Backtest + drift-parity + full gauntlet** → **pure compute, zero LLM** (run
    as two concurrent background Python processes, not subagents). This is *cheaper
    than Haiku* — the mechanical steps cost nothing.
  - **Verdict reasoning + artifacts** → Opus main loop.
  - **Subagent count: 1** (one Explore agent to map the pipeline entrypoints).
    No Opus subagent fleet → none of last run's $42 burn.

## Bot regression — clean
- **Strategies: 7 → 7** (no seed; both hunts REJECT — and RMultipleExit is
  BACKTEST-ONLY by design, so even a passing MR hunt could not be promoted to the
  live SpecTemplate trader without first decomposing the wrapper into an explicit
  protective stop — a known structural gate, see exit.py).
- **Modern Turtle (4H spec):** still enabled + live-evaluating; untouched.
- **trader_worker:** Up 8 days, uninterrupted (migration applied via worker
  container, never via trader_worker).
- **Ingestion fresh:** BTC/USDT + ETH/USDT candles current to 12:45Z (15m).
- **Queues:** redis `default` depth 0. **Alerts:** 2 routine in 24h (last 10:12Z),
  none critical.

## Recommendation
**Scale to a full parallel hunt cluster — with two adjustments.**
1. The parallel harness is proven: artifact isolation + in-process committed-ref
   gauntlet + compute-not-subagents gives correct, cheap, conflict-free fan-out.
   Lift lane count to 4–6 next run; the pattern holds.
2. **Pivot the mean-reversion question to 4H** before spending more on fast-cadence
   MR — the data says fast MR has no survivable regime on BTC at 30 bps. Test
   trend-gated MR (ZScore / RSI / Bollinger below_lower) at **4H**, where trade
   frequency drops 10–30× and the cost drag falls to a survivable band. That is
   the open question this cluster reframes.
3. Land the one-line `_align_to_primary` cross-TF fix in a normal (non-hunt)
   commit so future cross-timeframe hunts need no shim, and add a percentile+ATR
   worked example to the extraction prompt (the `atr_period`→`period` convention
   bleed cost one retry).
