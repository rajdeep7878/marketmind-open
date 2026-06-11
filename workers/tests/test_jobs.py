"""Unit tests for the Phase 2.1 RQ job callables.

Each job is glue: it takes args, calls a service, persists, and
returns a small dict. Tests stub the service + persistence layer so
the jobs are exercised without touching network or DB.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from marketmind_shared.schemas import (
    ArticleContent,
    RawTextContent,
    Transcript,
    TranscriptSegment,
    YouTubeContent,
)
from marketmind_workers.jobs import (
    ingest_article,
    ingest_raw_text,
    ingest_youtube,
    transcribe,
)

# ---- ingest_youtube job ----------------------------------------------------


def test_ingest_youtube_job(monkeypatch: pytest.MonkeyPatch) -> None:
    yt = YouTubeContent(
        video_id="abcdefghijk",
        title="Title",
        channel="Channel",
        duration_seconds=120.0,
        audio_path=Path("/data/x.m4a"),
    )

    monkeypatch.setattr(
        ingest_youtube,
        "ingest_youtube",
        lambda url, data_dir: yt,
    )

    saved: list[YouTubeContent] = []

    def fake_save(_url: str, content: YouTubeContent) -> UUID:
        saved.append(content)
        return UUID("11111111-1111-1111-1111-111111111111")

    monkeypatch.setattr(ingest_youtube, "save_content", fake_save)

    result = ingest_youtube.run(url="https://youtu.be/abcdefghijk")
    assert result["video_id"] == "abcdefghijk"
    assert result["content_id"] == "11111111-1111-1111-1111-111111111111"
    assert result["duration_seconds"] == 120.0
    assert saved == [yt]


# ---- ingest_article job ----------------------------------------------------


def test_ingest_article_job(monkeypatch: pytest.MonkeyPatch) -> None:
    art = ArticleContent(
        url="https://example.com",
        title="Title",
        text="A" * 500,
    )
    monkeypatch.setattr(
        ingest_article,
        "ingest_article",
        lambda url, data_dir: art,
    )

    def fake_save(_url: str, _content: ArticleContent) -> UUID:
        return UUID("22222222-2222-2222-2222-222222222222")

    monkeypatch.setattr(ingest_article, "save_content", fake_save)
    monkeypatch.setattr(
        ingest_article,
        "save_transcript",
        lambda _url, _cid, _tr: UUID("33333333-3333-3333-3333-333333333333"),
    )

    result = ingest_article.run(url="https://example.com")
    assert result["url"] == "https://example.com"
    assert result["chars"] == 500
    assert result["content_id"] == "22222222-2222-2222-2222-222222222222"


def test_ingest_article_job_writes_synthetic_transcript_for_extract_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the article→extract pipeline gap.

    The extract API route (POST /content/{id}/extract) refuses with
    404 unless a transcripts row exists for the content. Articles
    have no audio to transcribe, so the article ingest job must
    materialise a synthetic transcript at ingest time. Without this
    the article never becomes extractable.

    Asserts: `save_transcript` is invoked with `model_name="trafilatura"`,
    `full_text` equal to the article body, and an empty segments list.
    """
    art = ArticleContent(
        url="https://example.com/post",
        title="Post Title",
        text="The article body text " * 30,
    )
    monkeypatch.setattr(
        ingest_article,
        "ingest_article",
        lambda url, data_dir: art,
    )
    monkeypatch.setattr(
        ingest_article,
        "save_content",
        lambda _url, _content: UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    )

    saved_transcripts: list[Transcript] = []
    saved_content_ids: list[UUID] = []

    def capture_save_transcript(_url: str, content_id: UUID, transcript: Transcript) -> UUID:
        saved_content_ids.append(content_id)
        saved_transcripts.append(transcript)
        return UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    monkeypatch.setattr(ingest_article, "save_transcript", capture_save_transcript)

    result = ingest_article.run(url="https://example.com/post")
    assert result["content_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    # The synthetic transcript was persisted, content_id matches.
    assert saved_content_ids == [UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")]
    assert len(saved_transcripts) == 1
    tr = saved_transcripts[0]
    assert tr.model_name == "trafilatura"
    assert tr.full_text == art.text
    assert tr.segments == []
    assert tr.language == "en"
    # duration_seconds > 0 is a Pydantic constraint; the value itself
    # is a synthetic sentinel — assert it's positive and let the actual
    # number be an implementation detail.
    assert tr.duration_seconds > 0.0


def test_ingest_raw_text_job_writes_synthetic_transcript_for_extract_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression as the article path: raw-text submissions also
    need a synthetic transcript row so the extract API doesn't 404.
    Provenance marker: `model_name="raw_text"`.
    """
    rt = RawTextContent(text="strategy: buy if RSI < 30" * 20, label="manual paste")
    monkeypatch.setattr(
        ingest_raw_text,
        "ingest_raw_text",
        lambda text, label: rt,
    )
    monkeypatch.setattr(
        ingest_raw_text,
        "save_content",
        lambda _url, _content: UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
    )

    saved_transcripts: list[Transcript] = []

    def capture_save_transcript(_url: str, _cid: UUID, transcript: Transcript) -> UUID:
        saved_transcripts.append(transcript)
        return UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

    monkeypatch.setattr(ingest_raw_text, "save_transcript", capture_save_transcript)

    result = ingest_raw_text.run(text="ignored — service mock controls payload")
    assert result["content_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert len(saved_transcripts) == 1
    tr = saved_transcripts[0]
    assert tr.model_name == "raw_text"
    assert tr.full_text == rt.text
    assert tr.segments == []
    assert tr.language == "en"
    assert tr.duration_seconds > 0.0


# ---- transcribe job --------------------------------------------------------


def _make_transcript() -> Transcript:
    return Transcript(
        language="en",
        full_text="hello",
        segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="hello")],
        duration_seconds=1.0,
        model_name="small",
    )


def test_transcribe_job_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    cid = uuid4()
    yt = YouTubeContent(
        video_id="vidvidvidvi",
        title="Title",
        channel="C",
        duration_seconds=30.0,
        audio_path=Path("/data/v.m4a"),
    )
    tr = _make_transcript()

    monkeypatch.setattr(transcribe, "fetch_content", lambda _url, _cid: yt)
    monkeypatch.setattr(
        transcribe,
        "transcribe_audio",
        lambda audio_path, language, data_dir: tr,
    )
    monkeypatch.setattr(
        transcribe,
        "save_transcript",
        lambda _url, _cid, _tr: UUID("33333333-3333-3333-3333-333333333333"),
    )

    result = transcribe.run(content_id=str(cid), language="en")
    assert result["transcript_id"] == "33333333-3333-3333-3333-333333333333"
    assert result["n_segments"] == 1
    assert result["language"] == "en"


def test_transcribe_job_missing_content_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe, "fetch_content", lambda _url, _cid: None)
    with pytest.raises(ValueError, match="no ingested_content row"):
        transcribe.run(content_id=str(uuid4()))


def test_transcribe_job_rejects_non_youtube_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = RawTextContent(text="not transcribable")
    monkeypatch.setattr(transcribe, "fetch_content", lambda _url, _cid: raw)
    with pytest.raises(ValueError, match="only supports YouTubeContent"):
        transcribe.run(content_id=str(uuid4()))


# ---- extract_strategy (Phase 2.2 real wiring) ------------------------------
# Job-level happy-path / cache-hit / error tests live in test_extract_job.py.
# Leaving this section as a navigation breadcrumb.


# ---- llm helper -----------------------------------------------------------


def test_get_anthropic_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from marketmind_workers.services import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm.get_anthropic_api_key() == "sk-test"


def test_get_anthropic_api_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from marketmind_workers.services import llm

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.get_anthropic_api_key() == ""


def test_llm_module_does_not_import_anthropic_sdk() -> None:
    """Phase 2.1 hard rule: the anthropic SDK must not be imported in
    production code paths. Validate by inspecting sys.modules after a
    fresh reimport of the llm helper.
    """
    import importlib
    import sys

    # The user-prompt-submit hook ensures `anthropic` is on the path
    # (it's an installed dep), but the llm.py module must NOT import it.
    sys.modules.pop("marketmind_workers.services.llm", None)
    importlib.import_module("marketmind_workers.services.llm")
    # Note: other tests in this session may have imported anthropic;
    # the assertion narrows to "llm.py has no top-level `from anthropic`".
    # We check the module's source dependency map instead.
    import marketmind_workers.services.llm as llm_mod

    source = Path(llm_mod.__file__).read_text() if llm_mod.__file__ else ""
    assert "import anthropic" not in source
    assert "from anthropic" not in source
