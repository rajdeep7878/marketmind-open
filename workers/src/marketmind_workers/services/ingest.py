"""Content ingestion: YouTube audio, article text, raw text.

Each function returns the matching `IngestedContent` variant. Low-level
exceptions from yt-dlp / trafilatura / network code are translated to
the `IngestError` tree below so the calling job can map errors to
sensible HTTP responses without poking at vendor exception types.

The on-disk cache layout (see README):

  ${DATA_DIR}/cache/audio/{video_id}.m4a       (yt-dlp output)
  ${DATA_DIR}/cache/audio/{video_id}.json      (metadata sidecar)
  ${DATA_DIR}/cache/articles/{sha256}.json     (trafilatura output)

Caches are conservative: hit only when BOTH expected files exist and
the sidecar JSON parses into a model. A partial download (audio file
present, metadata missing) re-downloads.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

import structlog
from marketmind_shared.schemas import (
    ArticleContent,
    RawTextContent,
    YouTubeContent,
)
from marketmind_shared.urls import is_youtube_url as _is_youtube_url

log = structlog.get_logger(__name__)


# ---- Error hierarchy --------------------------------------------------------


class IngestError(Exception):
    """Base for any failure raised by an ingest_* service function."""


class NotFoundError(IngestError):
    """Source resource does not exist (404, deleted video, etc.)."""


class PrivateContentError(IngestError):
    """Source exists but is gated (private video, login wall)."""


class UnsupportedError(IngestError):
    """URL is shaped like something we accept but isn't actually supported
    (e.g. a playlist URL passed to ingest_youtube).
    """


class NetworkError(IngestError):
    """Transient or permanent network failure during fetch."""


class CookieError(NetworkError):
    """yt-dlp could not authenticate without cookies (YouTube bot-detection
    or an explicit "Use --cookies" hint). Distinct from NetworkError so
    the API layer can return a 503 + friendly message instead of bubbling
    a Python traceback to the user. Cookies are rotated by re-encoding
    a fresh Chrome export into YOUTUBE_COOKIES_B64.
    """


class FormatUnavailableError(IngestError):
    """yt-dlp returned "Requested format is not available" — the video
    exists and we're authenticated, but none of the format selectors
    we asked for matched anything YouTube would serve to this session.

    Distinct from NetworkError because the failure is a permanent
    property of the source (or of how YouTube negotiates formats for
    the current cookie session) — retrying won't help; the user has
    to pick a different video.
    """


class ContentTooShortError(IngestError):
    """Extracted text/audio is below the minimum useful length."""


class ContentTooLongError(IngestError):
    """Source media exceeds the 4-hour processing ceiling."""


# ---- Config + helpers -------------------------------------------------------

# Single source of truth for the 4-hour ceiling. Mirrors the validator
# bound on YouTubeContent.duration_seconds in the shared schema, so a
# raised ContentTooLongError here matches the schema's hard limit.
MAX_MEDIA_SECONDS: Final[float] = 4 * 3600.0
MIN_ARTICLE_CHARS: Final[int] = 200


# YouTube hostname matching lives in `marketmind_shared.urls` so the
# API can apply the same rule for kind-detection. We re-export it here
# to keep this module's public surface unchanged.
is_youtube_url = _is_youtube_url

_YOUTUBE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Path the cookies blob is materialised to on worker boot. Inside the
# Railway container `/tmp` is writable for the non-root `app` user and
# is scoped to the container's lifetime — exactly what we want for a
# secret that should be ephemeral.
_COOKIES_FILE: Final[Path] = Path("/tmp/youtube_cookies.txt")  # noqa: S108

# Patterns we can confidently parse a video_id from without yt-dlp.
# Order matters: youtu.be path-id before youtube.com query-id, otherwise
# youtu.be/<id>?si=... would fall through to the query-string branch.
_VIDEO_ID_FROM_URL_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})(?:[?&/].*)?$"),
    re.compile(r"youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|shorts/|v/)([A-Za-z0-9_-]{11})"),
)


def _video_id_from_url(url: str) -> str | None:
    """Pull the 11-char video id straight out of the URL.

    Returns None if the URL doesn't match any known YouTube pattern;
    callers fall back to yt-dlp's own resolver in that case.
    """
    for pattern in _VIDEO_ID_FROM_URL_RES:
        m = pattern.search(url)
        if m and _YOUTUBE_ID_RE.match(m.group(1)):
            return m.group(1)
    return None


def _cache_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / "cache"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- YouTube ----------------------------------------------------------------


class _YoutubeDownloader(Protocol):
    """Minimal subset of yt_dlp.YoutubeDL we depend on.

    Defined as a Protocol so tests can mock it without touching yt-dlp.
    """

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _YoutubeMetadata:
    """Plain-data container for cached YouTube metadata.

    Persisted as JSON; loaded with strict checks. Not a Pydantic model
    because the schema is private to this module — YouTubeContent is the
    public contract.

    Deliberately does NOT store the audio file's absolute path. That
    path is reconstructed at read time from `{data_dir}/cache/audio/
    {video_id}.m4a`, which is always correct for the current process
    regardless of which environment first populated the cache. The
    previous design baked the writer's host-absolute path into the
    sidecar and broke as soon as a worker container tried to read a
    cache populated by a host-side ingestion.
    """

    video_id: str
    title: str
    channel: str
    duration_seconds: float
    uploaded_at: str | None  # ISO-format, UTC

    def to_youtube_content(self, audio_path: Path) -> YouTubeContent:
        uploaded_at = datetime.fromisoformat(self.uploaded_at) if self.uploaded_at else None
        return YouTubeContent(
            video_id=self.video_id,
            title=self.title,
            channel=self.channel,
            duration_seconds=self.duration_seconds,
            audio_path=audio_path,
            uploaded_at=uploaded_at,
        )


def _resolve_cookies_path() -> str | None:
    """Materialise YOUTUBE_COOKIES_B64 to a file and return its path.

    Reads the env var directly (rather than going through WorkerSettings)
    so this function can run at module import without forcing a full
    Settings construction — useful for test environments that import the
    services module without setting REDIS_URL etc.

    Returns the file path as a string, or None if the env var is empty
    or the value can't be decoded / written. Decode/write failures are
    logged at ERROR but never raised: yt-dlp will simply run without
    cookies and the regular classification path will handle the
    eventual "not a bot" rejection.
    """
    raw = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not raw:
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        log.error("youtube_cookies_b64_decode_failed", error=str(exc))
        return None
    try:
        _COOKIES_FILE.write_bytes(decoded)
        # 0600 — cookies are credentials; default umask is fine in the
        # container but explicit is better.
        _COOKIES_FILE.chmod(0o600)
    except OSError as exc:
        log.error(
            "youtube_cookies_b64_write_failed",
            error=str(exc),
            path=str(_COOKIES_FILE),
        )
        return None
    log.info("youtube_cookies_loaded", path=str(_COOKIES_FILE), bytes=len(decoded))
    return str(_COOKIES_FILE)


# Evaluated once at module import. Tests that need a different state
# call `_resolve_cookies_path()` directly with monkey-patched env, or
# overwrite this constant via monkeypatch.setattr.
COOKIES_PATH: str | None = _resolve_cookies_path()


def _build_ytdl(audio_outpath: Path, *, cookiefile: str | None = None) -> _YoutubeDownloader:
    """Build a yt_dlp.YoutubeDL configured to download m4a-only audio.

    Pulled into its own function so tests can monkeypatch this entire
    builder without going through yt-dlp's option-parsing surface area.

    Cast to our _YoutubeDownloader Protocol because yt-dlp's real
    extract_info returns `_InfoDict` (a private subclass of dict) and
    accepts more kwargs than we use — Protocol gives us the small,
    stable surface to mock against.

    ``cookiefile`` defaults to ``COOKIES_PATH`` at call time (not at
    definition time) so a test that monkeypatches ``COOKIES_PATH``
    after import still affects subsequent yt-dlp invocations.
    """
    # Import inside the function to keep tests from needing yt-dlp on the path.
    from yt_dlp import YoutubeDL  # type: ignore[import-untyped]

    opts = _build_ytdl_opts(audio_outpath, cookiefile=cookiefile)
    return cast("_YoutubeDownloader", YoutubeDL(opts))  # type: ignore[arg-type]


def _build_ytdl_opts(audio_outpath: Path, *, cookiefile: str | None = None) -> dict[str, Any]:
    """Build the options dict ``_build_ytdl`` passes to YoutubeDL.

    Pulled out as a separate function so tests can assert on the dict
    without intercepting the YoutubeDL constructor. ``_build_ytdl`` is
    a thin wrapper around it. Always returns a fresh dict.
    """
    effective_cookies = cookiefile if cookiefile is not None else COOKIES_PATH
    # Format fallback chain. Production hit "Requested format is not
    # available" with the old `bestaudio[ext=m4a]/bestaudio` selector
    # for some videos in the cookie-auth path. The chain now:
    #   1. m4a audio-only — preferred; ffmpeg passes it through to
    #      whisper without re-encoding.
    #   2. webm audio-only — second-most-common YouTube format;
    #      whisper accepts it.
    #   3. any audio-only — last audio-only attempt; whatever yt-dlp
    #      can find.
    #   4. best — final fallback; downloads the merged AV stream.
    #      Larger payload, but we strip the video downstream during
    #      transcription, so we still get a usable result. Better
    #      than refusing the source.
    opts: dict[str, Any] = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": str(audio_outpath),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # We don't want yt-dlp creating sibling .info.json files; we
        # write our own pruned sidecar so the cache stays small.
        "writeinfojson": False,
        "writethumbnail": False,
    }
    if effective_cookies:
        opts["cookiefile"] = effective_cookies
    return opts


def _coerce_upload_date(value: object) -> str | None:
    """Convert yt-dlp's `upload_date` (YYYYMMDD string) to a UTC ISO ts.

    yt-dlp gives a date with no tz; treat it as midnight UTC. Returning
    None for missing/malformed values is fine — uploaded_at is optional
    on YouTubeContent.
    """
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    try:
        dt = datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)  # tz set after parse
    except ValueError:
        return None
    return dt.isoformat()


def _load_youtube_cache(audio_path: Path, sidecar_path: Path) -> YouTubeContent | None:
    """Return cached content if both files are present and parse cleanly.

    `audio_path` is computed by the caller from `cache_dir / f"{video_id}.m4a"`
    and is the authoritative location for the audio in the current
    process — we never re-read it from the sidecar. Legacy sidecars
    that include an `audio_path` field are tolerated (the key is
    stripped before constructing _YoutubeMetadata) so we don't have to
    migrate existing cache directories.
    """
    if not (audio_path.exists() and sidecar_path.exists()):
        return None
    try:
        raw = json.loads(sidecar_path.read_text())
        # Drop any legacy audio_path key. Modern sidecars don't write it;
        # older ones (pre-Phase-2.2-cache-fix) baked the writer's
        # host-absolute path here and broke cross-environment reads.
        raw.pop("audio_path", None)
        meta = _YoutubeMetadata(**raw)
        return meta.to_youtube_content(audio_path)
    except (OSError, ValueError, TypeError):
        # Corrupt sidecar - treat as cache miss; caller will overwrite.
        return None


def _classify_ytdlp_error(exc: BaseException) -> IngestError:
    """Map yt-dlp's DownloadError to our IngestError tree by inspecting
    the message text. yt-dlp does not expose typed exception subclasses
    for these cases.

    Cookie-related rejections are flagged first so the bot-detection
    case (which yt-dlp also describes as "Sign in to confirm...") gets
    classified before falling into other branches. Matched on the
    narrow phrases "not a bot" and "use --cookies"; the broader
    "sign in to confirm" also catches the age-gate path which cookies
    don't fix.
    """
    msg = str(exc).lower()
    if "not a bot" in msg or "use --cookies" in msg or "use cookies to authenticate" in msg:
        log.error("ytdlp_cookie_auth_required", raw_error=str(exc))
        return CookieError(str(exc))
    if "requested format is not available" in msg or "requested format not available" in msg:
        # Even with the broadened format chain there's still a tail of
        # videos that serve nothing matching our selectors — log so we
        # can monitor whether the residual rate is small enough to
        # leave alone or worth another format pass.
        log.error("ytdlp_format_unavailable", raw_error=str(exc))
        return FormatUnavailableError(str(exc))
    if "private" in msg or "login required" in msg or "members-only" in msg:
        return PrivateContentError(str(exc))
    if "unavailable" in msg or "removed" in msg or "does not exist" in msg or "404" in msg:
        return NotFoundError(str(exc))
    if (
        "playlist" in msg
        or "live event" in msg
        or "this live event" in msg
        or "is a live stream" in msg
    ):
        return UnsupportedError(str(exc))
    return NetworkError(str(exc))


def ingest_youtube(url: str, *, data_dir: str | Path = "/data") -> YouTubeContent:
    """Download (or load from cache) a YouTube video's audio + metadata.

    Returns a fully populated YouTubeContent. The audio file lives at
    `${data_dir}/cache/audio/{video_id}.m4a`.

    Raises one of the IngestError subclasses on failure.
    """
    url = url.strip()
    if not is_youtube_url(url):
        raise UnsupportedError(f"not a supported YouTube URL: {url!r}")

    cache_dir = _cache_root(data_dir) / "audio"
    _ensure_dir(cache_dir)

    # Try a cache hit first using the video_id parsed directly from the
    # URL — saves a metadata round-trip to YouTube. If the URL doesn't
    # match a known shape (or the cache misses), we fall back to yt-dlp
    # for both metadata and download.
    url_video_id = _video_id_from_url(url)
    if url_video_id is not None:
        cached = _load_youtube_cache(
            cache_dir / f"{url_video_id}.m4a",
            cache_dir / f"{url_video_id}.json",
        )
        if cached is not None:
            log.info("youtube_cache_hit", video_id=url_video_id, source="url_match")
            return cached

    # First pass: extract metadata only, so we know the video_id and
    # can decide whether to skip the download entirely.
    ytdl_meta = _build_ytdl(cache_dir / "%(id)s.%(ext)s")
    try:
        info = ytdl_meta.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises broad types
        raise _classify_ytdlp_error(exc) from exc

    video_id = info.get("id")
    if not isinstance(video_id, str) or not video_id:
        raise UnsupportedError(f"yt-dlp returned no video id for {url!r}")

    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        # Live streams, partial uploads, premieres before air-time.
        raise UnsupportedError(f"video {video_id!r} has no duration (live or premiere?)")
    if duration > MAX_MEDIA_SECONDS:
        raise ContentTooLongError(
            f"video {video_id!r} duration {duration:.0f}s exceeds {MAX_MEDIA_SECONDS:.0f}s ceiling",
        )

    audio_path = cache_dir / f"{video_id}.m4a"
    sidecar_path = cache_dir / f"{video_id}.json"

    cached = _load_youtube_cache(audio_path, sidecar_path)
    if cached is not None:
        log.info("youtube_cache_hit", video_id=video_id)
        return cached

    log.info("youtube_download_starting", video_id=video_id, url=url)
    ytdl_download = _build_ytdl(audio_path)
    try:
        ytdl_download.extract_info(url, download=True)
    except Exception as exc:
        raise _classify_ytdlp_error(exc) from exc

    if not audio_path.exists():
        # yt-dlp may pick a different extension if m4a isn't available.
        # Fall back to the first matching file in the cache dir.
        candidates = sorted(cache_dir.glob(f"{video_id}.*"))
        candidates = [c for c in candidates if c.suffix != ".json"]
        if not candidates:
            raise NetworkError(
                f"yt-dlp claimed to download {video_id!r} but no audio file appeared",
            )
        audio_path = candidates[0]

    title = info.get("title") or video_id
    channel = info.get("uploader") or info.get("channel") or "unknown"
    uploaded_at = _coerce_upload_date(info.get("upload_date"))

    meta = _YoutubeMetadata(
        video_id=video_id,
        title=str(title),
        channel=str(channel),
        duration_seconds=float(duration),
        uploaded_at=uploaded_at,
    )
    sidecar_path.write_text(json.dumps(meta.__dict__, indent=2, sort_keys=True))
    log.info("youtube_download_complete", video_id=video_id, audio_path=str(audio_path))
    return meta.to_youtube_content(audio_path)


# ---- Article ---------------------------------------------------------------


def _trafilatura_fetch(url: str) -> str | None:
    """Fetch URL and return the raw HTML, or None on failure.

    Wrapped in a function so tests can monkeypatch it independently of
    `extract`. Errors map to NetworkError at the call site.
    """
    import trafilatura  # type: ignore[import-untyped]

    return trafilatura.fetch_url(url)  # type: ignore[no-any-return]


def _trafilatura_extract(html: str, url: str) -> dict[str, Any] | None:
    import trafilatura  # type: ignore[import-untyped]

    # `output_format="json"` returns the extracted text plus a metadata
    # block (title, author, date) in a single payload. `with_metadata`
    # = True ensures we get those even if defaults change.
    out = trafilatura.extract(
        html,
        url=url,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=False,
    )
    if not out:
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_published_at(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # trafilatura emits ISO-8601-ish strings, but they may be just dates
    # ("2024-05-10") or include a tz. Always normalize to UTC ISO.
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # Date-only ("2024-05-10") — treat as midnight UTC.
        try:
            dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)  # tz set after parse
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def ingest_article(url: str, *, data_dir: str | Path = "/data") -> ArticleContent:
    """Fetch and extract the body text of an article URL.

    Cache key is sha256(url). Cache hits return immediately without a
    network call. Articles shorter than MIN_ARTICLE_CHARS are rejected
    (almost always a paywall or a failed extraction).
    """
    url = url.strip()
    if not url:
        raise UnsupportedError("empty URL")

    cache_dir = _cache_root(data_dir) / "articles"
    _ensure_dir(cache_dir)
    cache_path = cache_dir / f"{_sha256_hex(url)}.json"

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            published_at = cached.get("published_at")
            return ArticleContent(
                url=cached["url"],
                title=cached["title"],
                author=cached.get("author"),
                published_at=(datetime.fromisoformat(published_at) if published_at else None),
                text=cached["text"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            # Corrupt cache; fall through and re-fetch.
            log.warning("article_cache_corrupt", url=url, path=str(cache_path))

    try:
        html = _trafilatura_fetch(url)
    except Exception as exc:
        raise NetworkError(f"failed to fetch {url!r}: {exc}") from exc
    if html is None:
        raise NetworkError(f"failed to fetch {url!r} (no HTML returned)")

    extracted = _trafilatura_extract(html, url=url)
    if extracted is None or not extracted.get("text"):
        raise ContentTooShortError(f"trafilatura extracted nothing from {url!r}")

    text = str(extracted.get("text", "")).strip()
    if len(text) < MIN_ARTICLE_CHARS:
        raise ContentTooShortError(
            f"extracted text from {url!r} is {len(text)} chars "
            f"(< {MIN_ARTICLE_CHARS}); likely a paywall or failed extraction",
        )

    title = str(extracted.get("title") or "untitled")
    author = extracted.get("author")
    author = str(author) if isinstance(author, str) and author else None
    published_iso = _coerce_published_at(extracted.get("date"))

    payload = {
        "url": url,
        "title": title,
        "author": author,
        "published_at": published_iso,
        "text": text,
    }
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    return ArticleContent(
        url=url,
        title=title,
        author=author,
        published_at=datetime.fromisoformat(published_iso) if published_iso else None,
        text=text,
    )


# ---- Raw text ---------------------------------------------------------------


def ingest_raw_text(text: str, label: str | None = None) -> RawTextContent:
    """Trivial wrapper around the schema. Validates non-empty text."""
    text = text.strip()
    if not text:
        raise ContentTooShortError("raw text is empty")
    return RawTextContent(text=text, label=label)


__all__ = [
    "COOKIES_PATH",
    "MAX_MEDIA_SECONDS",
    "MIN_ARTICLE_CHARS",
    "ContentTooLongError",
    "ContentTooShortError",
    "CookieError",
    "FormatUnavailableError",
    "IngestError",
    "NetworkError",
    "NotFoundError",
    "PrivateContentError",
    "UnsupportedError",
    "ingest_article",
    "ingest_raw_text",
    "ingest_youtube",
    "is_youtube_url",
]
