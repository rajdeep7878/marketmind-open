# ADR-0002: RQ over Celery for background jobs

**Date:** 2026-05-14
**Status:** Accepted

## Context

We need a job queue for ingestion (transcribe, scrape), extraction (LLM calls), and backtesting (potentially minutes long). Single-developer project, single-node deploy target (Hetzner VPS).

## Decision

[RQ](https://python-rq.org/) on Redis.

## Why

- **Cognitive load.** RQ has one concept (`Queue.enqueue(callable_ref)`) and one infrastructure dep (Redis, which we already need for caching). Celery has brokers, results backends, beat, flower, routing — most of which we'd never use.
- **Failure modes.** Celery's "task lost" failure modes are well-known; RQ's queues are dead simple to introspect with `redis-cli`.
- **Reasonable scale ceiling.** RQ handles thousands of jobs/min easily. We won't outgrow it inside the multi-month MVP window.
- **Type-checking story.** RQ jobs are plain Python functions. Celery's task decorator obscures signatures from pyright.

## Trade-offs we accept

- No native scheduled tasks. RQ has `rq-scheduler` if needed; we'll add it in Phase 3 when periodic market-data refreshes appear.
- No native task routing/priorities by header. We can split queues by name if/when needed.

## When we'd revisit

If we ever need multi-node workers with sophisticated routing, fan-out/fan-in, or first-class chains — i.e., behavior that's worse to roll ourselves on RQ than to adopt Celery's complexity. Unlikely.
