# MarketMind

**A deterministic backtesting + validation engine for crypto/FX strategies,
built around one principle: the backtester must never lie.**

Across seven research programs — trend, mean-reversion, market-neutral perp
pairs, perp trend, carry, and an ML + microstructure stack evaluated at
realistic UK retail fees — this engine rejected nearly everything it tested.
The few survivors earned only a paper-trading account, and the strictest
gauntlet later rejected those too. **The negative results are the point**:
this is infrastructure rigorous enough to disprove its own ideas, published
so you can test yours the same way.

> **Disclaimers, condensed:** not financial advice · no profitability claim
> (the shipped research record is a catalogue of rejections) · paper-only by
> default, live execution intentionally not wired · no warranty, use at your
> own risk (Apache 2.0 §7–8) · backtested performance does not indicate
> future results.

---

## What it is, and why it's different

Most public trading repos sell a strategy. This one sells **the machinery
for catching self-deception** — overfitting, lookahead bias, and fantasy
transaction costs — which is where almost all retail backtests quietly fail.

You bring a strategy idea; the engine answers one question honestly: *would
this have worked, net of real costs, beyond the period you tuned it on?*
A verdict of REJECTED is the system working as designed.

## The validation gauntlet

Every strategy faces the same battery. Each test removes a specific way of
fooling yourself:

| Test | The self-deception it catches |
|---|---|
| **Walk-forward analysis** — rolling train/test windows; the FTR stack adds purged splits with a 24h embargo | A strategy tuned to one period that collapses out-of-sample; label leakage across the train/test boundary |
| **Parameter sweep + plateau scoring** | The "lone peak": parameters that only work at exactly those values are curve-fit, not robust |
| **Monte Carlo nulls** — return permutation, 24h-block bootstrap, matched-frequency random entries, label-permutation model refits | Mistaking luck, or just market beta, for timing skill |
| **Deflated Sharpe Ratio** (Bailey & López de Prado 2014) with honestly counted `n_trials` and T in *years* | Multiple-testing bias — trying 144 configurations and reporting the winner as if it were the only attempt |
| **Cost realism** — per-venue fee + half-spread + slippage profiles, with ×1.5/×2 stress | Strategies that are only profitable at fee tiers the trader cannot actually access |
| **Frequency + cost/edge diagnostics** | "Profitable" systems that pay most of their gross edge to the exchange |

Mechanical honesty underneath: signal at bar close, fill at next bar open,
fees on every side, no same-bar fills, gaps reported but never filled,
features mathematically unable to see the future (one shared shifting
module + truncation-invariance tests).

## Architecture (for the engineers)

- **Dual backtest engines, cross-checked.** A vectorized engine for
  parameter sweeps and a Decimal-ledger event-driven engine for final runs
  share one fill law; CI enforces drift parity (identical trade timestamps,
  net return within tolerance). A silent simulator bug has to happen twice,
  identically, to survive.
- **Declarative strategy specs.** Strategies are frozen pydantic models
  (discriminated unions, strict validation) — data, not code. No LLM
  anywhere in the decision path; deterministic by design (fixed seeds,
  single-threaded XGBoost, golden-file byte-identity test).
- **ML done with discipline.** Purged walk-forward (94 folds over ~8 years),
  isotonic calibration fit on validation slices only, test slices touched
  once, per-fold model artifacts content-hashed into a registry, and a
  cost-aware expected-value gate that cannot be configured away.
- **Money math that audits.** `Decimal` ledger at the accounting boundary,
  lot/tick quantization, UTC-aware timestamps everywhere (naive datetimes
  rejected at module boundaries).
- **Hard paper-only walls.** The FTR trader's `ExecutionMode` enum has one
  member; a paper assert runs before any other import; instruments are
  `Literal["spot"]`; research-only specs are refused *by type*; no
  environment variable can introduce a live mode (a test scans the source
  to prove it). The original trader gates every job on
  `assert_paper_only()`.
- **Operationally real.** Two-container Docker (API + workers, plus opt-in
  research profile), idempotent SQL migrations, crash-safe state recovery,
  structured logging, ~450 fixture-independent tests passing, pyright
  strict across the codebase.

## The honest results

The full research record ships in this repo (`docs/hunts/`,
`docs/ftr/REPORT.md`). Headline numbers from the FTR campaign — stitched
out-of-sample, net of named venue costs:

| Strategy | Venue profile (round-trip cost) | Net | Gross | Sharpe | Verdict |
|---|---|---|---|---|---|
| ML hourly BTC (XGBoost, EV-gated) | Binance reference (26 bps) | +154% | +2005% | 0.54 | **REJECTED** |
| ML hourly BTC | Kraken UK tier (90 bps) | **+5%** | **+435%** | 0.14 | **REJECTED** |
| ML hourly BTC | Coinbase UK tier (130 bps) | +17% | +65% | 0.31 | **REJECTED** |
| 4h trend portfolio (8 coins, vol-targeted) | Kraken UK tier | +70% | +135% | 0.82 | **REJECTED** |
| Same model, *no* cost gate (baseline) | any | **−96% to −100%** | — | — | ruin |

Three findings worth a hiring reader's attention:

1. **Real but unmonetizable predictability.** The ML model genuinely
   predicted — it beat label-permutation nulls (OOS AUC 0.542 vs permuted
   95th percentile 0.522) and 100% of matched-frequency random-entry
   simulations. The net economics still died at retail fees: on Kraken-tier
   costs, 90% of gross edge went to the venue. Predictive signal and
   tradable edge are different things; this engine measures the difference.
2. **The cost gate is the strategy.** The identical model without its
   expected-value gate loses everything. Most retail backtests omit
   exactly this.
3. **Even the best idea was honestly rejected.** The trend portfolio beat
   buy-and-hold, beat every null, sat on a robust parameter plateau — and
   still failed the Deflated Sharpe bar (144 configurations tried, 2.4-year
   holdout) and return-consistency gates. The system can say no to its own
   best result. That is the feature.

## Quickstart

```bash
# install (Python 3.12 + uv)
uv sync
cp .env.example .env                  # defaults: paper-only local dev

# market data is NOT shipped (exchange ToS) — regenerate via keyless public APIs
uv run python -m marketmind_workers.ftr.data.fetch_all        # spot 1h/4h/1m, ~100MB
uv run python workers/scripts/fetch_perp_fixture.py           # USDM perp + funding

# run the validation gauntlet on real data, read the verdicts
uv run python -m marketmind_workers.ftr.validation.runner --strategy trend
uv run python -m marketmind_workers.ftr.report verdicts

# full stack (Postgres / Redis / API / workers / web)
docker compose up -d
docker compose --profile ftr up -d    # optional: paper trader + order-book recorder

# tests (~450 pass without market data; fixture tests skip until fetched)
uv run pytest -q
```

## Where to look

| You want to see… | Go to |
|---|---|
| The strategy spec schema | `shared/src/marketmind_shared/schemas/strategy_spec/` + `docs/strategy-spec.md` |
| The backtest engines | `workers/src/marketmind_workers/backtest/` (vectorbt, iterative, perp pairs/trend/carry) |
| The overfitting gauntlet | `workers/src/marketmind_workers/overfitting/` |
| The FTR research stack | `workers/src/marketmind_workers/ftr/` (data QA, features, strategies, G1–G9 gates, paper trader) |
| The anti-lookahead discipline | `ftr/features/shifting.py` + `workers/tests/test_ftr_no_lookahead_features.py` |
| The research record | `docs/hunts/` (20 hunt write-ups), `docs/ftr/REPORT.md` (full verdict matrix), `docs/project_log.md` |
| The paper-only proofs | `workers/tests/test_ftr_paper_only.py`, `workers/tests/test_ftr_guardrails_trip.py` |

## On live trading

Live execution is **intentionally not wired**. Both traders are paper-only
by multiple independent mechanisms (asserts, single-member mode enums,
keyless market-data clients, type-level refusals), each backed by a test.
Going live would require deliberate code changes this project does not
provide or document. If you make those changes, you assume all risk.

## License

Apache 2.0 — see [LICENSE](LICENSE), particularly §7 (no warranty) and §8
(no liability). Market data fetched by the scripts comes from exchange
public APIs under *their* terms; do not redistribute downloaded data.
