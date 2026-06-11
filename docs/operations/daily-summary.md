# Daily summary report

A structured snapshot of the paper-trading bot — the trailing 24h plus
current state — written once a day and readable from the CLI.

## What it is

Every day at **00:05 UTC** the trader emits a daily summary: a JSON file
(the source of truth) and a rendered text file. It covers bot health,
equity, per-strategy state, and trailing-24h event counts. It is operator
observability — separate from the trading logic, and read-only against
every table it touches.

## Where it runs

The `tick_summary_daily` job in `workers/src/marketmind_workers/trader/jobs.py`,
scheduled by the trader's in-process scheduler in the `trader_worker`
container. It augments the long-standing one-line activity-feed alert (a
`trader_alerts` row, `subject='Daily summary'`) — that alert is unchanged;
the structured report is additional.

Report generation is **best-effort**: if it fails (a DB hiccup, a disk
issue) the failure is logged (`daily_summary_report_failed`) and the tick
continues — the activity-feed alert and the re-enqueue are never blocked.

## Storage

Files land in `/data/daily-summaries/` — a volume-mounted directory
(`./data:/data` on `trader_worker`), so they survive container restarts:

- `daily-summary-YYYY-MM-DD.json` — the source of truth.
- `daily-summary-YYYY-MM-DD.txt`  — rendered from the JSON.

Re-running for the same day overwrites (it is a snapshot, not a log).

## CLI

```
docker compose exec trader_worker \
    python -m marketmind_workers.observability.daily_summary
```

With no flags: generates a fresh report against the live DB, writes both
files, and prints the rendered text. With `--latest`: renders the most
recent already-written report without touching the DB.

## Report contents

- **Bot health** — heartbeat freshness, trailing-24h cycle counts,
  errors. A heartbeat stale by >5 min ⇒ status `DOWN` and a prominent
  `BOT NOT RUNNING` banner at the top.
- **Equity** — current equity, 24h change, open positions, closed trades,
  all-time P&L (all GBP).
- **Strategies** — per version: status (`WARMUP` / `EVALUATING` /
  `IN_POSITION` / `DISABLED`), last decision, bar history, state rows,
  trades. Sorted earliest-seeded first.
- **Event counts** — risk events, drift events, disable-and-alert events
  (trailing 24h).
- **Notes** — auto callouts: warmup ETAs, disabled strategies, a bot-down
  line.

## Data sources

| Field | Source |
|---|---|
| Heartbeat | `trader_bot_runs` |
| Cycles / signal-cycles | `trader_audit_logs` |
| Equity | `trader_portfolio_snapshots` |
| Strategies / state rows | `trader_strategy_versions`, `trader_strategy_state` |
| Trades | `trader_paper_positions` |
| Risk / drift / disable-alerts | `trader_risk_events`, `trader_drift_metrics`, `trader_alerts` |

`idempotency_guard_hits` is a per-cycle log stat (`signal_cycle_complete`
`pair_state_guarded`) with no queryable store — it is reported as `0` by
design.

## Schema

`DailySummary` (`marketmind_workers.observability.models`) is the schema.
The JSON file validates as a `DailySummary`; the text view is derived
from it and never the other way round. `now` is threaded through every
query as an explicit parameter (never SQL `NOW()`), so a fixed DB state
produces a byte-identical report — the basis of the snapshot tests.
