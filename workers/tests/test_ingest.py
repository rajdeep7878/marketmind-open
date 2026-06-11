"""Unit tests for the ingest service.

Both yt-dlp and trafilatura are mocked entirely — these tests must NOT
hit the real network. The full pipeline is exercised by opt-in
integration tests in /tests (none yet for Phase 2.1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from marketmind_workers.services import ingest

# ---- is_youtube_url + video-id parsing ---------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", True),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", True),
        ("https://youtu.be/dQw4w9WgXcQ", True),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", True),
        ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", True),
        ("http://youtube.com/watch?v=x", True),  # hostname is what we test here
        ("https://example.com/watch?v=x", False),
        ("https://vimeo.com/123", False),
        ("not a url", False),
    ],
)
def test_is_youtube_url(url: str, expected: bool) -> None:
    assert ingest.is_youtube_url(url) is expected


@pytest.mark.parametrize(
    "url, expected_id",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12s", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://example.com/no-id", None),
    ],
)
def test_video_id_from_url(url: str, expected_id: str | None) -> None:
    assert ingest._video_id_from_url(url) == expected_id


# ---- ingest_raw_text --------------------------------------------------------


def test_ingest_raw_text_happy() -> None:
    rt = ingest.ingest_raw_text("hello world", label="manual entry")
    assert rt.text == "hello world"
    assert rt.label == "manual entry"


def test_ingest_raw_text_rejects_empty() -> None:
    with pytest.raises(ingest.ContentTooShortError):
        ingest.ingest_raw_text("   ")


# ---- ingest_youtube — error mapping -----------------------------------------


def test_ingest_youtube_rejects_non_youtube_url(tmp_path: Path) -> None:
    with pytest.raises(ingest.UnsupportedError):
        ingest.ingest_youtube("https://vimeo.com/123", data_dir=tmp_path)


@pytest.mark.parametrize(
    "yt_dlp_error_text, expected_cls",
    [
        # Bot-detection: cookies will fix this. Distinct from age-gate
        # (also worded "Sign in to confirm ...") which they won't.
        (
            "Sign in to confirm you're not a bot. This helps protect our community.",
            ingest.CookieError,
        ),
        (
            "ERROR: [youtube] dQw4w9WgXcQ: Use --cookies-from-browser or --cookies for the authentication.",
            ingest.CookieError,
        ),
        # Format-selector miss: permanent property of the source; the
        # broadened fallback chain helps, but a tail remains.
        (
            "ERROR: [youtube] xyz: Requested format is not available. Use --list-formats for a list of available formats",
            ingest.FormatUnavailableError,
        ),
        (
            "Requested format not available",
            ingest.FormatUnavailableError,
        ),
        # Age-gate: cookies don't bypass age verification — keep as
        # NetworkError so the cookie-rotation runbook isn't triggered
        # by something cookies can't actually fix.
        ("Sign in to confirm your age", ingest.NetworkError),
        ("This video is private", ingest.PrivateContentError),
        ("Video unavailable", ingest.NotFoundError),
        ("This video does not exist", ingest.NotFoundError),
        ("It's a playlist", ingest.UnsupportedError),
        ("This live event has not yet started", ingest.UnsupportedError),
    ],
)
def test_classify_ytdlp_error(yt_dlp_error_text: str, expected_cls: type[BaseException]) -> None:
    exc = ingest._classify_ytdlp_error(Exception(yt_dlp_error_text))
    assert isinstance(exc, expected_cls)


def test_format_unavailable_is_not_network_error() -> None:
    # FormatUnavailableError describes a permanent property of the
    # source — must NOT be a NetworkError subclass (callers retry
    # NetworkErrors; this should never be retried).
    assert not issubclass(ingest.FormatUnavailableError, ingest.NetworkError)
    assert issubclass(ingest.FormatUnavailableError, ingest.IngestError)


def test_ytdl_opts_format_includes_fallback_chain(tmp_path: Path) -> None:
    # The exact ordering matters for download-size and transcription
    # cost: prefer audio-only formats, fall back to merged AV only as
    # a last resort.
    opts = ingest._build_ytdl_opts(tmp_path / "out.m4a")
    assert opts["format"] == "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"


def test_cookie_error_subclasses_network_error() -> None:
    # API-side detection treats CookieError as a transient service issue;
    # the inheritance chain is what makes that mapping safe.
    assert issubclass(ingest.CookieError, ingest.NetworkError)
    assert issubclass(ingest.CookieError, ingest.IngestError)


# ---- cookie loading + yt-dlp opt threading ----------------------------------


def test_resolve_cookies_path_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("YOUTUBE_COOKIES_B64", raising=False)
    assert ingest._resolve_cookies_path() is None


def test_resolve_cookies_path_writes_file_when_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Redirect the cookies file to a temp location so the test doesn't
    # touch /tmp.
    target = tmp_path / "cookies.txt"
    monkeypatch.setattr(ingest, "_COOKIES_FILE", target)

    import base64

    cookie_contents = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSID\tabc\n"
    monkeypatch.setenv("YOUTUBE_COOKIES_B64", base64.b64encode(cookie_contents).decode())

    resolved = ingest._resolve_cookies_path()
    assert resolved == str(target)
    assert target.read_bytes() == cookie_contents


def test_resolve_cookies_path_returns_none_on_invalid_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_B64", "not-valid-base64!!!")
    assert ingest._resolve_cookies_path() is None


def test_build_ytdl_opts_omits_cookiefile_when_no_cookies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ingest, "COOKIES_PATH", None)
    opts = ingest._build_ytdl_opts(tmp_path / "out.m4a")
    assert "cookiefile" not in opts


def test_build_ytdl_opts_includes_cookiefile_when_path_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ingest, "COOKIES_PATH", "/tmp/youtube_cookies.txt")
    opts = ingest._build_ytdl_opts(tmp_path / "out.m4a")
    assert opts["cookiefile"] == "/tmp/youtube_cookies.txt"


def test_build_ytdl_opts_explicit_arg_overrides_module_constant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ingest, "COOKIES_PATH", "/tmp/module_default.txt")
    opts = ingest._build_ytdl_opts(tmp_path / "out.m4a", cookiefile="/tmp/explicit.txt")
    assert opts["cookiefile"] == "/tmp/explicit.txt"


# ---- ingest_youtube — happy path (mocked yt-dlp) ----------------------------


class _FakeYoutubeDL:
    """Test double for yt_dlp.YoutubeDL.

    Returns canned metadata on `extract_info(download=False)` and, on
    `download=True`, additionally writes a fake audio file to the
    download path so the caller's existence check passes.
    """

    def __init__(self, info: dict[str, Any], audio_path: Path | None) -> None:
        self._info = info
        self._audio_path = audio_path
        self.calls: list[dict[str, Any]] = []

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        self.calls.append({"url": url, "download": download})
        if download and self._audio_path is not None:
            self._audio_path.parent.mkdir(parents=True, exist_ok=True)
            self._audio_path.write_bytes(b"fake-audio-bytes")
        return self._info


def _install_fake_ytdl(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, Any],
    audio_path: Path | None,
) -> list[_FakeYoutubeDL]:
    instances: list[_FakeYoutubeDL] = []

    def factory(_outpath: Path) -> _FakeYoutubeDL:
        fdl = _FakeYoutubeDL(info=info, audio_path=audio_path)
        instances.append(fdl)
        return fdl

    monkeypatch.setattr(ingest, "_build_ytdl", factory)
    return instances


def test_ingest_youtube_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video_id = "dQw4w9WgXcQ"
    audio_path = tmp_path / "cache" / "audio" / f"{video_id}.m4a"
    info = {
        "id": video_id,
        "title": "Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "duration": 212,
        "upload_date": "20091025",
    }
    instances = _install_fake_ytdl(monkeypatch, info, audio_path)

    result = ingest.ingest_youtube(
        f"https://youtube.com/watch?v={video_id}",
        data_dir=tmp_path,
    )

    assert result.video_id == video_id
    assert result.title == "Never Gonna Give You Up"
    assert result.channel == "Rick Astley"
    assert result.duration_seconds == 212.0
    assert result.audio_path == audio_path
    assert result.uploaded_at is not None
    assert result.uploaded_at.year == 2009

    # First call was metadata-only, second was the actual download.
    flat_calls = [c["download"] for inst in instances for c in inst.calls]
    assert flat_calls == [False, True]

    # Sidecar persisted.
    sidecar = tmp_path / "cache" / "audio" / f"{video_id}.json"
    assert sidecar.exists()
    persisted = json.loads(sidecar.read_text())
    assert persisted["video_id"] == video_id
    # audio_path MUST NOT be in the sidecar: baking the writer's
    # absolute path here breaks cross-environment reads (host vs
    # container, two different dev machines, etc.). The path is
    # always recomputed at read time.
    assert "audio_path" not in persisted


def test_ingest_youtube_cache_hit_skips_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-populate the cache; the next call must NOT instantiate yt-dlp."""
    video_id = "abcdefghijk"
    cache_dir = tmp_path / "cache" / "audio"
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{video_id}.m4a").write_bytes(b"cached")
    # Modern sidecar shape — no audio_path field.
    sidecar = {
        "video_id": video_id,
        "title": "Cached",
        "channel": "Cached Channel",
        "duration_seconds": 60.0,
        "uploaded_at": None,
    }
    (cache_dir / f"{video_id}.json").write_text(json.dumps(sidecar))

    def _boom(_outpath: Path) -> None:
        raise AssertionError("yt-dlp should not be invoked on cache hit")

    monkeypatch.setattr(ingest, "_build_ytdl", _boom)

    result = ingest.ingest_youtube(f"https://youtu.be/{video_id}", data_dir=tmp_path)
    assert result.title == "Cached"
    # Audio path is recomputed at read time, not pulled from the sidecar.
    assert result.audio_path == cache_dir / f"{video_id}.m4a"


def test_ingest_youtube_cache_hit_ignores_legacy_audio_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sidecars written by older versions (pre-fix) bake a host-absolute
    audio_path that won't exist in another environment. The loader
    must ignore that field and use the caller-computed path instead.
    Regression guard for the smoke-test-3 bug.
    """
    video_id = "legacyxx_id"
    cache_dir = tmp_path / "cache" / "audio"
    cache_dir.mkdir(parents=True)
    real_audio = cache_dir / f"{video_id}.m4a"
    real_audio.write_bytes(b"cached")

    # Sidecar shaped like an old write: includes an audio_path that
    # points somewhere unreachable from the current process.
    legacy_sidecar = {
        "video_id": video_id,
        "title": "Legacy",
        "channel": "Legacy Channel",
        "duration_seconds": 42.0,
        "audio_path": "/nonexistent/cross/env/path/legacyxx_id.m4a",
        "uploaded_at": None,
    }
    (cache_dir / f"{video_id}.json").write_text(json.dumps(legacy_sidecar))

    def _boom(_outpath: Path) -> None:
        raise AssertionError("yt-dlp should not be invoked on cache hit")

    monkeypatch.setattr(ingest, "_build_ytdl", _boom)

    result = ingest.ingest_youtube(f"https://youtu.be/{video_id}", data_dir=tmp_path)
    assert result.title == "Legacy"
    # The bad path in the sidecar must not leak into the returned model.
    assert result.audio_path == real_audio
    assert "nonexistent" not in str(result.audio_path)


def test_ingest_youtube_rejects_long_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = {
        "id": "longvideoxxx",
        "title": "Marathon",
        "uploader": "Marathon",
        "duration": 4 * 3600 + 1,
        "upload_date": "20240101",
    }
    _install_fake_ytdl(monkeypatch, info, audio_path=None)

    with pytest.raises(ingest.ContentTooLongError):
        ingest.ingest_youtube("https://youtube.com/watch?v=longvideoxxx", data_dir=tmp_path)


def test_ingest_youtube_rejects_live_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # yt-dlp sets duration=None for ongoing live streams.
    info = {
        "id": "livestream0",
        "title": "Live",
        "uploader": "Live",
        "duration": None,
        "upload_date": None,
    }
    _install_fake_ytdl(monkeypatch, info, audio_path=None)

    with pytest.raises(ingest.UnsupportedError):
        ingest.ingest_youtube("https://youtube.com/watch?v=livestream0", data_dir=tmp_path)


def test_ingest_youtube_maps_private_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom_factory(_outpath: Path) -> _FakeYoutubeDL:
        class Boom:
            def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
                raise RuntimeError("This video is private")

        return Boom()  # type: ignore[return-value]

    monkeypatch.setattr(ingest, "_build_ytdl", boom_factory)

    with pytest.raises(ingest.PrivateContentError):
        ingest.ingest_youtube("https://youtube.com/watch?v=privatevidx", data_dir=tmp_path)


# ---- ingest_article ---------------------------------------------------------


def test_ingest_article_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body_text = "A" * 500

    def fake_fetch(_url: str) -> str:
        return "<html>doesn't matter, we mock extract too</html>"

    def fake_extract(_html: str, *, url: str) -> dict[str, Any]:
        return {
            "title": "Strategy 101",
            "author": "Jane Doe",
            "date": "2024-06-01",
            "text": body_text,
        }

    monkeypatch.setattr(ingest, "_trafilatura_fetch", fake_fetch)
    monkeypatch.setattr(ingest, "_trafilatura_extract", fake_extract)

    art = ingest.ingest_article("https://example.com/post", data_dir=tmp_path)
    assert art.title == "Strategy 101"
    assert art.author == "Jane Doe"
    assert art.text == body_text
    assert art.published_at is not None
    assert art.published_at.year == 2024
    assert art.published_at.tzinfo is not None

    # Cache persisted.
    cache_files = list((tmp_path / "cache" / "articles").glob("*.json"))
    assert len(cache_files) == 1


def test_ingest_article_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body_text = "B" * 400
    url = "https://example.com/cached"

    cache_dir = tmp_path / "cache" / "articles"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / f"{ingest._sha256_hex(url)}.json"
    cache_path.write_text(
        json.dumps(
            {
                "url": url,
                "title": "Cached",
                "author": None,
                "published_at": None,
                "text": body_text,
            },
        ),
    )

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("trafilatura should not be called on cache hit")

    monkeypatch.setattr(ingest, "_trafilatura_fetch", _boom)
    monkeypatch.setattr(ingest, "_trafilatura_extract", _boom)

    art = ingest.ingest_article(url, data_dir=tmp_path)
    assert art.title == "Cached"
    assert art.text == body_text


def test_ingest_article_rejects_too_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_trafilatura_fetch", lambda _url: "<html/>")
    monkeypatch.setattr(
        ingest,
        "_trafilatura_extract",
        lambda _html, *, url: {"title": "t", "text": "tiny"},
    )

    with pytest.raises(ingest.ContentTooShortError):
        ingest.ingest_article("https://example.com/short", data_dir=tmp_path)


def test_ingest_article_rejects_empty_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest, "_trafilatura_fetch", lambda _url: "<html/>")
    monkeypatch.setattr(ingest, "_trafilatura_extract", lambda _html, *, url: None)

    with pytest.raises(ingest.ContentTooShortError):
        ingest.ingest_article("https://example.com/empty", data_dir=tmp_path)


def test_ingest_article_maps_fetch_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch_returns_none(_url: str) -> None:
        return None

    monkeypatch.setattr(ingest, "_trafilatura_fetch", fetch_returns_none)

    with pytest.raises(ingest.NetworkError):
        ingest.ingest_article("https://example.com/down", data_dir=tmp_path)


def test_ingest_article_maps_fetch_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch_explodes(_url: str) -> None:
        raise ConnectionError("dns failure")

    monkeypatch.setattr(ingest, "_trafilatura_fetch", fetch_explodes)

    with pytest.raises(ingest.NetworkError):
        ingest.ingest_article("https://example.com/boom", data_dir=tmp_path)


def test_ingest_article_rejects_empty_url(tmp_path: Path) -> None:
    with pytest.raises(ingest.UnsupportedError):
        ingest.ingest_article("   ", data_dir=tmp_path)
