"""ExecutionMode — an enum with exactly ONE member.

PAPER ONLY. This system has no live execution path by design. There is no
environment variable, config flag, or code branch that introduces another
mode; ``test_ftr_no_live_env_escape`` scans FTR config and env parsing to
prove it, and ``test_ftr_paper_only_no_private_api`` proves no API-key
plumbing exists in FTR modules.
"""

from __future__ import annotations

from enum import Enum, unique


@unique
class ExecutionMode(Enum):
    PAPER = "paper"


EXECUTION_MODE: ExecutionMode = ExecutionMode.PAPER


class PaperOnlyViolation(RuntimeError):  # noqa: N818 — name mandated by the FTR spec
    """Raised when anything attempts a non-public/authenticated operation."""
