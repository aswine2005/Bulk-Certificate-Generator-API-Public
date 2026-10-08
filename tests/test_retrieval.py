"""Retrieving generated certificates: metadata, single PDF, ZIP, verification."""

import csv
import io
import zipfile

from tests.factories import failing_render, job_payload, recipient, recipients


def create_job(client, rows=None) -> tuple[str, list[dict]]:
    job_id = client.post("/api/v1/jobs", json=job_payload(rows)).json()["id"]
    items = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    return job_id, items


def test_get_certificate_metadata(client):
    job_id, items = create_job(client, [recipient("Ada Lovelace", "ada@example.com")])

    cert = client.get(f"/api/v1/certificates/{items[0]['id']}").json()

    assert cert["job_id"] == job_id
    assert cert["status"] == "generated"
    assert cert["recipient_name"] == "Ada Lovelace"
    assert cert["recipient_email"] == "ada@example.com"
    assert cert["download_url"] == f"/api/v1/certificates/{cert['id']}/download"


def test_download_single_certificate_pdf(client, settings):
    job_id, items = create_job(client, [recipient("Ada Lovelace")])
    cert = items[0]

    response = client.get(cert["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    expected_name = f"ada-lovelace-{cert['verification_code']}.pdf"
    assert response.headers["content-disposition"] == f'attachment; filename="{expected_name}"'
    assert response.content == (settings.storage_dir / job_id / f"{cert['id']}.pdf").read_bytes()


def test_download_is_refused_for_certificates_that_were_not_generated(client, monkeypatch):
    renderer = client.app.state.processor.renderer
    monkeypatch.setattr(renderer, "render", failing_render(renderer.render, ["Broken Name"]))
    _, items = create_job(
        client, [recipient("Broken Name"), {"name": "", "email": "x"}, recipient("Ada Lovelace")]
    )

    failed = client.get(f"/api/v1/certificates/{items[0]['id']}/download")
    invalid = client.get(f"/api/v1/certificates/{items[1]['id']}/download")

    assert failed.status_code == 409
    assert failed.json()["detail"] == "Certificate is failed, not generated."
    assert invalid.status_code == 409


def test_unknown_certificate_returns_404(client):
    assert client.get("/api/v1/certificates/nope").status_code == 404
    assert client.get("/api/v1/certificates/nope/download").status_code == 404


def test_missing_file_on_disk_returns_404(client, settings):
    job_id, items = create_job(client, [recipient()])
    (settings.storage_dir / job_id / f"{items[0]['id']}.pdf").unlink()

    response = client.get(items[0]["download_url"])

    assert response.status_code == 404
    assert "missing" in response.json()["detail"]


def test_list_certificates_is_paginated(client):
    job_id, _ = create_job(client, recipients(7))

    page = client.get(
        f"/api/v1/jobs/{job_id}/certificates", params={"limit": 3, "offset": 3}
    ).json()

    assert page["total"] == 7
    assert (page["limit"], page["offset"]) == (3, 3)
    assert [c["row_index"] for c in page["items"]] == [3, 4, 5]


def test_list_certificates_of_unknown_job_returns_404(client):
    assert client.get("/api/v1/jobs/nope/certificates").status_code == 404


def test_download_whole_job_as_zip_with_manifest(client, monkeypatch):
    renderer = client.app.state.processor.renderer
    monkeypatch.setattr(renderer, "render", failing_render(renderer.render, ["Broken Name"]))
    rows = [
        recipient("Ada Lovelace"),
        recipient("Broken Name"),
        {"name": "", "email": "x"},
        recipient("Grace Hopper"),
    ]
    job_id, items = create_job(client, rows)

    response = client.get(f"/api/v1/jobs/{job_id}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert ".zip" in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    pdfs = sorted(n for n in archive.namelist() if n.endswith(".pdf"))
    assert pdfs == [
        f"certificates/00000-ada-lovelace-{items[0]['verification_code']}.pdf",
        f"certificates/00003-grace-hopper-{items[3]['verification_code']}.pdf",
    ]
    assert archive.read(pdfs[0]).startswith(b"%PDF-")

    manifest = list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode("utf-8-sig"))))
    assert [(m["row_index"], m["status"]) for m in manifest] == [
        ("0", "generated"),
        ("1", "failed"),
        ("2", "invalid"),
        ("3", "generated"),
    ]
    assert "simulated failure" in manifest[1]["errors"]
    assert manifest[2]["errors"].startswith("name:")


def test_zip_download_waits_until_job_has_finished(manual_client):
    job_id = manual_client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    response = manual_client.get(f"/api/v1/jobs/{job_id}/download")

    assert response.status_code == 409
    manual_client.app.state.queue.run_all()
    assert manual_client.get(f"/api/v1/jobs/{job_id}/download").status_code == 200


def test_zip_download_of_job_without_certificates_returns_404(client, monkeypatch):
    renderer = client.app.state.processor.renderer
    monkeypatch.setattr(renderer, "render", failing_render(renderer.render, ["Ada Lovelace"]))
    job_id, _ = create_job(client, [recipient("Ada Lovelace")])

    assert client.get(f"/api/v1/jobs/{job_id}/download").status_code == 404


def test_verify_certificate_by_code(client):
    _, items = create_job(client, [recipient("Ada Lovelace")])
    code = items[0]["verification_code"]

    # Codes typed by a human: lower case, without dashes.
    response = client.get(f"/api/v1/verify/{code.lower().replace('-', '')}")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["verification_code"] == code
    assert body["recipient_name"] == "Ada Lovelace"
    assert body["course_name"] == "Advanced Backend Engineering"
    assert body["issue_date"] == "2026-10-08"


def test_verify_unknown_code_returns_404(client):
    response = client.get("/api/v1/verify/AAAA-BBBB-CCCC")
    assert response.status_code == 404
