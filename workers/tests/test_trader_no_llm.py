"""Permanent invariant: no file under `marketmind_workers.trader.*` imports anthropic.

The trader's decision path runs in production with `ANTHROPIC_API_KEY`
unset (and the docker-compose `trader_worker` service deliberately
omits the var). A future commit that accidentally imports anthropic
in any trader module would either crash at runtime in production OR,
worse, silently introduce a non-deterministic dependency into the
strategy-evaluation path.

This test runs at unit-test speed and gates that invariant at PR time.

It mirrors `test_jobs.py`'s string-in-source check on
`marketmind_workers.services.llm`, but iterates every `.py` file
under the trader subtree.

Why string-search and not AST: importing the modules themselves
would trigger their top-level code (DB connections, queue lookups).
Source scanning is hermetic and catches both `import anthropic` and
`from anthropic import ...` variants.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

# Resolve the trader source dir relative to this test file. parents[2]
# walks up workers/tests → workers → repo-root, then down to the
# package. The runner uses an `importlib.resources`-based fallback
# for wheel installs; tests run editable, so the file path resolves.
_TRADER_DIR: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "marketmind_workers"
    / "trader"
)


# Patterns to ban. Tolerates whitespace and a trailing import target.
# We match at line starts (after optional leading whitespace) to skip
# the case where the literal "anthropic" appears in a comment or
# docstring discussing the policy itself.
_BANNED_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*import\s+anthropic\b", re.MULTILINE),
    re.compile(r"^\s*from\s+anthropic(\.|\s+import\s)", re.MULTILINE),
)


def _trader_py_files() -> list[Path]:
    """All non-__pycache__ .py files in the trader package."""
    return sorted(p for p in _TRADER_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_trader_dir_exists() -> None:
    """Sanity gate: the file-system path resolves. If this fails,
    the test as a whole is silently doing nothing.
    """
    assert _TRADER_DIR.is_dir(), f"trader dir not found: {_TRADER_DIR}"


def test_trader_dir_has_python_files() -> None:
    """A second sanity gate: at least one .py under the dir. If the
    package is empty / moved, this fails loudly instead of silently
    accepting an empty scan.
    """
    files = _trader_py_files()
    assert files, f"no .py files under {_TRADER_DIR}"
    # At minimum the runner, jobs, config, heartbeat modules should
    # be present — flagging if the layout changes without this test
    # being touched.
    names = {f.name for f in files}
    assert {"runner.py", "jobs.py", "config.py", "heartbeat.py"} <= names


def test_no_trader_file_imports_anthropic() -> None:
    """The load-bearing assertion.

    For each `.py` file under `marketmind_workers/trader/`, assert
    that no line matches `import anthropic` or `from anthropic ...`.
    Comments mentioning "anthropic" (e.g. docstrings explaining the
    policy) are allowed because the regex requires `import` / `from`
    at the line start.
    """
    offenders: list[str] = []
    for path in _trader_py_files():
        source = path.read_text(encoding="utf-8")
        for pattern in _BANNED_PATTERNS:
            if pattern.search(source):
                offenders.append(f"{path.relative_to(_TRADER_DIR.parents[1])}")
                break
    assert not offenders, (
        "trader code must not import anthropic (the v1 decision path "
        "is LLM-free and the worker process runs with "
        "ANTHROPIC_API_KEY unset). Offending files:\n  "
        + "\n  ".join(offenders)
    )
