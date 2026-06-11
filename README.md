# MarketMind — a backtesting engine built to tell you "no"

A deterministic crypto/FX strategy backtesting and validation engine with a
rigorous overfitting/cost gauntlet, plus an ML + market-microstructure
research stack (FTR) validated at realistic UK retail fees. It is
**infrastructure for testing your own strategies honestly** — not a trading
bot, not a signal service, and not a money-making product.

> ## ⚠️ Read this first
>
> - **This is not financial advice.** Nothing in this repository is a
>   recommendation to buy, sell, or hold anything.
> - **No claim of profitability is made.** This is a research/backtesting
>   engine. The complete research record shipped in this repo **rejected
>   every strategy it tested** — that is the headline result, documented in
>   full below and in `docs/`.
> - **Paper-only by default.** Live execution is *intentionally not wired*
>   in either trader. Going live would require deliberate code changes that
>   this project does not provide or document.
> - **No warranty. Use at your own risk.** See the Apache 2.0 license,
>   sections 7 (Disclaimer of Warranty) and 8 (Limitation of Liability).
> - **Backtested performance does not indicate future results.** Even the
>   honest numbers in this repo describe the past only.

## The core principle: the backtester must never lie

Most retail backtests flatter their authors. Every component here exists to
remove a specific way of fooling yourself:

| Discipline | What it guards against |
|---|---|
| **Walk-forward analysis** (rolling IS/OOS windows; purged + embargoed splits in FTR) | Curve-fitting to one period; train/test leakage through overlapping labels |
| **Parameter sweep + plateau scoring** | The "lone peak" — a parameter set that only works at exactly those values |
| **Monte Carlo** (return permutation; block bootstrap; matched-frequency random-entry nulls; label-permutation refits) | Mistaking luck or market beta for timing skill |
| **Deflated Sharpe Ratio** (Bailey & López de Prado 2014, with honestly-counted `n_trials` and T in *years*) | Multiple-testing bias — trying 144 configs and reporting the winner as if it were the only attempt |
| **Cost realism** (per-venue fee + half-spread + slippage profiles; ×1.5/×2 sensitivity) | Strategies that are only profitable at fee tiers you cannot actually access |
| **Anti-lookahead by construction** (one shared shifting module; truncation-invariance tests) | Features that secretly peek at the future |
| **Two engines + drift parity** (vectorized for sweeps, Decimal event-driven for final runs; identical fill law, CI-gated parity) | Silent simulator bugs that only show up one code path at a time |
| **Next-bar-open fills, fees on every side, no same-bar entries** | Fills at prices the simulated trader could never have seen |

A strategy verdict of REJECTED is a successful outcome of this system.

## The honest results (why you should trust this engine)

This repo ships its full research record — 23 strategy hunts plus the FTR
validation campaign — and the result across all of it is consistent:

- **Fast/directional crypto ideas died.** Trend breakouts, mean-reversion
  (RSI/Bollinger/Z-score), market-neutral perp pairs, and carry variants
  were **rejected** for no edge or for edge eaten by realistic costs. The
  hunt docs (`docs/hunts/`) record each one, including the reasoning and
  the numbers.
- **The standout FTR finding: real but unmonetizable predictability.** An
  hourly BTC ML model (XGBoost vs a logistic baseline, 94 purged
  walk-forward folds over ~8 years) genuinely predicted — it beat
  label-permutation nulls and 100% of matched-frequency random-entry
  simulations. The net economics still died at UK retail fees: gross +435%
  collapsed to net +5% on a Kraken-tier cost profile, with 90% of gross
  edge paid out as costs. Verdict: REJECTED on every venue profile.
  (`docs/ftr/REPORT.md` has the full matrix.)
- **A naive version of the same model without its cost-aware EV gate lost
  96–100%.** Cost-gating is the difference between a small honest "no" and
  ruin.
- **The only durable edge found was slow trend, and it was modest.** A
  confirmation-layered 4h trend portfolio beat buy-and-hold and all nulls
  on its holdout — and was *still* rejected because it could not clear the
  Deflated Sharpe bar given how many configurations were tried, and its
  returns were too concentrated. The system is rigorous enough to disprove
  its own best idea; that is the point.

If you want a tool that confirms your strategy works, this is the wrong
repo. If you want the tool that tells you *whether* it works, welcome.

## What's in the box

```
shared/   strategy-spec schema (pydantic, discriminated unions, validator)
workers/  backtest engines (vectorbt + iterative + perp pairs/trend/carry),
          overfitting gauntlet (walk-forward, sweep, Monte Carlo, DSR),
          fee/slippage models, paper trader, FTR research stack
api/      FastAPI read/control surface (incl. /ftr/* research endpoints)
web/      Next.js frontend (editorial-quant design system)
infra/    Dockerfiles, SQL migrations (applied automatically on boot)
docs/     design docs, hunt records, FTR validation report
tests/    cross-service integration tests + spec fixtures
```

The FTR stack (`workers/src/marketmind_workers/ftr/`) adds: venue cost
profiles, a QA-gated OHLCV data layer, an L1/L2 order-book recorder
(public endpoints, keyless), anti-lookahead feature pipelines, four
research strategies (ML hourly, 4h trend portfolio, OFI microstructure,
liquidity overlay), a G1–G9 verdict gauntlet, and a paper-only trader.

## Quickstart

```bash
# 1. install (Python 3.12, uv)
uv sync

# 2. environment
cp .env.example .env        # defaults are paper-only local dev

# 3. market data — fixtures are NOT shipped (exchange ToS); regenerate them
#    via keyless public endpoints:
uv run python -m marketmind_workers.ftr.data.fetch_all          # spot 1h/4h/1m
uv run python workers/scripts/fetch_perp_fixture.py             # USDM perp + funding

# 4. run the FTR validation gauntlet on real data
uv run python -m marketmind_workers.ftr.validation.runner --strategy trend
uv run python -m marketmind_workers.ftr.report verdicts

# 5. full stack (Postgres/Redis/API/worker/web)
docker compose up -d
# optional FTR services (paper trader + order-book recorder):
docker compose --profile ftr up -d

# 6. tests
uv run pytest -q            # market-data-fixture tests need step 3 first
```

## Testing your own strategy

1. **Spec-based (original engine):** write a `StrategySpec` JSON
   (see `docs/strategy-spec.md` and `tests/fixtures/strategies/` for valid
   and *invalid* examples), validate it through `validate_spec`, backtest
   it, then submit the backtest to the overfitting gauntlet. The composite
   score and per-signal breakdown tell you whether the edge is likely real.
2. **FTR-style (research stack):** add a frozen pydantic spec in
   `ftr/strategies/specs.py`, implement the strategy module against the
   `DecisionRecord` contract (every bar gets a decision, skips included),
   and wire it into `ftr/validation/runner.py`. The G1–G9 gates and the
   verdict vocabulary (`PASS`, `PASS_LOW_FREQUENCY`,
   `CONDITIONAL_PASS_INFEASIBLE_VENUE`, `REJECTED`, `INSUFFICIENT_DATA`)
   are the contract: count every sweep cell in `n_trials`, report every
   baseline, and let the verdict be what it is.

Conventions that keep the engine honest (please keep them):
type everything (pyright strict), costs are explicit config and never zero,
features never look forward, every claim ships with the test that checks it.

## Paper-only safety (both traders)

- The original trader's every job begins with `assert_paper_only()` —
  it reads `TRADER_ALLOW_LIVE` (default `false`) and crashes on any other
  value. There is no live order code behind the flag; the adapters are
  market-data-only by construction.
- The FTR trader's `ExecutionMode` enum has **one** member (`PAPER`); a
  module-level assert runs before any other import; instruments are
  `Literal["spot"]` with a derivative-symbol rejector (crypto derivatives
  are banned for UK retail — FCA, in force since Jan 2021); research-only
  specs are refused by the trader *by type*; deployment additionally
  requires a PASS verdict on an accessible venue, which — see above —
  nothing currently has.
- The test suite proves each of these (`workers/tests/test_ftr_paper_only.py`
  and friends). If you fork this to trade live, you are on your own, in
  every sense.

## License

Apache 2.0 — see [LICENSE](LICENSE). Note in particular §7 (no warranty)
and §8 (no liability). Market data fetched by the scripts comes from
exchange public APIs under *their* terms; do not redistribute downloaded
candles/order-book data with your fork.
