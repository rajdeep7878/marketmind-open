# Architecture

## What we're building

MarketMind AI ingests trading content (YouTube transcripts, articles, blog posts), uses an LLM to extract the rules of the strategy described, validates the extraction against a strict JSON schema, runs the strategy through a backtesting engine, then stress-tests it against an anti-overfitting battery (walk-forward analysis, Monte Carlo permutation, deflated Sharpe, synthetic data) to produce a single "would this have actually worked?" verdict.

The differentiator is rigor, not breadth. Most "strategy review" content online is hype; the value here is in honest backtesting.

## Stack at a glance

| Concern | Choice | Why |
|---|---|---|
| HTTP layer | FastAPI + Pydantic v2 | Async, fast, schema-driven. Pydantic is the source of truth for cross-service contracts. |
| Background jobs | Redis + RQ | Solo-dev simplicity. Celery's flexibility isn't worth the operational tax here. |
| Database (metadata) | Postgres 16 + JSONB | Specs are dynamic; JSONB lets us index into them without an ORM ceremony. |
| Database (market data) | Parquet on disk | Columnar, compressed, cheap. Postgres for OHLCV would be a mistake. |
| LLM | Anthropic Claude (Sonnet + Haiku) | Tool-use schema enforcement gives us structured output; Haiku does the cheap stuff. |
| Backtesting | vectorbt | Vectorized, fast, lets us focus on the spec → operations translation. |
| Frontend | Next.js 14 App Router + Tailwind + shadcn/ui | Standard. Server components keep API surface area small. |
| Containerization | Docker Compose | One-node deployment to Hetzner; no Kubernetes until forced. |
| Python tooling | uv + ruff + pyright | See ADR-0003. |

## Service topology

```
                    +---------------+        +------------+
            HTTP    |               |  RQ    |            |
   User --> :8000 --|   FastAPI     |------->|  Workers   |--+
                    |   (api/)      |        | (workers/) |  |
                    +-------+-------+        +-----+------+  |
                            |                      |         |
                            |                      |         |
                       SQL  v                pickle v    files v
                      +-----+----+         +--------+-+   +----+----+
                      | Postgres |         |  Redis   |   | data/   |
                      +----------+         +----------+   | Parquet |
                                                          +---------+
   User --> :3000 (Next.js) --> :8000  (REST)
```

The API process never imports worker code. Communication is one-way through RQ. Workers import `marketmind_shared` (Pydantic schemas) but not `marketmind_api`. This keeps the deploy story simple: API and workers can scale independently.

## Why the strategy spec is the heart

Every strategy from every source — YouTube, blog, hand-typed — becomes a JSON document conforming to a single Pydantic-defined schema. Everything downstream operates on the spec:

- Backtesting reads the spec, never the source content.
- Reports describe the spec to the user.
- Comparison between strategies compares specs.

If the schema is well-designed, the rest of the system is mostly plumbing. If it's badly designed, everything is fragile.

Phase 1 is the schema. It will get its own design document at `docs/strategy-spec.md`.

## Phased plan

| Phase | What ships | Why this slice |
|---|---|---|
| **0 — Foundation** *(current)* | Monorepo, compose stack, dummy-job round trip, CI, tests | Prove the plumbing before adding domain complexity |
| **1 — Strategy spec** | Pydantic schema, JSON-Schema → TS pipeline, 10 hand-written fixtures | Lock the central data model before anything else |
| **2 — Ingestion + extraction** | yt-dlp + faster-whisper + trafilatura, Anthropic tool-use extraction, human-in-the-loop review | Get real specs into the system |
| **3 — Backtesting core** | ccxt → Parquet, spec → vectorbt translator, metrics + realistic costs | First answer to "did it work?" |
| **4 — Anti-overfitting** | Walk-forward, MC permutation, deflated Sharpe, synthetic data, composite score | The differentiator |
| **5 — Frontend + reports** | Submission flow, results dashboard, shareable report pages | What the user actually sees |
| **6 — Polish + deploy** | Rate limiting, cost tracking, admin dashboard, Hetzner deploy | Production-readiness |

Stop at the end of each phase. Don't roll work forward.

## Things this system intentionally does NOT do

- **Live trading.** Research tool only.
- **Authentication.** Single-user mode through Phase 5; auth added with deploy in Phase 6.
- **Real-time data.** Backtests run on historical OHLCV. Real-time is years away.
- **Mock data in backtesting unit tests.** Synthetic generators only.

## Decision records

See `docs/adr/` — short notes capturing why a non-obvious decision was made. New decisions add a numbered file; old ones don't get rewritten.
