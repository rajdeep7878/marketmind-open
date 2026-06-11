# ADR-0003: Ruff (lint + format) and pyright over the original Black + mypy plan

**Date:** 2026-05-14
**Status:** Accepted

## Context

The project brief specified `ruff + black + mypy`. Two of those choices have aged out.

## Decision

- **Ruff for both lint AND format.** Drop Black entirely.
- **Pyright in strict mode** for type checking. Drop mypy.

## Why

**Ruff format vs Black:** Ruff's formatter is a Black-compatible drop-in (same style, same output on 99.9% of inputs), implemented in Rust, ~30× faster. Running both means Ruff fixes a file and then Black reformats it — pure churn. No reason to keep both.

**Pyright vs mypy:**
- Pyright is much faster (often 10× on cold runs; 50×+ incrementally via watch mode).
- Pyright handles Pydantic v2 generics, FastAPI's `Depends[Annotated[...]]` patterns, and async correctness without the dance of `mypy --plugin` configurations.
- Pyright is what VS Code's Python extension (Pylance) uses, so the editor and CI agree on what's an error.
- Mypy's error messages are slightly nicer in places; not worth the speed and accuracy trade.

## Trade-offs

- Pyright is written in TypeScript and bundled with a Node binary. Installed via `pip install pyright`, which transparently fetches the Node runtime. One extra dependency in CI; trivial.
- Ruff's formatter is configured via `[tool.ruff.format]` rather than `[tool.black]`; minor surface change.

## Layout

- `pyproject.toml` (root): single `[tool.ruff]` block, single `[tool.pyright]` block. Settings inherit across the workspace.
- `pre-commit`: runs Ruff on every commit, defers Pyright to CI (too slow for hooks).
- CI: runs `ruff check`, `ruff format --check`, `pyright`, `pytest`.
