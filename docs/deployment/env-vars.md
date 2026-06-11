# Production environment variables

Catalogue of every environment variable MarketMind reads in production,
grouped by service. Use this as the source-of-truth checklist when
configuring Railway (Phase 5.2b) or any other host. Values shown are
example/default — substitute real production values before launch.

A value marked **secret** must be stored in Railway's secret manager
(or equivalent) and must never be committed to the repo. Values marked
public are safe to expose (the `NEXT_PUBLIC_*` family is bundled into
the client JS).

The schema is enforced by `pydantic-settings` (API, worker) and `zod`
(web) at process start — a missing required var fails the boot
loudly, not silently at first request.

---

## Postgres

| Var                 | Example                                 | Purpose                      | Secret |
|---------------------|-----------------------------------------|------------------------------|--------|
| `POSTGRES_USER`     | `marketmind`                            | DB role used by api + worker | no     |
| `POSTGRES_PASSWORD` | `…`                                     | DB role password             | yes    |
| `POSTGRES_DB`       | `marketmind`                            | Database name                | no     |

When Railway provisions Postgres it produces a single `DATABASE_URL`
that already encodes user/password/db — use that directly on the api
and worker services. The three variables above are only needed if you
run the Postgres container yourself.

---

## Redis

Railway's Redis add-on exposes a `REDIS_URL`. Otherwise:

| Var          | Example                       | Purpose                          | Secret |
|--------------|-------------------------------|----------------------------------|--------|
| `REDIS_URL`  | `redis://default:pw@host:6379/0` | RQ broker + rate-limit + cost cap | yes |

---

## API service (`marketmind-api`)

| Var                          | Example                                                   | Purpose                                                                                          | Secret |
|------------------------------|-----------------------------------------------------------|--------------------------------------------------------------------------------------------------|--------|
| `ENVIRONMENT`                | `production`                                              | One of `development | staging | production | test`. Toggles CORS strictness and OpenAPI docs.    | no     |
| `LOG_LEVEL`                  | `INFO`                                                    | structlog level. `DEBUG` only for short-lived debugging.                                         | no     |
| `API_HOST`                   | `0.0.0.0`                                                 | uvicorn bind host. Inside a container this is normally `0.0.0.0`.                                | no     |
| `API_PORT`                   | `8000`                                                    | uvicorn port. Railway expects a `PORT` env var; map it through.                                  | no     |
| `DATABASE_URL`               | `postgresql://user:pw@host:5432/marketmind`               | Postgres DSN. `postgresql://` (psycopg3) — strip any `+psycopg2` suffix.                         | yes    |
| `REDIS_URL`                  | `redis://host:6379/0`                                     | Redis DSN.                                                                                       | yes    |
| `RQ_QUEUE_NAME`              | `default`                                                 | RQ queue the API enqueues to. Workers must listen on the same name.                              | no     |
| `DATA_DIR`                   | `/data`                                                   | Filesystem path for any per-request scratch space. Mount a volume here in production.            | no     |
| `ANTHROPIC_API_KEY`          | `sk-ant-…`                                                | Required from Phase 2.2 onward. API doesn't call Anthropic directly but validates settings at boot. | yes |
| `RATE_LIMIT_INGEST_PER_DAY`  | `5`                                                       | Per-IP /content/ingest cap in a rolling 24h window.                                              | no     |
| `DAILY_COST_CAP_GBP`         | `5.0`                                                     | Hard daily ceiling on Anthropic spend, in GBP. 0 disables.                                       | no     |
| `GBP_USD_RATE`               | `1.27`                                                    | Fixed GBP→USD conversion rate for the cap check. Adjust quarterly.                               | no     |
| `ADMIN_USERNAME`             | `admin`                                                   | HTTP basic auth for /admin/* on the API.                                                         | yes    |
| `ADMIN_PASSWORD`             | `…long random string…`                                    | Paired with ADMIN_USERNAME. Both must be set or /admin/* returns 503.                            | yes    |
| `CORS_ORIGINS`               | `https://marketmind-web-production.up.railway.app`        | Comma-separated allow-list for browser preflight. Unset/empty → every cross-origin POST returns 400. | no |

---

## Worker service (`marketmind-workers`)

The worker shares most env vars with the API. Differences:

| Var                          | Example                                                   | Purpose                                                                                          | Secret |
|------------------------------|-----------------------------------------------------------|--------------------------------------------------------------------------------------------------|--------|
| `ENVIRONMENT`                | `production`                                              | Same semantics as the API.                                                                       | no     |
| `LOG_LEVEL`                  | `INFO`                                                    |                                                                                                  | no     |
| `DATABASE_URL`               | `postgresql://user:pw@host:5432/marketmind`               | Same Postgres as the API; worker also applies migrations at boot.                                | yes    |
| `REDIS_URL`                  | `redis://host:6379/0`                                     | Same Redis. Worker writes to the daily-cost counter, RQ reads its jobs.                          | yes    |
| `RQ_QUEUE_NAME`              | `default`                                                 | Must match the API.                                                                              | no     |
| `DATA_DIR`                   | `/data`                                                   | Audio downloads + transcription scratch space. Volume mount.                                     | no     |
| `ANTHROPIC_API_KEY`          | `sk-ant-…`                                                | Required from Phase 2.2 onward — the worker actually calls Anthropic.                            | yes    |
| `YOUTUBE_COOKIES_B64`        | `IyBOZXRzY2FwZSBIVFRQ…` (very long string)                | Base64-encoded Netscape-format cookies.txt for yt-dlp. Bypasses YouTube's bot-detection challenge on datacenter IPs. Optional locally; **required on Railway** or extractions return 503 once YouTube flags the IP. | yes |

### Rotating YouTube cookies (`YOUTUBE_COOKIES_B64`)

Datacenter IPs trip YouTube's "Sign in to confirm you're not a bot"
challenge. yt-dlp can authenticate past it with a cookies.txt file;
we ship that file by encoding it into a single env var.

**Export the cookies.** Use a Chrome extension that emits Netscape
format (e.g. "Get cookies.txt LOCALLY"). Save the file from a browser
session that's signed into a low-risk YouTube account (consider a
throwaway, not your personal account — the cookies grant full session
access). The exported file is plain text starting with
`# Netscape HTTP Cookie File`.

**Encode and copy to clipboard.**
```sh
base64 -i youtube_cookies.txt | tr -d '\n' | pbcopy
```

**Paste into Railway** → worker service → Variables → `YOUTUBE_COOKIES_B64`.
The worker container materialises the file to `/tmp/youtube_cookies.txt`
at boot (mode 0600) and passes its path to every yt-dlp invocation.

**Rotation cadence.** Cookies expire roughly every 2–4 weeks in
practice. When extractions start returning 503 with
`{"error": "extraction_unavailable"}` (the API-side surface for the
worker's `CookieError`), re-export and update the env var. The
deployment doesn't auto-detect expiry — operational reality is
"a user reports it" → re-export → push.

**Local dev.** Leaving `YOUTUBE_COOKIES_B64` unset is the expected
state. Residential IPs don't trip the bot-detection path, so
extractions work without cookies.

### Trader Oanda adapter (Phase C C.1.3 — provisioning gated to C.1.6)

The `OandaAdapter` in `workers/.../trader/exchanges_oanda.py` reads
three environment variables for FX + metals market-data ingestion.
**Not yet required** — the C.1.3 sub-phase ships the adapter against
cassette tests only (`workers/tests/cassettes/oanda/`). Live-API
smoke is gated to C.1.6 per Phase C design doc §10.4 ("Oanda
paper-account credentials provisioned + documented in env-vars").

| Var                  | Example                            | Purpose                                                                                                          | Secret |
|----------------------|------------------------------------|------------------------------------------------------------------------------------------------------------------|--------|
| `OANDA_API_KEY`      | `1234abcd…` (Oanda token)          | Bearer token from the Oanda fxTrade account dashboard. Passed in the `Authorization: Bearer …` header.            | yes    |
| `OANDA_ACCOUNT_ID`   | `101-001-1234567-001`              | Practice-account ID. Not currently used by the candles endpoint but read at startup for symmetry + future order endpoints (which will NOT live in this adapter — see PAPER-SAFE BY CONSTRUCTION). | no     |
| `OANDA_ENVIRONMENT`  | `practice`                         | **MUST be `"practice"`** in Phase C. The adapter raises `IngestionError` IMMEDIATELY at construction on any other value (including `"trade"`), before any HTTP call is attempted. Phase D will require a separate adapter class for live trading. | no     |

**Provisioning steps when C.1.6 is ready.**
1. Sign up at https://www.oanda.com/demo-account/ — free, instant
   approval, no funding required.
2. From the dashboard → My Account → Manage API Access → Generate
   token. Copy the token immediately; Oanda only shows it once.
3. Note the account ID format `101-001-1234567-001` from the same page.
4. Set the three env vars on the worker service. Leave
   `OANDA_ENVIRONMENT=practice` — there is no production-allowed
   value during Phase C.

**Local dev.** All three may be left unset — the adapter's unit
tests run against VCR cassettes that carry their own dummy token.
Setting real values is only needed once C.1.6 lands the live-API
smoke test.

### Phase C.1.6 verification (2026-05-26)

Live-API smoke tests live at `workers/tests/test_oanda_live_smoke.py`
with the `live_api` pytest marker. Default pytest invocation skips
them (`pyproject.toml` deselects `live_api`); to run explicitly:

```sh
uv run pytest -m live_api workers/tests/test_oanda_live_smoke.py -v
```

The three live tests confirm:
1. `make_adapter("fx_spot")` dispatches to OandaAdapter and fetches
   EUR/USD 1H candles from the live practice endpoint.
2. `ingest_one_cycle` with `TRADER_SYMBOLS="EUR/USD"` lands rows
   in `trader_candles` (test cleans up after assertion).
3. Real Oanda creds + `OANDA_ENVIRONMENT="trade"` STILL raise
   `IngestionError` at the factory — paper-only guard is real.

The smoke tests will skip gracefully if `OANDA_API_KEY` /
`OANDA_ACCOUNT_ID` are unset, so they're safe to keep in the
test suite even on machines without Oanda creds.

---

## Web service (`marketmind-web`, Next.js)

Two API URLs are needed because Next.js fetches from two places —
the browser and the server-side render runtime.

| Var                              | Example                                  | Purpose                                                                                              | Secret | When read       |
|----------------------------------|------------------------------------------|------------------------------------------------------------------------------------------------------|--------|-----------------|
| `NEXT_PUBLIC_API_URL`            | `https://api.marketmind.example`         | Browser-facing API URL. Inlined into the client JS at build time.                                    | no     | build + runtime |
| `API_URL_INTERNAL`               | `http://api:8000`                        | Server-side API URL used by Next.js server components / route handlers. Inside the same private network. | no | runtime         |
| `NEXT_PUBLIC_PLAUSIBLE_DOMAIN`   | `marketmind.example`                     | Plausible site identifier. Unset → analytics snippet not emitted.                                    | no     | build           |
| `NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL`| `https://plausible.io/js/script.exclusions.js` | Optional override for the Plausible script URL. Defaults to the exclusions variant so /admin/* is skipped. | no | build |
| `ADMIN_USERNAME`                 | `admin`                                  | Used by `web/src/middleware.ts` to gate /admin/*. Must match the API's `ADMIN_USERNAME`.             | yes    | runtime         |
| `ADMIN_PASSWORD`                 | `…long random string…`                   | Paired with ADMIN_USERNAME. Must match the API.                                                      | yes    | runtime         |
| `NEXT_TELEMETRY_DISABLED`        | `1`                                      | Suppress Next.js's anonymous telemetry.                                                              | no     | build + runtime |

### `NEXT_PUBLIC_*` are build-time on Railway

Next.js inlines every `NEXT_PUBLIC_*` reference into the client bundle
during `next build`. Setting them at runtime only is too late — the
strings are already baked into the generated JS by then.

Each `NEXT_PUBLIC_*` variable is declared as `ARG` in
`infra/web.Dockerfile`'s builder stage. Railway's Dockerfile builder
**automatically forwards** any service variable whose name matches a
declared `ARG` as `--build-arg`, so the operator workflow is the
expected one — just set the variable on the Railway service. No special
"build-arg" UI step is required as long as the ARG exists in the
Dockerfile.

`NEXT_PUBLIC_API_URL` is also validated at build time by
`web/src/lib/env.ts` (zod, must be a valid URL) — missing or malformed
values fail `pnpm build` during the "Collecting page data" phase.

---

## Deployment-time checklist

1. Provision Postgres + Redis add-ons (or self-host).
2. Set both `ADMIN_USERNAME` / `ADMIN_PASSWORD` to the same value on
   the api **and** the web service. Mismatched values lock you out of
   the dashboard.
3. Set `ANTHROPIC_API_KEY` on the worker (and api, for boot validation).
4. Set `NEXT_PUBLIC_API_URL` to the public api hostname and
   `API_URL_INTERNAL` to the private hostname.
5. Set `CORS_ORIGINS` on the api to the deployed web origin
   (comma-separated if multiple, e.g. preview environments). The
   prod web origin won't reach the api without this — browser
   preflight returns 400.
6. Set `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` if you've created a Plausible
   site for this deployment.
7. Confirm `DAILY_COST_CAP_GBP` matches the wallet ceiling you want
   to enforce. Default £5 is intentionally conservative.
8. After first deploy, hit `https://.../admin/stats` with the basic-
   auth credentials to confirm the observability path works.

## What deliberately isn't here

- `SENTRY_DSN` or any error-monitoring var — Phase 5.2b decision.
- `OPENAI_API_KEY` — never used; we're Anthropic-only.
- Any rate-limit / cost-cap overrides for /strategies/* or
  /backtests/* — only /content/ingest is gated in v1.
