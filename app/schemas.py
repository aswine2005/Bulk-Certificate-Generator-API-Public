"""Request / response schemas (the API contract)."""

import unicodedata
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.renderer import can_render

API_PREFIX = "/api/v1"

# ---------------------------------------------------------------------------
# Shared field types
# ---------------------------------------------------------------------------


def _clean_text(value: str) -> str:
    """Normalise free text and make sure the template can actually print it."""
    value = " ".join(value.split())  # trim + collapse internal whitespace/newlines
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("must not contain control characters")
    unsupported = sorted({ch for ch in value if not can_render(ch)})
    if unsupported:
        shown = ", ".join(repr(ch) for ch in unsupported[:5])
        raise ValueError(f"contains characters the certificate template cannot render: {shown}")
    return value


def _has_letter(value: str) -> str:
    if not any(ch.isalpha() for ch in value):
        raise ValueError("must contain at least one letter")
    return value


def _as_utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything we store is UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def CertText(min_length: int, max_length: int):  # noqa: N802 - reads like a type
    return Annotated[
        str, Field(min_length=min_length, max_length=max_length), AfterValidator(_clean_text)
    ]


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]
ValidationMode = Literal["partial", "strict"]

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CertificateDetails(BaseModel):
    """Information printed on every certificate in the job."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: CertText(3, 80) = "Certificate of Completion"
    course_name: CertText(2, 150) = Field(description="Course or event the certificate is for.")
    issuer_name: CertText(2, 100) = Field(description="Organisation issuing the certificate.")
    issue_date: date | None = Field(default=None, description="Defaults to today (UTC).")
    description: CertText(1, 300) | None = None
    signatory_name: CertText(2, 100) | None = None
    signatory_title: CertText(2, 100) | None = None


class RecipientIn(BaseModel):
    """One recipient. Validated row-by-row, see app/services/validation.py."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[CertText(1, 100), AfterValidator(_has_letter)]
    email: EmailStr


class JobCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "certificate": {
                        "title": "Certificate of Completion",
                        "course_name": "Advanced Backend Engineering",
                        "issuer_name": "Acme Academy",
                        "issue_date": "2026-10-08",
                        "signatory_name": "Priya Raman",
                        "signatory_title": "Director of Learning",
                    },
                    "recipients": [
                        {"name": "Ada Lovelace", "email": "ada@example.com"},
                        {"name": "Alan Turing", "email": "alan@example.com"},
                        {"name": "", "email": "not-an-email"},
                    ],
                    "validation_mode": "partial",
                }
            ]
        },
    )

    certificate: CertificateDetails
    # Recipients are accepted as raw objects and validated individually, so one
    # bad row produces a per-row error instead of rejecting the whole request.
    recipients: list[dict[str, Any]] = Field(
        min_length=1,
        description="Recipient objects with `name` and `email`. Each row is validated "
        "separately; see `validation_mode`.",
    )
    validation_mode: ValidationMode = Field(
        default="partial",
        description="`partial`: generate certificates for valid rows and report invalid "
        "ones. `strict`: reject the whole request if any row is invalid.",
    )


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class FieldError(BaseModel):
    field: str | None = Field(
        default=None, description="Offending field; null for row-level errors."
    )
    message: str


class InvalidRecipient(BaseModel):
    row_index: int
    errors: list[FieldError]


class Progress(BaseModel):
    total: int = Field(description="Recipients in the request (valid + invalid).")
    pending: int
    generated: int
    failed: int
    invalid: int
    processed: int = Field(description="Rows that reached a final state.")
    percent_complete: float

    @classmethod
    def from_counts(cls, counts: dict[CertificateStatus, int]) -> "Progress":
        pending = counts.get(CertificateStatus.PENDING, 0)
        generated = counts.get(CertificateStatus.GENERATED, 0)
        failed = counts.get(CertificateStatus.FAILED, 0)
        invalid = counts.get(CertificateStatus.INVALID, 0)
        total = pending + generated + failed + invalid
        processed = total - pending
        percent = round(processed / total * 100, 2) if total else 100.0
        return cls(
            total=total,
            pending=pending,
            generated=generated,
            failed=failed,
            invalid=invalid,
            processed=processed,
            percent_complete=percent,
        )


class CertificateDetailsOut(BaseModel):
    title: str
    course_name: str
    issuer_name: str
    issue_date: date
    description: str | None
    signatory_name: str | None
    signatory_title: str | None


class JobLinks(BaseModel):
    status: str
    certificates: str
    download_zip: str
    retry: str


class JobOut(BaseModel):
    id: str
    status: JobStatus
    validation_mode: ValidationMode
    certificate: CertificateDetailsOut
    progress: Progress
    error: str | None = Field(description="Job-level error, e.g. if the worker crashed.")
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    links: JobLinks

    @classmethod
    def build(cls, job: Job, progress: Progress, **extra: Any) -> "JobOut":
        base = f"{API_PREFIX}/jobs/{job.id}"
        return cls(
            id=job.id,
            status=job.status,
            validation_mode=job.validation_mode,
            certificate=CertificateDetailsOut(
                title=job.title,
                course_name=job.course_name,
                issuer_name=job.issuer_name,
                issue_date=job.issue_date,
                description=job.description,
                signatory_name=job.signatory_name,
                signatory_title=job.signatory_title,
            ),
            progress=progress,
            error=job.error,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            links=JobLinks(
                status=base,
                certificates=f"{base}/certificates",
                download_zip=f"{base}/download",
                retry=f"{base}/retry",
            ),
            **extra,
        )


class JobCreatedOut(JobOut):
    invalid_recipients: list[InvalidRecipient] = Field(
        description="Rows rejected by validation (only in `partial` mode)."
    )


class JobPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]


class CertificateOut(BaseModel):
    id: str
    job_id: str
    row_index: int = Field(description="Position of the recipient in the submitted list.")
    status: CertificateStatus
    recipient_name: str | None
    recipient_email: str | None
    verification_code: str | None
    errors: list[FieldError]
    attempts: int
    file_size_bytes: int | None
    created_at: UTCDateTime
    generated_at: UTCDateTime | None
    download_url: str | None

    @classmethod
    def build(cls, cert: Certificate) -> "CertificateOut":
        generated = cert.status == CertificateStatus.GENERATED
        return cls(
            id=cert.id,
            job_id=cert.job_id,
            row_index=cert.row_index,
            status=cert.status,
            recipient_name=cert.recipient_name,
            recipient_email=cert.recipient_email,
            verification_code=cert.verification_code,
            errors=[FieldError(**e) for e in cert.errors or []],
            attempts=cert.attempts,
            file_size_bytes=cert.file_size_bytes,
            created_at=cert.created_at,
            generated_at=cert.generated_at,
            download_url=f"{API_PREFIX}/certificates/{cert.id}/download" if generated else None,
        )


class CertificatePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]


class VerificationOut(BaseModel):
    valid: bool
    verification_code: str
    certificate_id: str
    recipient_name: str
    title: str
    course_name: str
    issuer_name: str
    issue_date: date
    generated_at: UTCDateTime


class ErrorOut(BaseModel):
    detail: str
    invalid_recipients: list[InvalidRecipient] | None = None
