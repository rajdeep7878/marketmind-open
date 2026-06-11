"""URL classification helpers shared across services.

Both the API (route-level dispatch) and the workers (ingest service)
need to recognize YouTube URLs. Defining the regex once here keeps
those decisions in lockstep.
"""

from __future__ import annotations

import re
from typing import Final

_YOUTUBE_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:https?://)?"
    r"(?:www\.|m\.)?"
    r"(?:youtube\.com|youtu\.be|youtube-nocookie\.com)"
    r"(?:/|$)",
    re.IGNORECASE,
)


def is_youtube_url(url: str) -> bool:
    """Return True if `url` matches one of the supported YouTube hostnames."""
    return bool(_YOUTUBE_HOST_RE.match(url.strip()))


__all__ = ["is_youtube_url"]
