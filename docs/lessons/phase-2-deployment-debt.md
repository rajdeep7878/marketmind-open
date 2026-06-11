# Phase 2 deployment debt: six bugs the unit tests never saw

*Written after closing out Phase 2 of MarketMind AI — a strategy-research
tool that ingests trading content, extracts strategies via an LLM, and
will eventually backtest them honestly.*

Phase 2 added the full extraction pipeline: ingest a YouTube video or
article, transcribe the audio with Whisper, hand the transcript to
Claude Sonnet with tool-use to enforce schema conformance, and persist
the result. By the end of Phase 2.2 the test suite was clean (305
Python tests + 6 web tests, all green), pyright passed in strict mode,
ruff had nothing to complain about, and CI was a happy row of
checkmarks.

So I brought the stack up with `docker compose up -d` and submitted a
YouTube URL through the browser.

It didn't work.

I went around the loop **six times.** Each attempt surfaced one bug,
required a real fix, and led to the next attempt. None of the bugs
would have been catchable by the existing test suite. All six were real
production-shaped failures that would have shipped to a first user. The
total cost was roughly £0.46 of Anthropic API spend (mostly from one
failed run where the LLM call succeeded but persistence crashed),
twelve commits, and one git tag.

This is a story about what unit tests don't catch, and what the cheapest
way to find it is.

## The six bugs

I'll keep these short. Each one was diagnosed and fixed before moving
to the next. The pattern matters more than the individual fix.

### 1. Container dependencies (ffmpeg)

**Symptom:** Worker container exited immediately on startup:

```
worker-1  | error  ffmpeg_missing
                  action=install ffmpeg via `brew install ffmpeg` (macOS)
                  ffmpeg_present=False  ffprobe_present=False
```

**Cause:** I'd correctly added a worker startup check that refuses to
boot without `ffmpeg` and `ffprobe` on `PATH` — yt-dlp, faster-whisper,
and our own duration probe all depend on them. The check worked exactly
as designed. The Dockerfile just didn't install ffmpeg into the runtime
image.

**Fix:** Three lines in the worker Dockerfile's runtime stage.

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

**Why CI didn't catch it:** CI installs the workspace via `uv sync` and
runs pytest. It never builds the worker image. The image's startup
behavior was untested.

### 2. Service-to-service env wiring

**Symptom:** Every database-touching worker job failed with:

```
psycopg.OperationalError: connection failed:
  connection to server at "127.0.0.1", port 5432 failed: Connection refused
```

**Cause:** Phase 2.1 added `database_url` to the worker's
`pydantic_settings` model with a localhost default (so settings could
load on the host outside Docker). The api service's compose block
passed `DATABASE_URL` through; the worker's didn't. Inside the worker
container, `localhost:5432` is the worker's own loopback — postgres is
on hostname `postgres` in the compose network.

**Fix:** One line in `docker-compose.yml` under `worker.environment`,
matching the api default. Plus `postgres` added to `worker.depends_on`.

**The same shape would bite again** later in Phase 2 for the Next.js
server-component fetch — `NEXT_PUBLIC_API_URL` was bundled into the
client JS for the browser, but server-side rendering inside the web
container needed a different URL. Two env vars, picked at call time
based on `typeof window === "undefined"`. Same diagnostic shape, same
fix shape.

### 3. The `parents[N]` anti-pattern (twice)

**Symptom (first instance):** Worker startup log warned
`migrations_directory_empty` at
`/opt/venv/lib/infra/db/migrations`. Migrations weren't being applied
at the worker layer — the schema was still in place only because the
postgres init script happened to be mounting the same SQL files
directly into the container.

**Symptom (second instance):** First LLM call failed with
`FileNotFoundError: schemas.json not found at
/opt/venv/lib/web/src/types/generated/schemas.json`.

**Cause:** Both modules resolved a repo-relative resource via
`Path(__file__).resolve().parents[N]`. On the host with an editable
install, `__file__` lives in the source tree and `parents[N]` lands at
the repo root. In a `--no-editable` wheel install (which is what the
Dockerfile uses to keep `.pth` files from pointing at non-existent build
paths), `__file__` lives under `/opt/venv/lib/python3.12/site-packages/`
and `parents[N]` lands at `/opt/venv/lib/`. Neither has the resources.

**Fix pattern:**

1. Use Hatch's `[tool.hatch.build.targets.wheel.force-include]` to copy
   the resource INTO the wheel as package data:

   ```toml
   [tool.hatch.build.targets.wheel.force-include]
   "../infra/db/migrations" = "marketmind_workers/_migrations"
   "../web/src/types/generated/schemas.json" = "marketmind_workers/_schemas.json"
   ```

2. Use `importlib.resources.files("mypackage")` to find them at runtime:

   ```python
   def _resolve_migrations_dir() -> Path:
       try:
           bundled = resources.files("marketmind_workers").joinpath("_migrations")
           if bundled.is_dir() and any(c.name.endswith(".sql") for c in bundled.iterdir()):
               return Path(str(bundled))
       except (FileNotFoundError, ModuleNotFoundError, AttributeError, OSError):
           pass
       # Fallback for editable installs (host-side dev, tests).
       repo_root = Path(__file__).resolve().parents[4]
       return repo_root / "infra" / "db" / "migrations"
   ```

3. Audit periodically:

   ```sh
   grep -rn "parents\[" workers/src/ shared/src/ api/src/
   ```

   Every hit should be either inside a fallback like the one above, or
   a docstring comment about a fallback. Naked `parents[N]` reaching
   outside the package is a Docker-time bug waiting to happen.

**Why this is so easy to write and so hard to catch:** the editable
install path looks identical to a developer's mental model of "where
the file is." Path-resolution code that works in development can be
fundamentally wrong in production — and the failure mode is silent (a
warning log line) until something further downstream actually needs
the resource.

### 4. Schema/code agreement on nullable columns

**Symptom:** First time the LLM ever produced a refusal verdict
(`not_extractable`, `spec=None`):

```
psycopg.errors.NotNullViolation: null value in column "spec_json"
  of relation "extracted_strategies" violates not-null constraint
```

**Cause:** Phase 2.1 declared `spec_json JSONB NOT NULL` when extraction
was a stub and only successful extractions could exist. Phase 2.2
introduced the four-way verdict — `fully_extractable`,
`partially_extractable`, `not_extractable`, `not_a_strategy` — where the
latter two carry `spec=None`. The Pydantic model `ExtractionResult` even
has a model validator enforcing this iff-relationship. But the database
column never got updated.

**Fix:** One migration that drops the NOT NULL constraint and adds a
`COMMENT ON COLUMN` explaining why.

**Why CI didn't catch it:** the integration test that exercises this
exact write path exists (`tests/test_db_integration.py::test_save_and_fetch_extraction`
covers a refusal case). It just doesn't run in CI — it's
`@pytest.mark.integration` because it uses testcontainers, and CI runs
the unit slice. The bug was hiding in plain sight, behind a marker.

### 5. Atomic persistence of related rows

This one cost real money.

**Symptom:** The first LLM call that actually fired in production
succeeded. Anthropic billed us. The model returned a valid extraction
report. But the worker's persistence step crashed (gap #4 above) before
either row could be saved.

The damage: the `save_extraction()` call raised on the NOT NULL
violation; `save_extraction_cost()` was a separate transaction that
came after it, so it never ran. The cost row didn't exist. We had no
audit trail of what we'd paid for.

**Fix:** Combine the writes into a single transaction:

```python
def save_extraction_with_cost(
    database_url, transcript_id, result, *, model, input_tokens, ...
) -> UUID:
    with _connect(database_url) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(<INSERT extracted_strategies ...>)
        extraction_id = ...
        cur.execute(<INSERT extraction_costs ...>)
    return extraction_id
```

**General rule:** if you have two database writes that record related
state — especially when one of them is "we already paid for this" — they
should commit together or not at all. Anything else is a real-money
leak waiting for an exception to widen the hole.

### 6. Next.js server/client URL split

**Symptom:** The form page at `/extract` worked (HTTP 200). The result
page at `/strategies/[id]` returned 500 with `ECONNREFUSED ::1:8000`
in the web container's logs — for an extraction that was demonstrably
in the database.

**Cause:** `/extract` is a client-side React page; its fetch runs in
the user's browser, which can reach the api via the host's port
mapping (`http://localhost:8000`). `/strategies/[id]` is a Next.js
**server component**; its fetch runs inside the web container during
SSR. Same URL, different vantage point: inside the web container,
`localhost:8000` is the web container's own loopback.

`NEXT_PUBLIC_*` env vars are bundled into the client JS at build time
and are the right shape for the browser. They're the wrong shape for
server-side rendering.

**Fix:** Two env vars on the web service, and a helper that picks at
call time:

```ts
export function apiBaseUrl(): string {
  if (typeof window === "undefined") {
    const internal = process.env.API_URL_INTERNAL;
    if (internal && internal.length > 0) {
      return internal;
    }
  }
  return env.NEXT_PUBLIC_API_URL;
}
```

`API_URL_INTERNAL` is server-only and read inside the typeof-window
branch, so Next.js never bundles it into the client.

**Honest moment:** I'd flagged this exact risk in the Phase 2.2 final
report ("worth confirming end-to-end in dev mode before relying on it").
I knew the failure mode existed and still let it ship to smoke testing.
That's how cheap end-to-end smoke testing is compared to imagining
failure modes — actually run the thing.

## Bonus: the LLM-shape regression

After fixing all six, the Quant Tactics extraction was returning
`not_extractable` even though the same transcript reliably extracted to
a real Golden Cross spec during prompt-design testing. Diagnosis from
the persisted report:

```
Validation error:
  [extra_forbidden] metadata.overall_confidence: Extra inputs are not permitted
```

The model produced a valid spec but added a field
`metadata.overall_confidence` that doesn't exist in the schema. Pydantic
rejected it under `extra="forbid"`. The retry-and-downgrade path fired,
both attempts failed the same way, and we recorded a refusal on a
textbook-extractable transcript.

Root cause: a schema-naming collision. `ExtractionReport.overall_confidence`
(top-level on the report) and `Metadata.confidence` (inside the spec)
are documented identically as "overall LLM confidence in the extraction."
The model rationally conflated them and pasted the report-level field
name into the spec-level location.

Fix: one paragraph added to the system prompt's ADDITIONAL RULES
section, explicitly distinguishing the two field names. Verified on a
single targeted retry — the spec now lands clean.

The underlying duplication is real schema debt, filed for Phase 3.

## The cost

| | |
|---|---|
| Anthropic API spend (smoke test loop) | ≈ £0.46 |
| Commits | 12 |
| Smoke test attempts | 6 |
| Bug categories closed | 6 |
| `phase-2-complete` tags pushed | 1 |

Total time was a long evening. The single most expensive single bug was
gap #5 — the failed LLM call where we paid ~$0.20 and lost the audit
trail. Every other bug was caught at compose-time or at the very first
request, before any API spend.

## The lesson

A green test suite is the floor, not the ceiling.

Concretely: the bugs above split into three categories.

**Things unit tests can't see:** Dockerfile contents (#1), compose env
wiring (#2, #6), `--no-editable` install layouts (#3). These exist in
the build/runtime configuration, not the code. The only way to find
them is to actually build and run.

**Things integration tests CAN see but only if they actually run:**
the schema/code agreement on nullable columns (#4) was already covered
by an existing integration test, but the test was gated behind
`@pytest.mark.integration` and CI was running the unit slice only.
Having an integration test is necessary but not sufficient — it has to
be wired into the deployment pipeline somewhere.

**Things only a full request-flow can see:** the
service-to-service-URL-split kind of bugs (#6), the related-rows
transaction problem (#5). These require the request to actually flow
all the way through the stack. They aren't testable in isolation
because they're about how isolated pieces compose.

The cheapest way to find all three categories: **CI brings up the
compose stack and drives one end-to-end request.** Pick the cheapest
real flow you have — for us, a refusal extraction is ~$0.04 and runs
in ~10 seconds. Do that once per PR and you catch every shape of bug
in this article.

I've filed it as Phase 3 housekeeping. It's how MarketMind will catch
the next bug like this one before I do.
