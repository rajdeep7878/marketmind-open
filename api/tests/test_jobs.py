from __future__ import annotations

from uuid import UUID

from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_shared.schemas import JobStatus
from rq import Queue, SimpleWorker


def test_post_jobs_enqueues_and_returns_queued(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    resp = client.post(
        "/jobs",
        json={"kind": "dummy", "payload": {"message": "hello"}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == JobStatus.QUEUED.value
    assert body["kind"] == "dummy"
    # id must be a valid uuid
    UUID(body["id"])
    # Job should be present in fakeredis queue
    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1


def test_post_jobs_rejects_invalid_payload(client: TestClient) -> None:
    # empty message -> validation error on DummyJobPayload (min_length=1)
    resp = client.post(
        "/jobs",
        json={"kind": "dummy", "payload": {"message": ""}},
    )
    assert resp.status_code == 422


def test_post_jobs_rejects_extra_fields(client: TestClient) -> None:
    # extra="forbid" on JobSubmission
    resp = client.post(
        "/jobs",
        json={
            "kind": "dummy",
            "payload": {"message": "x"},
            "unexpected_field": True,
        },
    )
    assert resp.status_code == 422


def test_get_unknown_job_returns_404(client: TestClient) -> None:
    resp = client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_full_dummy_job_round_trip_via_simple_worker(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    # SimpleWorker runs jobs synchronously in the test process — no Redis BLPOP loop.
    submit = client.post(
        "/jobs",
        json={"kind": "dummy", "payload": {"message": "round-trip"}},
    )
    assert submit.status_code == 201
    job_id = submit.json()["id"]

    queue = Queue(name="default", connection=fake_redis)
    worker = SimpleWorker([queue], connection=fake_redis)
    worker.work(burst=True, with_scheduler=False)

    fetched = client.get(f"/jobs/{job_id}")
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["status"] == JobStatus.FINISHED.value
    assert body["result"]["echoed"] == "round-trip"
    assert "completed_at" in body["result"]


# ---- CookieError → 503 friendly response ---------------------------------


def test_get_job_returns_503_when_cookie_error_in_exc_info(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    """A failed job whose traceback mentions CookieError surfaces as
    503 with a friendly body, NOT a 200 JobView with a raw traceback.

    We construct the failure state directly in fakeredis rather than
    routing a real job through SimpleWorker — the contract under test
    is the API's translation of exc_info, not the worker plumbing.
    """
    from rq.job import JobStatus as RqJobStatus

    queue = Queue(name="default", connection=fake_redis)
    job = queue.enqueue(
        "marketmind_workers.jobs.ingest_youtube.run",
        kwargs={"url": "https://youtu.be/x"},
        job_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        meta={"marketmind:kind": "ingest_youtube"},
    )
    # Stamp a fake traceback containing the dotted-path marker the API
    # looks for. Matches what RQ would store when a worker raises.
    # RQ 2.x made `exc_info` read-only; mutate the underlying slot.
    job._exc_info = (  # type: ignore[attr-defined]
        "Traceback (most recent call last):\n"
        '  File "...", line 123, in run\n'
        "    ...\n"
        "marketmind_workers.services.ingest.CookieError: "
        "Sign in to confirm you're not a bot\n"
    )
    job.set_status(RqJobStatus.FAILED)
    job.save()

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "extraction_unavailable"
    assert "Please try again later" in body["detail"]["message"]
    # Critically: the raw traceback must not leak into the response.
    assert "Traceback" not in resp.text
    assert "CookieError" not in resp.text


def test_get_job_returns_422_when_format_unavailable_in_exc_info(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    """FormatUnavailableError → 422 + friendly body (permanent failure
    for this video; client should pick a different one)."""
    from rq.job import JobStatus as RqJobStatus

    queue = Queue(name="default", connection=fake_redis)
    job = queue.enqueue(
        "marketmind_workers.jobs.ingest_youtube.run",
        kwargs={"url": "https://youtu.be/x"},
        job_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        meta={"marketmind:kind": "ingest_youtube"},
    )
    job._exc_info = (  # type: ignore[attr-defined]
        "Traceback (most recent call last):\n"
        "marketmind_workers.services.ingest.FormatUnavailableError: "
        "Requested format is not available\n"
    )
    job.set_status(RqJobStatus.FAILED)
    job.save()

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "format_unavailable"
    assert "try a different video" in body["detail"]["message"]
    assert "Traceback" not in resp.text
    assert "FormatUnavailableError" not in resp.text


def test_get_job_returns_200_for_unrelated_failures(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    """Non-cookie failures keep the existing 200 + JobView path."""
    from rq.job import JobStatus as RqJobStatus

    queue = Queue(name="default", connection=fake_redis)
    job = queue.enqueue(
        "marketmind_workers.jobs.ingest_youtube.run",
        kwargs={"url": "https://youtu.be/x"},
        job_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        meta={"marketmind:kind": "ingest_youtube"},
    )
    # RQ 2.x made `exc_info` read-only; mutate the underlying slot.
    job._exc_info = (  # type: ignore[attr-defined]
        "Traceback (most recent call last):\n"
        "marketmind_workers.services.ingest.NotFoundError: video gone\n"
    )
    job.set_status(RqJobStatus.FAILED)
    job.save()

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == JobStatus.FAILED.value
