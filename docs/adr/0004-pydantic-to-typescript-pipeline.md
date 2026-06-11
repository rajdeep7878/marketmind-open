# ADR-0004: Pydantic → JSON Schema → TypeScript for cross-service types

**Date:** 2026-05-14
**Status:** Accepted

## Context

The strategy spec (Phase 1) is the central data model. We need the API contract to be enforced on both sides of the wire: Pydantic on the Python side, strict TypeScript on the frontend. Both must derive from a single source of truth, with no manual sync.

## Decision

One-way pipeline: **Pydantic → JSON Schema → TypeScript**.

1. `shared/scripts/export_json_schema.py` invokes `Model.model_json_schema()` on every exported Pydantic model and bundles them into `web/src/types/generated/schemas.json`.
2. The frontend runs `json-schema-to-typescript` against that file to produce `schemas.ts`.
3. Both outputs live in `web/src/types/generated/` (gitignored — regenerated on build).

Pydantic is the canonical source. JSON Schema is the transport format. TS is a derived artifact.

## Why this direction

- Pydantic v2's JSON Schema export is well-supported and uses the same draft (2020-12) that `json-schema-to-typescript` consumes natively.
- Going the other way (TS → Python) has no good tooling.
- Going Python → TS directly via something like `pydantic2ts` works but couples us to a fragile project; the two-step path via standard JSON Schema is more robust.
- JSON Schema is also useful for Anthropic tool-use schema enforcement in Phase 2 — same artifact, two consumers.

## Workflow

- Developer changes a Pydantic model in `shared/`.
- Runs `uv run python shared/scripts/export_json_schema.py` (or it's invoked by a make target / CI).
- Runs `cd web && pnpm gen:types`.
- Frontend gets new types; build fails if a consumer wasn't updated.

In Phase 1 we'll add a CI check that runs the export and fails if the resulting JSON differs from what was committed — pinning the generator output to the committed shared models.

## Trade-offs

- The frontend can't import enums directly; they come through as string-union types. Acceptable.
- We must rerun the generator after every schema change. A pre-commit hook can enforce this if it becomes a footgun.
