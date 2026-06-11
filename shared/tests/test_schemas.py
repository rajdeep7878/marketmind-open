from __future__ import annotations

from uuid import uuid4

import pytest
from marketmind_shared import SCHEMA_VERSION
from marketmind_shared.schemas import JobKind, JobStatus, JobSubmission, JobView
from pydantic import ValidationError


def test_schema_version_present() -> None:
    assert SCHEMA_VERSION  # must not be empty


def test_job_submission_validates_dummy() -> None:
    js = JobSubmission(kind=JobKind.DUMMY, payload={"message": "hi"})  # type: ignore[arg-type]
    assert js.kind is JobKind.DUMMY


def test_job_submission_rejects_empty_message() -> None:
    with pytest.raises(ValidationError):
        JobSubmission(kind=JobKind.DUMMY, payload={"message": ""})  # type: ignore[arg-type]


def test_job_submission_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        JobSubmission.model_validate({"kind": "dummy", "payload": {"message": "x"}, "extra": True})


def test_job_view_minimum_fields() -> None:
    jv = JobView(id=uuid4(), kind=JobKind.DUMMY, status=JobStatus.QUEUED)
    assert jv.status is JobStatus.QUEUED
    assert jv.result is None
    assert jv.error is None


@pytest.mark.parametrize(
    "kind_value",
    [
        "dummy",
        "ingest_youtube",
        "ingest_article",
        "ingest_raw_text",
        "transcribe",
        "extract_strategy",
    ],
)
def test_job_kind_value_round_trip(kind_value: str) -> None:
    # Migration safety check: persisting a job row with a string `kind`
    # and parsing it back must produce the matching enum member without
    # rewriting any old data.
    jv = JobView.model_validate(
        {"id": str(uuid4()), "kind": kind_value, "status": "queued"},
    )
    assert jv.kind == JobKind(kind_value)
    # Round-trip through JSON to make sure model_dump preserves the
    # raw string value, not the qualified enum name.
    blob = jv.model_dump_json()
    assert f'"kind":"{kind_value}"' in blob
