# v2 Rollback Procedure

How to revert MarketMind to its v1 final state if v2 development breaks the
running trader bot or the local stack. Written at the start of v2 Phase A
(2026-05-20).

## The safety net

`main` is frozen at the v1 final state and does **not** move during v2 work:

| Commit | What it is |
|--------|-----------|
| `9e64326` | v1 final — `main` HEAD and `origin/main`. v1.1 todos doc. |
| `6233c1e` | worker `restart: unless-stopped` fix (parent of `9e64326`). |

Both are pushed to `origin/main` (verified 2026-05-20). All v2 work happens on
the `v2-phase-a-stateful-conditions` branch (and later v2 branches); `main`
stays at `9e64326`. The v1 codebase is therefore always one `git checkout main`
away — locally and on GitHub.

## What is and isn't at risk

The running trader bot executes from the **`trader_worker` container**, whose
image was built from whatever was checked out at build time. Checking out the
v2 branch or editing v2 files does **not** affect the running container — only
a rebuild (`docker compose up --build` / `docker compose build`) swaps the code
the bot runs.

So during v2 development the bot is safe **as long as the `trader_worker` (and
`worker`) images are not rebuilt off the v2 branch**. A rebuild is the only
action that puts the live bot on v2 code.

## Rollback procedure

If a v2 rebuild has been done and it breaks the bot or the stack:

```bash
# On the Windows/WSL2 host, in the repo root:
cd ~/marketmind-ai
git checkout main                 # back to v1 (9e64326)
docker compose down               # stop + remove containers — NOT volumes
docker compose up -d --build      # rebuild images from main, restart
```

- `git checkout main` puts the working tree back on v1 code.
- `docker compose down` removes the containers and the compose network. It does
  **not** remove named volumes — `pgdata` and `redisdata` persist.
- `docker compose up -d --build` rebuilds every image from the v1 checkout and
  starts the stack.

The bot comes back on v1 code against the **same database**: strategy versions,
the `trader_candles` history, portfolio snapshots, open positions, and the
audit log all live in the `pgdata` volume and survive the rebuild. The trader
runner resumes from wherever it left off (e.g. mid candle-warmup). The
`restart: unless-stopped` policy is part of v1 (`6233c1e`), so it is restored
by the rollback too.

### Critical: never use `docker compose down -v`

`-v` removes named volumes. That wipes `pgdata` (the entire trader DB — strategy
versions, candle history, positions, audit log) and `redisdata`. A plain
`docker compose down` is the rollback command. `down -v` is a destructive reset,
not a rollback.

## If the Windows DB itself is corrupted

The procedure above assumes the `pgdata` volume is intact. If the Windows
Postgres volume is corrupted — not just the code:

- The **Mac is the deeper backup.** The original repo and its Docker volumes on
  the Mac hold the pre-migration v1 state. Recovering means re-running the
  migration on a clean machine (clone, populate `.env`, `docker compose up`) and
  re-seeding the trader strategies through the extract → backtest → overfitting
  → seed → approve chain.
- A Mac recovery loses any trader state accumulated on Windows since the
  migration (candle history, paper fills, snapshots). For a bot still in warmup
  that loss is negligible.

## Quick reference

| Situation | Action |
|-----------|--------|
| v2 branch work, no rebuild | Nothing — the bot is unaffected |
| v2 rebuild broke the bot | `git checkout main` → `docker compose down` → `up -d --build` |
| Windows `pgdata` corrupted | Recover from the Mac, re-migrate + re-seed |
| Never (unless deliberately wiping) | `docker compose down -v` |
