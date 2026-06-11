"""Content ingestion + transcription schemas.

These are the data shapes the workers pipeline produces:

  source -> ingest_* job -> IngestedContent
  audio  -> transcribe job -> Transcript
  (IngestedContent + Transcript) -> ExtractionInput -> [Phase 2.2: LLM extraction]

All models are immutable (frozen=True), reject unknown fields
(extra="forbid"), and require timezone-aware UTC for any datetime.
"""

from marketmind_shared.schemas.content.models import (
    ArticleContent,
    ContentSourceType,
    ExtractionInput,
    IngestedContent,
    RawTextContent,
    Transcript,
    TranscriptSegment,
    YouTubeContent,
)

__all__ = [
    "ArticleContent",
    "ContentSourceType",
    "ExtractionInput",
    "IngestedContent",
    "RawTextContent",
    "Transcript",
    "TranscriptSegment",
    "YouTubeContent",
]
