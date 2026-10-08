"""Job status and progress tracking."""

import time

import pytest

from app.models import CertificateStatus, JobStatus
from app.services import job_service
from app.services.processor import final_status
from app.worker import ThreadPoolJobQueue
from tests.factories import job_payload, recipient, recipients


def test_new_job_is_pending_with_zero_progress(manual_client):
    job_id = manual_client.post("/api/v1/jobs", json=job_payload(recipients(4))).json()["id"]

    job = manual_client.get(f"/api/v1/jobs/{job_id}").json()

    assert job["status"] == "pending"
    assert job["progress"]["percent_complete"] == 0.0
    assert job["progress"]["pending"] == 4
    assert job["started_at"] is None and job["completed_at"] is None


def test_finished_job_is_completed_with_full_progress(manual_client):
    job_id = manual_client.post("/api/v1/jobs", json=job_payload(recipients(5))).json()["id"]
    manual_client.app.state.queue.run_all()

    job = manual_client.get(f"/api/v1/jobs/{job_id}").json()

    assert job["status"] == "completed"
    assert job["progress"] == {
        "total": 5,
        "pending": 0,
        "generated": 5,
        "failed": 0,
        "invalid": 0,
        "processed": 5,
        "percent_complete": 100.0,
    }
    assert job["started_at"] is not None
    assert job["completed_at"] >= job["started_at"]


def test_job_with_invalid_rows_completes_with_errors(client):
    rows = [recipient("Ada Lovelace"), {"name": "Bad", "email": "x"}, recipient("Alan Turing")]
    job_id = client.post("/api/v1/jobs", json=job_payload(rows)).json()["id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()

    assert job["status"] == "completed_with_errors"
    assert job["progress"]["generated"] == 2
    assert job["progress"]["invalid"] == 1
    assert job["progress"]["percent_complete"] == 100.0


def test_progress_is_visible_while_the_job_is_running(manual_client, monkeypatch):
    """Results are committed batch by batch (batch_size=2 in tests), so another
    database session can watch the job advance before it finishes."""
    state = manual_client.app.state
    job_id = manual_client.post("/api/v1/jobs", json=job_payload(recipients(5))).json()["id"]

    observed: list[tuple[str, int]] = []
    original = state.processor.renderer.render

    def render_and_observe(data):
        with state.session_factory() as db:
            status = job_service.get_job(db, job_id).status.value
            observed.append((status, job_service.get_progress(db, [job_id])[job_id].generated))
        return original(data)

    monkeypatch.setattr(state.processor.renderer, "render", render_and_observe)
    state.queue.run_all()

    # What another client would have seen just before each certificate rendered.
    assert observed == [
        ("processing", 0),
        ("processing", 0),
        ("processing", 2),
        ("processing", 2),
        ("processing", 4),
    ]


GEN, FAIL, INV = CertificateStatus.GENERATED, CertificateStatus.FAILED, CertificateStatus.INVALID


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({GEN: 3}, JobStatus.COMPLETED),
        ({GEN: 2, FAIL: 1}, JobStatus.COMPLETED_WITH_ERRORS),
        ({GEN: 2, INV: 1}, JobStatus.COMPLETED_WITH_ERRORS),
        ({FAIL: 2, INV: 1}, JobStatus.FAILED),
    ],
)
def test_final_status_rules(counts, expected):
    assert final_status(counts) is expected


def test_unknown_job_returns_404(client):
    response = client.get("/api/v1/jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Job does-not-exist not found."}


def test_background_thread_pool_processes_job_end_to_end(make_client):
    client = make_client(queue_factory=lambda handler: ThreadPoolJobQueue(handler, max_workers=2))
    created = client.post("/api/v1/jobs", json=job_payload(recipients(12)))
    assert created.status_code == 202  # returned before generation finished
    job_id = created.json()["id"]

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] not in ("pending", "processing"):
            break
        time.sleep(0.05)

    assert job["status"] == "completed"
    assert job["progress"]["generated"] == 12
