# Trader v1 — local operator runbook

How to run the trader v1 entirely on your Mac, with no Railway or
cloud involvement. Companion to `trader-runbook.md` (which is
host-agnostic); this file is the concrete Mac-local recipe.

Stack: docker compose for Postgres + Redis (+ optionally api / web /
worker / trader_worker). The trader runner can also run on the host
via `uv run python -m marketmind_workers.trader.runner`, which is
the recommended dev workflow because logs land directly in your
terminal and SIGTERM is `Ctrl-C`.

---

## 0. One-time setup

```bash
# Copy the template + fill in any secrets you actually need.
cp infra/.env.example .env

# Install Python deps into the workspace venv.
uv sync

# Install ffmpeg (needed by the Phase 2 worker; the trader doesn't use it).
brew install ffmpeg
```

`.env` defaults will run the trader with:

- £1000 paper account
- BTC/USDT + ETH/USDT at 4h
- 1% per-trade risk, 5% portfolio risk, 2% daily / 5% weekly loss caps, 10% drawdown kill switch
- No Telegram delivery (alerts land in `trader_alerts` with `delivered=False`)
- No admin auth (every `POST /trader/admin/*` returns 503 until you set `ADMIN_USERNAME` / `ADMIN_PASSWORD`)

Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` before you try
to approve a strategy.

---

## 1. Start the stack (two modes)

### Mode A — everything in docker

```bash
docker compose up -d
docker compose ps
```

All five services come up: postgres, redis, api, worker,
trader_worker, web. Postgres and Redis pin their data to named
volumes (`pgdata`, `redisdata`) — `docker compose down` preserves
your trade history; only `docker compose down -v` wipes it.

### Mode B (recommended for dev) — infra in docker, services on host

```bash
# Just the data layer.
docker compose up -d postgres redis

# Apply migrations to the local DB (idempotent — safe to re-run).
DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \
  uv run python -c "from marketmind_workers.db.migrations import apply_migrations; \
    [print(f) for f in apply_migrations('postgresql://marketmind:marketmind_dev@localhost:5432/marketmind')]"

# In separate terminals (in this order):

# Terminal 1 — api
DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \
REDIS_URL=redis://localhost:6379/0 \
ADMIN_USERNAME=admin ADMIN_PASSWORD=set-a-real-password \
  uv run uvicorn marketmind_api.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2 — trader runner
DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \
REDIS_URL=redis://localhost:6379/0 \
TRADER_ALLOW_LIVE=false \
  uv run python -m marketmind_workers.trader.runner

# Terminal 3 — web (optional)
cd web && pnpm install && pnpm dev
```

Mode B is faster to iterate on (no rebuilds), and `Ctrl-C` in the
trader terminal triggers a graceful shutdown (status → 'stopped').

---

## 2. Confirm the bot is alive

```bash
# bot_run row should be present + status='running' + heartbeat recent
docker compose exec postgres psql -U marketmind -d marketmind -c \
  "SELECT id, loop_name, status, last_heartbeat_at, notes FROM trader_bot_runs ORDER BY started_at DESC LIMIT 1"

# Latest portfolio snapshot — written every minute
curl -s http://localhost:8000/trader/portfolio/current | jq .

# Risk-status — kill_switch_tripped must be false
curl -s http://localhost:8000/trader/risk/status | jq .
```

---

## 3. Seed a strategy

You need an `extraction_id` from MarketMind that has both a
`backtest_results` row AND an `overfitting_analyses` row with a
non-empty `walk_forward.windows`. The seed script fails fast if
any of those are missing.

```bash
# 1. Dry-run to inspect the JSONB payloads.
DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \
  uv run python scripts/trader_seed_strategy.py \
    --extraction-id <UUID-FROM-MARKETMIND> \
    --name "MA Trend BTC 4h v1" \
    --template ma_trend \
    --parameters-json '{"fast_ema_period": 12, "slow_ema_period": 26, "trend_ema_period": 200, "atr_period": 14, "atr_mult": "2.0"}' \
    --symbols BTC/USDT \
    --timeframes 4h \
    --risk-pct 0.005 \
    --dry-run

# Inspect the printed JSONB. Confirm:
#   walk_forward.out_of_sample_trade_freq_per_week ≈ what you saw in MarketMind
#   single_pass.win_rate, avg_return_per_trade, max_drawdown_pct
#     match the backtest_results page

# 2. Seed for real — drop --dry-run.
DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \
  uv run python scripts/trader_seed_strategy.py \
    --extraction-id <UUID-FROM-MARKETMIND> \
    --name "MA Trend BTC 4h v1" \
    --template ma_trend \
    --parameters-json '{"fast_ema_period": 12, "slow_ema_period": 26, "trend_ema_period": 200, "atr_period": 14, "atr_mult": "2.0"}' \
    --symbols BTC/USDT \
    --timeframes 4h \
    --risk-pct 0.005
```

The script prints `strategy_version_id`. **Note it down — you need
it to approve.**

The version lands with `approved_for_paper=FALSE`. The signal
engine sees it (because `enabled=TRUE`) but the risk manager blocks
every signal it emits with a `strategy_not_paper_approved` event
until you approve.

---

## 4. Approve a strategy for paper trading

Requires `ADMIN_USERNAME` + `ADMIN_PASSWORD` set on the API.

```bash
# Inspect the version BEFORE approving.
curl -s -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" \
  http://localhost:8000/trader/strategies | jq '.items[] | select(.id=="<VERSION-ID>")'

# Approve.
curl -s -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" \
  -X POST http://localhost:8000/trader/strategies/<VERSION-ID>/approve_paper | jq .

# If approve returns 422, the seed payload was incomplete.
# detail.missing_keys names the problem. Re-seed after fixing the
# upstream MarketMind data.
```

To turn a version off later (open positions keep honouring stops):

```bash
curl -s -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" \
  -X POST http://localhost:8000/trader/strategies/<VERSION-ID>/disable | jq .
```

---

## 5. Watch the bot via the API

All read endpoints are unauthenticated. Curl + jq makes everything
inspectable.

```bash
# Most recent portfolio snapshot (cash, equity, drawdown_pct).
curl -s http://localhost:8000/trader/portfolio/current | jq .

# Risk status + recent risk events (kill_switch, daily_loss_block, …).
curl -s http://localhost:8000/trader/risk/status | jq .

# All OPEN positions.
curl -s http://localhost:8000/trader/positions/open | jq '.items[] | {symbol, entry_price, stop_price, size, side}'

# Recent fills (most recent first).
curl -s http://localhost:8000/trader/fills/recent | jq '.items[0:5]'

# Recent signals (BUY/SELL/EXIT — no HOLDs, those are deliberately not persisted).
curl -s http://localhost:8000/trader/signals/recent | jq '.items[0:5]'

# Equity curve since a given UTC timestamp.
curl -s "http://localhost:8000/trader/portfolio/equity_curve?since=2026-05-19T00:00:00Z" | jq .

# Recent alerts (delivered=false means Telegram either failed or wasn't configured).
curl -s http://localhost:8000/trader/alerts/recent | jq '.items[0:5]'

# Drift status (one row per strategy version, populated daily at 01:00 UTC).
curl -s http://localhost:8000/trader/drift/latest | jq .

# Audit log — every state change the bot wrote.
curl -s "http://localhost:8000/trader/audit/recent?limit=20" | jq '.items[] | {ts, event, entity_type, payload}'
```

---

## 6. Gracefully stop the bot

If the runner is on the host (Mode B):

```bash
# In the trader terminal — RQ's signal handler catches SIGINT.
Ctrl-C
```

You'll see `trader_run_stopped` in the log + the bot_run row's
status flips to `stopped`. The current cycle finishes (it's <2
seconds), the scheduler shuts down, advisory locks release on
commit.

If the runner is in docker (Mode A):

```bash
docker compose stop trader_worker
# Or, to keep the whole stack but stop just the trader:
docker compose stop trader_worker
# To start again later:
docker compose start trader_worker
```

`docker compose stop` sends SIGTERM with a 10-second grace period
by default — plenty for the trader to land cleanly.

---

## 7. Inspect trade history via psql

```bash
# Open a psql shell against the local DB.
docker compose exec postgres psql -U marketmind -d marketmind

# Useful queries inside the shell:

-- Trade history (round trips: entry + exit).
SELECT symbol, side, entry_price, exit_price, size, realised_pnl,
       realised_pnl_pct, close_reason, entry_ts, exit_ts
FROM trader_paper_positions
WHERE status = 'CLOSED'
ORDER BY exit_ts DESC LIMIT 20;

-- Open positions right now.
SELECT symbol, side, entry_price, stop_price, size,
       (SELECT close FROM trader_candles
        WHERE symbol = p.symbol AND timeframe = '4h'
        ORDER BY close_ts DESC LIMIT 1) AS latest_mark,
       entry_ts
FROM trader_paper_positions p
WHERE status = 'OPEN';

-- Recent risk-manager blocks with reasons.
SELECT ts, event_type, severity, symbol, details
FROM trader_risk_events
ORDER BY ts DESC LIMIT 20;

-- Equity curve from snapshots.
SELECT ts, cash, equity, drawdown_pct, open_positions_count
FROM trader_portfolio_snapshots
ORDER BY ts DESC LIMIT 50;

-- Drift breaches (one per strategy version per day).
SELECT v.notes AS strategy, d.ts, d.window_label, d.health_status,
       d.paper_trade_count, d.paper_win_rate, d.drawdown_ratio
FROM trader_drift_metrics d
JOIN trader_strategy_versions v ON v.id = d.strategy_version_id
ORDER BY d.ts DESC LIMIT 20;
```

To exit: `\q`.

---

## 8. Mac-specific: keep the laptop awake near 4h candle closes

The 4h candle boundaries in UTC are: **00:00, 04:00, 08:00, 12:00,
16:00, 20:00**. On the trader's 1-minute cycle, the relevant signal
gets generated at the first cycle after that boundary (so signals
fire within ~60–90s of the bar closing).

If your Mac sleeps through a boundary, the trader misses that
signal. When the laptop wakes, the runner catches up — but only
for new bars that have closed since. **It does NOT back-fill old
signals.** A 12-hour overnight sleep means you'll miss the 04:00
and 08:00 windows.

Two ways to avoid this:

```bash
# Option 1: keep the Mac awake while the bot runs.
caffeinate -d -i -s &
# (run for the session; `kill %1` or Ctrl-C the process to stop).

# Option 2: set a System Settings → Battery → Options policy
# that keeps the Mac awake when on power. Then leave the lid open
# and plug in. This survives reboots; caffeinate doesn't.
```

For a true 24/7 paper trader on a Mac, the cleanest answer is to
ship the runner to a tiny cloud box (Railway, a Hetzner CX11, a
Raspberry Pi). The build is already plumbed for that.

---

## 9. What's normal vs. what's not

| Observation | Severity | Action |
|---|---|---|
| `trader_main_cycle_complete` every ~60s in logs | Normal | None |
| `signal_cycle_complete versions=0` | Normal until you approve a version | None |
| `risk_cycle_complete signals_blocked > 0` | Normal — the risk manager IS doing its job | Inspect `trader_risk_events.event_type` |
| `kill_switch` row in `trader_risk_events` | **Stop** — drawdown threshold tripped | Disable every version; investigate |
| `trader_alerts` row with `severity='critical'` | High | Check `subject` — phase failure or stale heartbeat |
| `last_heartbeat_at` more than 5 min stale | High | The stale-detector should flip it to 'crashed' within 5 min; if not, the runner process is dead |
| `candles_inserted=0` for several cycles | Investigate | Binance API hiccup or symbol typo |
| `ffmpeg_missing` at boot of the Phase-2 worker | Normal if you didn't `brew install ffmpeg` — only the Phase-2 worker needs it, not the trader | `brew install ffmpeg` only if you also want extraction to work |
