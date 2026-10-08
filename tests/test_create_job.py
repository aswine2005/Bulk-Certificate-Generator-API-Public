"""Creating a generation job."""

from datetime import UTC, datetime

from tests.factories import job_payload, recipient, recipients


def test_create_job_is_accepted_and_queued(manual_client):
    response = manual_client.post("/api/v1/jobs", json=job_payload(recipients(3)))

    assert response.status_code == 202
    body = response.json()
    assert response.headers["location"] == f"/api/v1/jobs/{body['id']}"
    assert body["status"] == "pending"
    assert body["progress"] == {
        "total": 3,
        "pending": 3,
        "generated": 0,
        "failed": 0,
        "invalid": 0,
        "processed": 0,
        "percent_complete": 0.0,
    }
    assert body["invalid_recipients"] == []
    assert body["links"]["status"] == f"/api/v1/jobs/{body['id']}"
    # Handed to the background queue exactly once, not processed in the request.
    assert manual_client.app.state.queue.queued == [body["id"]]


def test_create_job_stores_one_row_per_recipient_in_order(manual_client):
    rows = recipients(5)
    job_id = manual_client.post("/api/v1/jobs", json=job_payload(rows)).json()["id"]

    items = manual_client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]

    assert [c["row_index"] for c in items] == [0, 1, 2, 3, 4]
    assert [c["recipient_name"] for c in items] == [r["name"] for r in rows]
    assert all(c["status"] == "pending" for c in items)
    # Each certificate gets its own unique verification code up front.
    codes = {c["verification_code"] for c in items}
    assert len(codes) == 5 and None not in codes


def test_create_job_stores_certificate_details_and_defaults(manual_client):
    payload = job_payload([recipient()])
    del payload["certificate"]["issue_date"]

    body = manual_client.post("/api/v1/jobs", json=payload).json()

    assert body["certificate"] == {
        "title": "Certificate of Completion",  # default template title
        "course_name": "Advanced Backend Engineering",
        "issuer_name": "Acme Academy",
        "issue_date": datetime.now(UTC).date().isoformat(),  # defaults to today
        "description": None,
        "signatory_name": "Priya Raman",
        "signatory_title": "Director of Learning",
    }
    assert body["validation_mode"] == "partial"


def test_idempotency_key_replay_returns_the_original_job(manual_client):
    payload = job_payload(recipients(2))
    headers = {"Idempotency-Key": "batch-2026-10-08"}

    first = manual_client.post("/api/v1/jobs", json=payload, headers=headers)
    second = manual_client.post("/api/v1/jobs", json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert manual_client.get("/api/v1/jobs").json()["total"] == 1
    assert manual_client.app.state.queue.queued == [first.json()["id"]]  # queued once


def test_idempotency_key_reused_with_different_body_is_rejected(manual_client):
    headers = {"Idempotency-Key": "key-1"}
    manual_client.post("/api/v1/jobs", json=job_payload(recipients(2)), headers=headers)

    response = manual_client.post("/api/v1/jobs", json=job_payload(recipients(3)), headers=headers)

    assert response.status_code == 409
    assert "different request body" in response.json()["detail"]


def test_list_jobs_newest_first_with_status_filter(manual_client):
    older = manual_client.post("/api/v1/jobs", json=job_payload(recipients(1))).json()["id"]
    newer = manual_client.post("/api/v1/jobs", json=job_payload(recipients(1))).json()["id"]

    listing = manual_client.get("/api/v1/jobs").json()
    assert [j["id"] for j in listing["items"]] == [newer, older]

    manual_client.app.state.queue.handler(older)  # process only the older job
    completed = manual_client.get("/api/v1/jobs", params={"status": "completed"}).json()
    assert [j["id"] for j in completed["items"]] == [older]
