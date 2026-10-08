"""Job use-cases: create, read progress, list results, retry.

Kept free of FastAPI so it can be called from the API, the worker or tests.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import ConflictError, NotFoundError, UnprocessableError
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import InvalidRecipient, JobCreate, Progress
from app.services.validation import InvalidRow, validate_recipients

# 32 characters with no look-alikes (0/O, 1/I), 12 chars => 60 bits of entropy.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_verification_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_verification_code(code: str) -> str:
    """Accept codes typed by humans: any case, with or without dashes."""
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}" if len(raw) == 12 else code.upper()


@dataclass
class CreateJobResult:
    job: Job
    invalid_recipients: list[InvalidRecipient]
    created: bool  # False when an Idempotency-Key replay returned an existing job


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_job(
    db: Session, payload: JobCreate, settings: Settings, idempotency_key: str | None = None
) -> CreateJobResult:
    if len(payload.recipients) > settings.max_recipients_per_job:
        raise UnprocessableError(
            f"Too many recipients: {len(payload.recipients)} "
            f"(maximum per job is {settings.max_recipients_per_job}). Split the list into "
            "several jobs."
        )

    request_hash = _hash_payload(payload)
    if idempotency_key:
        existing = _find_idempotent_job(db, idempotency_key, request_hash)
        if existing:
            return existing

    valid_rows, invalid_rows = validate_recipients(payload.recipients)
    invalid_out = [InvalidRecipient(row_index=r.row_index, errors=r.errors) for r in invalid_rows]

    if not valid_rows:
        raise UnprocessableError(
            "None of the recipients are valid; no job was created.",
            invalid_recipients=invalid_out,
        )
    if invalid_rows and payload.validation_mode == "strict":
        raise UnprocessableError(
            f"{len(invalid_rows)} recipient(s) are invalid and validation_mode is 'strict'; "
            "no job was created.",
            invalid_recipients=invalid_out,
        )

    details = payload.certificate
    job = Job(
        status=JobStatus.PENDING,
        validation_mode=payload.validation_mode,
        title=details.title,
        course_name=details.course_name,
        issuer_name=details.issuer_name,
        issue_date=details.issue_date or datetime.now(UTC).date(),
        description=details.description,
        signatory_name=details.signatory_name,
        signatory_title=details.signatory_title,
        total_recipients=len(payload.recipients),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    job.certificates = [
        Certificate(
            row_index=row.row_index,
            status=CertificateStatus.PENDING,
            recipient_name=row.recipient.name,
            recipient_email=row.recipient.email,
            verification_code=new_verification_code(),
        )
        for row in valid_rows
    ] + [_invalid_certificate(row) for row in invalid_rows]

    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # Two requests with the same Idempotency-Key raced; the other one won.
        db.rollback()
        if idempotency_key:
            existing = _find_idempotent_job(db, idempotency_key, request_hash)
            if existing:
                return existing
        raise

    return CreateJobResult(job=job, invalid_recipients=invalid_out, created=True)


def _invalid_certificate(row: InvalidRow) -> Certificate:
    return Certificate(
        row_index=row.row_index,
        status=CertificateStatus.INVALID,
        recipient_name=_safe_str(row.raw.get("name"), 255),
        recipient_email=_safe_str(row.raw.get("email"), 320),
        errors=[e.model_dump() for e in row.errors],
    )


def _safe_str(value: Any, max_length: int) -> str | None:
    # Keep whatever the client sent (for reference) as long as it is a string.
    return value[:max_length] if isinstance(value, str) else None


def _hash_payload(payload: JobCreate) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _find_idempotent_job(db: Session, key: str, request_hash: str) -> CreateJobResult | None:
    job = db.scalar(select(Job).where(Job.idempotency_key == key))
    if job is None:
        return None
    if job.request_hash != request_hash:
        raise ConflictError("This Idempotency-Key was already used with a different request body.")
    invalid = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.INVALID)
        .order_by(Certificate.row_index)
    )
    invalid_out = [InvalidRecipient(row_index=c.row_index, errors=c.errors or []) for c in invalid]
    return CreateJobResult(job=job, invalid_recipients=invalid_out, created=False)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return job


def get_progress(db: Session, job_ids: list[str]) -> dict[str, Progress]:
    """Progress for several jobs with one GROUP BY query.

    Counts are derived from the certificate rows rather than stored on the job,
    so they can never drift out of sync with the actual results.
    """
    counts: dict[str, dict[CertificateStatus, int]] = {job_id: {} for job_id in job_ids}
    if job_ids:
        rows = db.execute(
            select(Certificate.job_id, Certificate.status, func.count())
            .where(Certificate.job_id.in_(job_ids))
            .group_by(Certificate.job_id, Certificate.status)
        )
        for job_id, status, count in rows:
            counts[job_id][status] = count
    return {job_id: Progress.from_counts(c) for job_id, c in counts.items()}


def list_jobs(
    db: Session, status: JobStatus | None, limit: int, offset: int
) -> tuple[int, list[Job]]:
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    jobs = db.scalars(query.order_by(Job.created_at.desc()).limit(limit).offset(offset)).all()
    return total, list(jobs)


def list_certificates(
    db: Session, job_id: str, status: CertificateStatus | None, limit: int, offset: int
) -> tuple[int, list[Certificate]]:
    get_job(db, job_id)  # 404 if the job does not exist
    query = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        query = query.where(Certificate.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    certs = db.scalars(query.order_by(Certificate.row_index).limit(limit).offset(offset)).all()
    return total, list(certs)


def get_certificate(db: Session, certificate_id: str) -> Certificate:
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise NotFoundError(f"Certificate {certificate_id} not found.")
    return cert


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def retry_failed(db: Session, job_id: str) -> Job:
    """Put a finished job's FAILED certificates back in the queue.

    INVALID rows are not retried: the data itself is wrong, so the client has
    to submit a corrected request instead.
    """
    job = get_job(db, job_id)

    # Conditional UPDATE = atomic state transition: only one concurrent retry
    # request can move the job out of a terminal state.
    terminal = [s for s in JobStatus if s.is_terminal]
    reopened = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(terminal))
        .values(status=JobStatus.PENDING, error=None, completed_at=None)
    )
    if reopened.rowcount == 0:
        db.rollback()
        raise ConflictError(f"Job is still {job.status.value}; retry it once it has finished.")

    db.execute(
        update(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.FAILED)
        .values(status=CertificateStatus.PENDING, errors=None)
    )
    pending = db.scalar(
        select(func.count())
        .select_from(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.PENDING)
    )
    if not pending:
        db.rollback()
        raise ConflictError("Job has no failed certificates to retry.")

    db.commit()
    db.refresh(job)
    return job
