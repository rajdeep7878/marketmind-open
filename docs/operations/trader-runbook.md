# Trader v1 — operator runbook

The trader v1 is **paper-only**. There is no live-trading code path
in this build. `TRADER_ALLOW_LIVE` must stay `false`; any other
value crashes the runner at boot (`assert_paper_only()`).

This doc covers the seed → approve → run flow for adding a new
strategy version to the bot, and what to watch in the first 48
hours.

---

## 1. Prerequisites in MarketMind

Before seeding, the strategy must have completed the full
MarketMind research pipeline:

1. **Extraction** — `extracted_strategies.spec_json` is NOT NULL.
   A refusal verdict (spec_json IS NULL) cannot be seeded.
2. **Backtest** — at least one `backtest_results` row exists for
   the extraction. The seed script picks the most recent one.
3. **Overfitting analysis** — `overfitting_analyses` has been run
   against that backtest, in particular with a non-empty
   walk-forward result. Without walk-forward data, the trader's
   drift analyzer can't compare paper performance against the
   approving backtest, and the admin `approve_paper` endpoint
   would reject the version anyway. The seed script enforces
   this fail-fast.

Look it up in the MarketMind UI or via `SELECT … FROM
extracted_strategies WHERE id = …` before proceeding.

---

## 2. Seed the version

```bash
DATABASE_URL=postgresql://USER:PW@HOST:PORT/DB \
uv run python scripts/trader_seed_strategy.py \
    --extraction-id 00000000-0000-0000-0000-000000000000 \
    --name "MA Trend BTC 4h" \
    --template ma_trend \
    --parameters-json '{"fast_ema_period": 12, "slow_ema_period": 26, "trend_ema_period": 200, "atr_period": 14, "atr_mult": "2.0"}' \
    --symbols BTC/USDT \
    --timeframes 4h \
    --risk-pct 0.005
```

Always run with `--dry-run` first to inspect the JSONB payloads
that would land in `trader_strategy_versions.backtest_metrics` +
`overfitting_metrics`. The script prints the exact dict; spot-check
that walk-forward trade frequency and single-pass win rate look
sensible.

What gets written:

- `trader_strategies` — one row per `--name`. Re-running with the
  same name re-uses it and bumps the version number.
- `trader_strategy_versions` — one row per call. Key fields:
  - `approved_for_paper = FALSE` (the load-bearing safety default).
  - `approved_for_live = FALSE` (locked by the immutability trigger
    in migration 0006).
  - `enabled = TRUE` (signal engine sees the version, but risk
    blocks any signal it produces until approved).
  - `backtest_metrics` JSONB — the two-subtree shape the drift
    analyzer + admin validator both read.
  - `overfitting_metrics` JSONB — operator-facing summary of the
    walk-forward / parameter-sweep / monte-carlo / deflated-sharpe
    / composite-score analyses.

The script prints `strategy_id`, `strategy_version_id`, `version`,
plus the next-step reminder.

---

## 3. Review + approve

Before flipping `approved_for_paper`:

```bash
# Inspect the version (admin auth required)
curl -u admin:PW https://API/trader/strategies | jq '.items[] | select(.id=="VERSION_ID")'
```

Sanity-check:

- `walk_forward.out_of_sample_trade_freq_per_week` is in a band
  the strategy actually traded at (matches what you saw in
  MarketMind's overfitting view).
- `single_pass.win_rate`, `avg_return_per_trade`, `max_drawdown_pct`
  match the backtest_results page.
- `latest_drift_health` is `null` (no drift rows yet — drift
  runs daily; the first row appears on the next 01:00 UTC tick
  after the version starts trading).

When satisfied, approve:

```bash
curl -u admin:PW -X POST \
    https://API/trader/strategies/VERSION_ID/approve_paper
```

The endpoint re-runs the backtest_metrics shape validator and
returns 200 (with the new flags) or 422 (with a list of missing
keys). On 422, the seed payload was incomplete — re-seed after
fixing the upstream MarketMind data.

`enable` / `disable` toggles are separate endpoints (same auth):

```bash
curl -u admin:PW -X POST https://API/trader/strategies/VERSION_ID/disable
curl -u admin:PW -X POST https://API/trader/strategies/VERSION_ID/enable
```

`disable` does NOT close existing OPEN positions — they continue
to honour their stops + any EXIT signals. The signal engine just
stops evaluating the version on the next cycle.

---

## 4. First-48h watch

The trader runs on a 1-minute main cycle. Check these on day 0:

- **GET /trader/risk/status** — `kill_switch_tripped` should be
  `false`; `recent_risk_events` should NOT contain `kill_switch`,
  `daily_loss_breach`, or `weekly_loss_breach` entries.
- **GET /trader/portfolio/current** — `last_snapshot_ts` should be
  within the last 60–90 seconds. If older, the runner has either
  stopped or a phase is hung; check `trader_bot_runs`.
- **GET /trader/alerts/recent** — look for any `severity=critical`
  rows. The phase-failure detector emits one if any phase fails
  3 cycles in a row; the stale-heartbeat detector emits one if
  the runner row's `last_heartbeat_at` exceeds 5 minutes.

Day 1 (first 01:00 UTC after seeding):

- **GET /trader/drift/latest** should show a row for your version.
  `health_status` is `HEALTHY` / `WATCH` / `BREACH`. v1's drift
  is advisory only — a `BREACH` does NOT auto-disable the
  version, just emits a warning alert.

Day 1+ ongoing:

- Telegram should be receiving the daily summary at 00:05 UTC
  and (eventually) the weekly summary at Monday 00:10 UTC.
- If you see drift `BREACH` repeatedly, either re-evaluate the
  strategy or `disable` the version manually.

---

## 5. What v1 deliberately does NOT do

- **Live trading.** No code path exists. `TRADER_ALLOW_LIVE` is a
  string env var the assertion compares verbatim against
  `"false"`; setting it to anything else makes the runner crash.
- **Auto-approval.** Every paper-trading version requires a manual
  POST. The seed script cannot bypass this.
- **Auto-disable on drift breach.** v1's drift is advisory. If you
  want the bot to stop trading a drifted version, hit
  `/disable` yourself.
- **Per-strategy drawdown.** The portfolio drawdown applies bot-
  wide. v1.1 will reconstruct per-strategy drawdown curves.
- **Partial fills, leverage, shorts, scalping.** Long-only spot,
  full size or none, every cycle.

---

## 6. Common operator tasks

| Task | Command |
|------|---------|
| Disable a misbehaving version | `POST /trader/strategies/{id}/disable` |
| Force kill-switch (close everything) | Set `equity` low enough in DB? — actually, just `disable` every version + wait for positions to hit stops. v1 has no "panic close" command. |
| Inspect current open positions | `GET /trader/positions/open` |
| Inspect recent fills | `GET /trader/fills/recent` |
| Inspect equity curve | `GET /trader/portfolio/equity_curve?since=2026-05-18T00:00:00Z` |
| Inspect audit log | `GET /trader/audit/recent` |

All admin POST endpoints require HTTP Basic auth with the
`ADMIN_USERNAME` / `ADMIN_PASSWORD` env vars set on the API.
