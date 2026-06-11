# ADR-0001: Monorepo with a uv workspace

**Date:** 2026-05-14
**Status:** Accepted

## Context

Three Python services (`api`, `workers`, `shared`) plus a Node frontend (`web`). The Python services need to share a Pydantic schema package without copy-paste or path-dependency awkwardness.

## Decision

Single repo. Python services managed as a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) at the repo root. Each service has its own `pyproject.toml`; a single `uv.lock` at the root resolves them all together. `shared/` is consumed by `api/` and `workers/` via `[tool.uv.sources] marketmind-shared = { workspace = true }`, which is an editable install.

The frontend (`web/`) is a standalone pnpm package — no need to entangle it with Python tooling.

## Consequences

- Editing `shared/` reflects in both services immediately. No build step, no reinstall.
- `uv sync` from the root sets up everything in one venv at `.venv/`.
- Docker builds copy each service's manifest into the image for layer-cache friendliness; only the relevant workspace package is installed per image.
- We commit `uv.lock` for reproducibility.

## Alternatives considered

- **Poetry path deps.** Works, but `uv` is dramatically faster and the workspace concept is first-class.
- **Separate repos per service.** Premature for a solo project; the schema contract is best maintained in lockstep with its consumers.
- **PEP 420 namespace packages.** Avoids the manifest-per-service overhead but loses metadata (deps, scripts) — not worth it.
