"""Input validation: the request envelope and individual recipient rows."""

import pytest

from app.services.validation import validate_recipients
from tests.factories import job_payload, recipient, recipients

# ---------------------------------------------------------------------------
# Row-level validation (unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "field", "message"),
    [
        ({"email": "a@example.com"}, "name", "Field required"),
        ({"name": "   ", "email": "a@example.com"}, "name", "at least 1 character"),
        ({"name": "12345", "email": "a@example.com"}, "name", "at least one letter"),
        ({"name": "A" * 101, "email": "a@example.com"}, "name", "at most 100 characters"),
        ({"name": "Ada\x00Lovelace", "email": "a@example.com"}, "name", "control characters"),
        ({"name": "李雷", "email": "a@example.com"}, "name", "cannot render"),
        ({"name": 42, "email": "a@example.com"}, "name", "valid string"),
        ({"name": "Ada"}, "email", "Field required"),
        ({"name": "Ada", "email": "not-an-email"}, "email", "not a valid email"),
        ({"name": "Ada", "email": "ada@"}, "email", "not a valid email"),
        ({"name": "Ada", "email": "a@example.com", "phone": "1"}, "phone", "Extra inputs"),
    ],
)
def test_invalid_recipient_rows_are_reported_with_field_and_reason(row, field, message):
    valid, invalid = validate_recipients([row])

    assert valid == []
    assert len(invalid) == 1
    assert invalid[0].row_index == 0
    assert any(e.field == field and message in e.message for e in invalid[0].errors), invalid[0]


@pytest.mark.parametrize(
    "name",
    [
        "José Müller",
        "Zoë O'Brien-Smith",
        "Ngozi Okonjo-Iweala",
        "Aswin E",
        "Ana María García Núñez",
    ],
)
def test_accepts_real_world_latin_names(name):
    valid, invalid = validate_recipients([recipient(name, "x@example.com")])
    assert invalid == [] and valid[0].recipient.name == name


def test_normalises_whitespace_in_names():
    valid, _ = validate_recipients([recipient("  Ada \t  Lovelace\n", "ada@example.com")])
    assert valid[0].recipient.name == "Ada Lovelace"


def test_duplicate_emails_in_one_job_are_rejected_case_insensitively():
    valid, invalid = validate_recipients(
        [
            recipient("Ada Lovelace", "ada@example.com"),
            recipient("Ada L.", "ADA@Example.com"),
        ]
    )

    assert [v.row_index for v in valid] == [0]
    assert invalid[0].row_index == 1
    assert invalid[0].errors[0].field == "email"
    assert "duplicate email" in invalid[0].errors[0].message


# ---------------------------------------------------------------------------
# API behaviour
# ---------------------------------------------------------------------------


def test_partial_mode_creates_job_for_valid_rows_and_reports_invalid_ones(client):
    rows = [recipient("Ada Lovelace"), {"name": "", "email": "bad"}, recipient("Alan Turing")]

    response = client.post("/api/v1/jobs", json=job_payload(rows))

    assert response.status_code == 202
    body = response.json()
    assert [r["row_index"] for r in body["invalid_recipients"]] == [1]
    assert {e["field"] for e in body["invalid_recipients"][0]["errors"]} == {"name", "email"}

    items = client.get(f"/api/v1/jobs/{body['id']}/certificates").json()["items"]
    assert [c["status"] for c in items] == ["generated", "invalid", "generated"]
    assert items[1]["errors"] and items[1]["download_url"] is None
    assert items[1]["verification_code"] is None


def test_strict_mode_rejects_whole_request_if_any_row_is_invalid(client):
    rows = [recipient("Ada Lovelace"), recipient("Bad Email", "nope")]

    response = client.post("/api/v1/jobs", json=job_payload(rows, validation_mode="strict"))

    assert response.status_code == 422
    body = response.json()
    assert "strict" in body["detail"]
    assert body["invalid_recipients"][0]["row_index"] == 1
    assert client.get("/api/v1/jobs").json()["total"] == 0  # nothing was created


def test_strict_mode_accepts_fully_valid_request(client):
    payload = job_payload(recipients(2), validation_mode="strict")
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 202
    assert response.json()["validation_mode"] == "strict"


def test_request_with_no_valid_recipients_is_rejected(client):
    rows = [{"name": "", "email": "x"}, {"name": "Bob"}]

    response = client.post("/api/v1/jobs", json=job_payload(rows))

    assert response.status_code == 422
    assert [r["row_index"] for r in response.json()["invalid_recipients"]] == [0, 1]
    assert client.get("/api/v1/jobs").json()["total"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update(recipients=[]), id="empty-recipient-list"),
        pytest.param(lambda p: p.update(recipients="ada@example.com"), id="recipients-not-a-list"),
        pytest.param(lambda p: p.update(recipients=["ada@example.com"]), id="recipient-not-object"),
        pytest.param(lambda p: p.pop("certificate"), id="missing-certificate"),
        pytest.param(lambda p: p["certificate"].pop("course_name"), id="missing-course-name"),
        pytest.param(lambda p: p["certificate"].update(issuer_name=" "), id="blank-issuer"),
        pytest.param(lambda p: p["certificate"].update(course_name="Ψ" * 5), id="unrenderable"),
        pytest.param(lambda p: p["certificate"].update(issue_date="08/10/2026"), id="bad-date"),
        pytest.param(lambda p: p.update(validation_mode="lenient"), id="bad-validation-mode"),
        pytest.param(lambda p: p.update(priority="high"), id="unknown-top-level-field"),
    ],
)
def test_malformed_request_envelope_is_rejected(client, mutate):
    payload = job_payload(recipients(1))
    mutate(payload)

    response = client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 422
    assert client.get("/api/v1/jobs").json()["total"] == 0


def test_too_many_recipients_is_rejected(make_client):
    client = make_client(max_recipients_per_job=3)

    response = client.post("/api/v1/jobs", json=job_payload(recipients(4)))

    assert response.status_code == 422
    assert "maximum per job is 3" in response.json()["detail"]
