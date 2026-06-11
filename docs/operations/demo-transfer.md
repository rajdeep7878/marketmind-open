# Transferring demo strategies to production

`scripts/transfer_demos.py` copies pre-extracted demo strategies (with
their transcript, ingested-content, backtest, and overfitting-analysis
rows) from one Postgres instance to another. The intended use is
seeding production with the three homepage receipts when fresh
extractions can't run there.

## When to use this

**Phase 5.2b — Option 5 fallback.** YouTube's bot-detection blocks
`/content/ingest` from Railway's datacenter IPs (see the
`YOUTUBE_COOKIES_B64` notes in `docs/deployment/env-vars.md` for the
mitigation path). Until cookie auth is verified working end-to-end
in production, the homepage's three "receipts" cards link to UUIDs
that exist only in local Postgres. Running this script copies them
across so the production homepage isn't broken.

Other operational uses where this script is also the right tool:
- Restoring a single strategy chain after a destructive change in
  production (e.g. an accidental DELETE) when you have the chain in
  a staging or local DB.
- Reproducing a customer's strategy locally for debugging — same
  script, source and destination swapped.

## Strategy IDs to transfer

For the Phase 5.2a homepage receipts (`web/src/app/page.tsx` →
`RECEIPTS`):

| Receipt          | Strategy id (extracted_strategies.id)    | Source video                                  |
|------------------|------------------------------------------|-----------------------------------------------|
| Bollinger + RSI  | `1facd855-f995-4b73-85b2-a0a789a38044`   | `youtube.com/watch?v=pCmJ8wsAS_w`             |
| Golden Cross     | `8045af6b-80db-454e-8769-177171ea5070`   | `youtube.com/watch?v=5s8R_BBwgEk`             |
| Dungeon Channels | `ce2ed293-e140-4d1d-96d9-d5a68fb0536b`   | `youtube.com/watch?v=9JEmsSItdt4` (refused)   |

The third strategy has no backtest (the LLM refused the extraction);
the script handles that case automatically — backtest / overfitting
queries return zero rows and nothing downstream gets written.

## How it works

For each strategy id you pass, the script walks the foreign-key
chain on the source DB:

```
extracted_strategies (id = <yours>)
    │
    ├─ transcripts (id = strategy.transcript_id)
    │       │
    │       └─ ingested_content (id = transcript.content_id)
    │
    └─ backtest_results (strategy_id = strategy.id)   [0..N rows]
            │
            └─ overfitting_analyses (backtest_id = backtest.id)
```

It then inserts every row into the destination in FK order
(`ingested_content` → `transcripts` → `extracted_strategies` →
`backtest_results` → `overfitting_analyses`) using
`INSERT ... ON CONFLICT (id) DO NOTHING`, all inside a single
transaction.

Before any writes, the script does a schema sanity check: it pulls
`information_schema.columns` from both sides for the five affected
tables and refuses to run if any column name, type, or ordinal-
position differs. If the check fails, apply the destination's missing
migrations first (running a worker against the destination is the
laziest way — it auto-applies `infra/db/migrations/*.sql` at startup).

## Not transferred

- **`extraction_costs`** rows. They share an FK chain with
  `extracted_strategies` and are written atomically with extractions
  in production, but they exist only to populate `/admin/stats` spend
  totals — the homepage receipts don't read them. If you ever need
  them in the destination, extend `DEPENDENCIES_IN_ORDER` in the
  script with a fetcher that joins on `extracted_strategy_id`.

## Running it

```sh
# 1. Set the destination DSN. Get it from Railway → Postgres add-on →
#    Connect → Postgres Connection URL. Use the *external* URL when
#    running this from your laptop; the internal one only resolves
#    inside Railway's private network.
export PROD_DATABASE_URL="postgresql://USER:PW@HOST.proxy.rlwy.net:PORT/railway"

# 2. (Optional) override the source. Defaults to the compose Postgres
#    on localhost:5432.
# export LOCAL_DATABASE_URL="postgresql://marketmind:marketmind_dev@localhost:5432/marketmind"

# 3. Dry run first — reads source + sanity-checks schemas, prints the
#    plan, writes nothing.
uv run python scripts/transfer_demos.py --dry-run \
  1facd855-f995-4b73-85b2-a0a789a38044 \
  8045af6b-80db-454e-8769-177171ea5070 \
  ce2ed293-e140-4d1d-96d9-d5a68fb0536b

# 4. If the dry run looks right, run for real.
uv run python scripts/transfer_demos.py \
  1facd855-f995-4b73-85b2-a0a789a38044 \
  8045af6b-80db-454e-8769-177171ea5070 \
  ce2ed293-e140-4d1d-96d9-d5a68fb0536b
```

The script masks the password in all stdout output. The final report
shows per-table inserted / skipped counts so you can confirm exactly
what changed:

```
[report] destination write results:
  table                       inserted     skipped
  ------------------------    ----------  ----------
  ingested_content                   3           0
  transcripts                        3           0
  extracted_strategies               3           0
  backtest_results                   2           0
  overfitting_analyses               2           0
```

## Re-running

The script is **idempotent**: re-running with the same arguments after
a successful (or partially successful) run inserts zero new rows and
skips everything. Useful if you ever need to verify the destination
has the demos without remembering which strategies you already
copied.

One caveat to the idempotency: `ON CONFLICT (id) DO NOTHING` means a
row with the same id but *different content* on the destination is
silently kept. That's fine for demo data (ids are stable, content is
write-once), but don't repurpose this script for arbitrary syncing
without thinking about that.

## Safety checks the script enforces

- `PROD_DATABASE_URL` must be set explicitly — no default. The local
  source URL has a sensible compose-shaped default; the destination
  doesn't, because there's no defensible default for "production".
- Refuses to run if source and destination resolve to the same
  host/port/database (paranoia: nobody intends to copy a DB into
  itself).
- Single transaction on the destination: either the whole transfer
  commits or nothing does. No half-applied chain even on connection
  drop mid-write.
- No defaults for strategy ids — you pass them on the command line
  every time. Prevents the "I forgot to update the script and shipped
  last quarter's demo set" failure mode.
