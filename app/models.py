"""ORM models.

A *job* is one bulk request. Every recipient in that request becomes one
*certificate* row, including recipients that failed validation, so that the
client gets a result for every row it submitted (matched by ``row_index``).
"""

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class JobStatus(enum.StrEnum):
    PENDING = "pending"  # accepted, waiting for a worker
    PROCESSING = "processing"  # a worker is generating certificates
    COMPLETED = "completed"  # every recipient got a certificate
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some generated, some failed/invalid
    FAILED = "failed"  # nothing could be generated

    @property
    def is_terminal(self) -> bool:
        return self not in (JobStatus.PENDING, JobStatus.PROCESSING)


class CertificateStatus(enum.StrEnum):
    PENDING = "pending"  # valid, waiting to be generated
    GENERATED = "generated"  # PDF written to storage
    FAILED = "failed"  # generation raised an error (can be retried)
    INVALID = "invalid"  # rejected by validation, never generated


def _str_enum(enum_cls: type[enum.Enum]) -> Enum:
    # Store the lowercase value as a plain VARCHAR so the schema is portable.
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[JobStatus] = mapped_column(
        _str_enum(JobStatus), default=JobStatus.PENDING, index=True
    )
    validation_mode: Mapped[str] = mapped_column(String(16), default="partial")

    # Certificate details shared by every recipient in the job.
    title: Mapped[str] = mapped_column(String(80))
    course_name: Mapped[str] = mapped_column(String(150))
    issuer_name: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(String(300))
    signatory_name: Mapped[str | None] = mapped_column(String(100))
    signatory_title: Mapped[str | None] = mapped_column(String(100))

    # Number of recipients in the original request (valid + invalid).
    total_recipients: Mapped[int] = mapped_column(Integer)

    # Optional client-supplied key that makes job creation safe to retry.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))

    # Job-level error (e.g. the worker crashed), separate from per-certificate errors.
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("job_id", "row_index", name="uq_certificate_job_row"),
        # Serves both "next pending batch" and the per-status progress counts.
        Index("ix_certificates_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    # Position of the recipient in the submitted list (0-based).
    row_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[CertificateStatus] = mapped_column(
        _str_enum(CertificateStatus), default=CertificateStatus.PENDING
    )

    # Nullable because an invalid row may be missing these fields.
    recipient_name: Mapped[str | None] = mapped_column(String(255))
    recipient_email: Mapped[str | None] = mapped_column(String(320))

    # Short human-readable code printed on the certificate / encoded in its QR code.
    verification_code: Mapped[str | None] = mapped_column(String(20), unique=True)

    file_path: Mapped[str | None] = mapped_column(String(500))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)

    # List of {"field": str | None, "message": str} for INVALID and FAILED rows.
    errors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="certificates")
