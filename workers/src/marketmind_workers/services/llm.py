"""Anthropic SDK stub.

Phase 2.1 leaves this deliberately inert. The SDK is installed (see
workers/pyproject.toml) and the API key is read from env so phase-2.2
can fail loudly if it's missing, but no calls are made anywhere in the
2.1 codebase. The extraction prompt design is done with the user in
the loop and lives in Phase 2.2.

The single helper `get_anthropic_api_key()` is here so other modules
can reference it without importing pydantic-settings directly; the
extract_strategy job uses it to fail-fast when called prematurely.
"""

from __future__ import annotations

import os
from typing import Final

# Module name kept exposed even though we don't import the SDK here, to
# make `python -c "from marketmind_workers.services import llm"`
# unambiguous about whether the SDK is wired in.
ANTHROPIC_SDK_NAME: Final[str] = "anthropic"


def get_anthropic_api_key() -> str:
    """Return the ANTHROPIC_API_KEY from environment, or "" if unset.

    NOT validated here — Phase 2.2's extract_strategy job is responsible
    for raising a clear error before invoking the SDK. This helper just
    centralizes the env-lookup so other modules don't sprinkle os.environ.
    """
    return os.environ.get("ANTHROPIC_API_KEY", "")


__all__ = ["ANTHROPIC_SDK_NAME", "get_anthropic_api_key"]
