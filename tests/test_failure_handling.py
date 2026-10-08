"""A failure in one certificate must not stop the others; failures are visible
and retryable; interrupted jobs are resumed."""

import pytest
from sqlalchemy import update

from app.models import Job, JobStatus
from tests.factories import failing_render, job_payload, recipient


def rows():
    return [recipient("Ada Lovelace"), recipient("Broken Name"), recipient("Grace Hopper")]


def patch_render(client, monkeypatch, names, **kwargs):
    renderer = client.app.state.processor.renderer
    monkeypatch.setattr(renderer, "render", failing_render(renderer.render, names, **kwargs))


def test_one_failing_certificate_does_not_stop_the_others(client, monkeypatch):
    patch_render(client, monkeypatch, ["Broken Name"])

    job_id = client.post("/api/v1/jobs", json=job_payload(rows())).json()["id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed_with_errors"
    assert job["progress"]["generated"] == 2
    assert job["progress"]["failed"] == 1

    items = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    assert [c["status"] for c in items] == ["generated", "failed", "generated"]
    failed = items[1]
    assert failed["recipient_name"] == "Broken Name"
    assert failed["errors"] == [
        {"field": None, "message": "RuntimeError: simulated failure for Broken Name"}
    ]
    assert failed["download_url"] is None


def test_failed_rows_can_be_filtered(client, monkeypatch):
    patch_render(client, monkeypatch, ["Broken Name"])
    job_id = client.post("/api/v1/jobs", json=job_payload(rows())).json()["id"]

    page = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "failed"}).json()

    assert page["total"] == 1
    assert page["items"][0]["row_index"] == 1


def test_storage_error_is_isolated_to_that_certificate(client, monkeypatch):
    storage = client.app.state.storage
    original_save = storage.save
    calls = {"n": 0}

    def flaky_save(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(storage, "save", flaky_save)
    job_id = client.post("/api/v1/jobs", json=job_payload(rows())).json()["id"]

    items = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    assert [c["status"] for c in items] == ["generated", "failed", "generated"]
    assert "No space left on device" in items[1]["errors"][0]["message"]


def test_job_fails_when_no_certificate_can_be_generated(client, monkeypatch):
    patch_render(client, monkeypatch, ["Ada Lovelace", "Broken Name", "Grace Hopper"])

    job_id = client.post("/api/v1/jobs", json=job_payload(rows())).json()["id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["progress"]["failed"] == 3


def test_retry_regenerates_only_the_failed_certificates(client, monkeypatch):
    # Fails once (a transient error), succeeds on the second attempt.
    patch_render(client, monkeypatch, ["Broken Name"], times=1)
    job_id = client.post("/api/v1/jobs", json=job_payload(rows())).json()["id"]
    before = {
        c["row_index"]: c for c in client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    }

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 202
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["progress"]["generated"] == 3
    after = {
        c["row_index"]: c for c in client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    }
    assert after[1]["status"] == "generated"
    assert after[1]["attempts"] == 2
    assert after[1]["errors"] == []
    # Certificates that already succeeded were not regenerated.
    assert after[0]["attempts"] == 1
    assert after[0]["generated_at"] == before[0]["generated_at"]


def test_retry_does_not_touch_invalid_rows(client, monkeypatch):
    patch_render(client, monkeypatch, ["Broken Name"], times=1)
    payload = job_payload(rows() + [{"name": "", "email": "invalid"}])
    job_id = client.post("/api/v1/jobs", json=payload).json()["id"]

    client.post(f"/api/v1/jobs/{job_id}/retry")

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed_with_errors"
    assert job["progress"]["generated"] == 3
    assert job["progress"]["invalid"] == 1


def test_retry_without_failures_is_a_conflict(client):
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == "Job has no failed certificates to retry."
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"  # unchanged


def test_retry_while_job_is_still_running_is_a_conflict(manual_client):
    job_id = manual_client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    response = manual_client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert "still pending" in response.json()["detail"]


def test_unexpected_job_level_error_marks_job_failed_and_is_retryable(client, monkeypatch):
    processor = client.app.state.processor
    original = processor._generate_pending

    def crash(*args, **kwargs):
        raise RuntimeError("database connection lost")

    monkeypatch.setattr(processor, "_generate_pending", crash)
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"] == "RuntimeError: database connection lost"
    assert job["progress"]["pending"] == 3  # nothing was lost

    monkeypatch.setattr(processor, "_generate_pending", original)
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 202
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["error"] is None


@pytest.mark.parametrize("left_in", [JobStatus.PENDING, JobStatus.PROCESSING])
def test_unfinished_jobs_are_resumed_on_startup(make_client, left_in):
    # First "run" of the app: job accepted but the process dies before finishing.
    first = make_client(queue_factory=_DroppingQueue)
    job_id = first.post("/api/v1/jobs", json=job_payload()).json()["id"]
    with first.app.state.session_factory() as db:
        db.execute(update(Job).where(Job.id == job_id).values(status=left_in))
        db.commit()

    # Second run against the same database: the job is picked up again.
    second = make_client()

    job = second.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["progress"]["generated"] == 3


def test_shutdown_stops_after_current_batch_and_job_resumes_later(make_client, monkeypatch):
    first = make_client(queue_factory=_DroppingQueue)
    rows = [recipient(f"Person {c}") for c in "ABCDE"]
    job_id = first.post("/api/v1/jobs", json=job_payload(rows)).json()["id"]
    processor = first.app.state.processor
    original = processor.renderer.render
    rendered = []

    def render_then_request_shutdown(data):
        rendered.append(data.recipient_name)
        processor.stop_event.set()  # shutdown arrives during the first batch
        return original(data)

    monkeypatch.setattr(processor.renderer, "render", render_then_request_shutdown)
    processor.process(job_id)

    assert rendered == ["Person A", "Person B"]  # finished its batch (size 2), then stopped
    assert first.get(f"/api/v1/jobs/{job_id}").json()["status"] == "processing"

    second = make_client()  # restart
    job = second.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["progress"]["generated"] == 5


class _DroppingQueue:
    """Accepts jobs but never runs them, like a process that crashed."""

    def __init__(self, handler):
        pass

    def enqueue(self, job_id):
        pass

    def shutdown(self):
        pass
