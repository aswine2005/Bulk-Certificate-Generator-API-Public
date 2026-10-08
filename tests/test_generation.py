"""Certificate generation: the PDF template and the files the worker writes."""

import io
from datetime import date

import pytest
from pypdf import PdfReader

from app.services.renderer import CertificateData, CertificateRenderer, can_render
from tests.factories import job_payload, recipient


def pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    assert len(reader.pages) == 1
    return reader.pages[0].extract_text()


def sample_data(**overrides) -> CertificateData:
    values = dict(
        recipient_name="Ada Lovelace",
        title="Certificate of Completion",
        course_name="Advanced Backend Engineering",
        issuer_name="Acme Academy",
        issue_date=date(2026, 10, 8),
        verification_code="ABCD-EFGH-JKLM",
        verification_url="https://certs.example.com/api/v1/verify/ABCD-EFGH-JKLM",
        description="With distinction",
        signatory_name="Priya Raman",
        signatory_title="Director of Learning",
    )
    values.update(overrides)
    return CertificateData(**values)


# ---------------------------------------------------------------------------
# Renderer (unit)
# ---------------------------------------------------------------------------


def test_renders_a_pdf_containing_the_recipient_specific_information():
    content = CertificateRenderer().render(sample_data())

    assert content.startswith(b"%PDF-")
    text = pdf_text(content)
    for expected in [
        "Ada Lovelace",
        "Advanced Backend Engineering",
        "ACME ACADEMY",
        "CERTIFICATE OF COMPLETION",
        "8 October 2026",
        "With distinction",
        "Priya Raman",
        "DIRECTOR OF LEARNING",
        "ABCD-EFGH-JKLM",
    ]:
        assert expected in text, expected


def test_optional_fields_can_be_omitted():
    data = sample_data(description=None, signatory_name=None, signatory_title=None)
    text = pdf_text(CertificateRenderer().render(data))
    assert "AUTHORIZED SIGNATORY" in text


def test_rendering_is_deterministic():
    renderer = CertificateRenderer()
    assert renderer.render(sample_data()) == renderer.render(sample_data())


def test_longest_allowed_values_still_render():
    data = sample_data(
        recipient_name="W" * 100,
        title="T" * 80,
        course_name="Course " * 21,
        issuer_name="I" * 100,
        description="Description " * 25,
        signatory_name="S" * 100,
        signatory_title="Title " * 16,
    )
    assert CertificateRenderer().render(data).startswith(b"%PDF-")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("José Müller – €100", True),
        ("李雷", False),
        ("Ελένη", False),
    ],
)
def test_can_render_reports_font_coverage(text, expected):
    assert can_render(text) is expected


# ---------------------------------------------------------------------------
# Worker output (integration)
# ---------------------------------------------------------------------------


def test_each_recipient_gets_their_own_pdf_on_disk(client, settings):
    rows = [recipient("Ada Lovelace"), recipient("Alan Turing"), recipient("Grace Hopper")]
    job_id = client.post("/api/v1/jobs", json=job_payload(rows)).json()["id"]

    items = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]

    for item, row in zip(items, rows, strict=True):
        assert item["status"] == "generated"
        assert item["attempts"] == 1
        assert item["generated_at"] is not None
        path = settings.storage_dir / job_id / f"{item['id']}.pdf"
        content = path.read_bytes()
        assert item["file_size_bytes"] == len(content)
        text = pdf_text(content)
        assert row["name"] in text  # each PDF carries its own recipient...
        assert item["verification_code"] in text  # ...and its own verification code
        others = [r["name"] for r in rows if r is not row]
        assert not any(name in text for name in others)


def test_storage_leaves_no_temporary_files(client, settings):
    job_id = client.post("/api/v1/jobs", json=job_payload()).json()["id"]

    files = sorted(p.suffix for p in (settings.storage_dir / job_id).iterdir())

    assert files == [".pdf", ".pdf", ".pdf"]


def test_qr_code_points_at_public_verification_url(manual_client, monkeypatch):
    seen: list[CertificateData] = []
    renderer = manual_client.app.state.processor.renderer
    original = renderer.render
    monkeypatch.setattr(renderer, "render", lambda data: seen.append(data) or original(data))

    manual_client.post("/api/v1/jobs", json=job_payload([recipient()]))
    manual_client.app.state.queue.run_all()

    code = seen[0].verification_code
    assert seen[0].verification_url == f"https://certs.example.com/api/v1/verify/{code}"
