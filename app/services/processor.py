"""Background generation of a job's certificates.

Flow for one job:
  1. Claim it: atomically move PENDING -> PROCESSING (no double processing).
  2. Repeatedly take the next batch of PENDING certificates, generate each one
     inside its own try/except, and commit the batch (= progress update).
  3. Derive the final job status from the per-certificate results.

A failure in one certificate only marks that certificate FAILED; the loop moves
on to the next one.
"""

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.renderer import CertificateData, CertificateRenderer
from app.storage import LocalStorage

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 500


class JobProcessor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        renderer: CertificateRenderer,
        storage: LocalStorage,
        public_base_url: str,
        batch_size: int = 50,
        stop_event: threading.Event | None = None,
    ):
        self.session_factory = session_factory
        self.renderer = renderer
        self.storage = storage
        self.public_base_url = public_base_url.rstrip("/")
        self.batch_size = batch_size
        # Set on shutdown: workers stop after the current batch, and the job is
        # resumed on the next start-up (see recover_interrupted_jobs).
        self.stop_event = stop_event or threading.Event()

    def __call__(self, job_id: str) -> None:
        self.process(job_id)

    def process(self, job_id: str) -> None:
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                logger.info("Job %s is not pending (already claimed or finished); skipping", job_id)
                return

            logger.info("Job %s: processing started", job_id)
            try:
                finished = self._generate_pending(db, job_id)
                if finished:
                    self._finalize(db, job_id)
            except Exception as exc:
                # Something outside a single certificate broke (e.g. the database).
                logger.exception("Job %s: unexpected error, marking job as failed", job_id)
                db.rollback()
                self._mark_failed(db, job_id, exc)

    # -- steps ----------------------------------------------------------------

    def _claim(self, db: Session, job_id: str) -> bool:
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.PENDING)
            .values(status=JobStatus.PROCESSING, started_at=_now(), error=None)
        )
        db.commit()
        return result.rowcount == 1

    def _generate_pending(self, db: Session, job_id: str) -> bool:
        """Generate certificates batch by batch. Returns False if stopped early."""
        job = db.get(Job, job_id)
        while True:
            if self.stop_event.is_set():
                logger.info("Job %s: shutdown requested, will resume on restart", job_id)
                return False

            batch = db.scalars(
                select(Certificate)
                .where(
                    Certificate.job_id == job_id, Certificate.status == CertificateStatus.PENDING
                )
                .order_by(Certificate.row_index)
                .limit(self.batch_size)
            ).all()
            if not batch:
                return True

            for cert in batch:
                self._generate_one(job, cert)
            db.commit()  # one commit per batch == one progress update

    def _generate_one(self, job: Job, cert: Certificate) -> None:
        cert.attempts += 1
        try:
            content = self.renderer.render(self._certificate_data(job, cert))
            key = self.storage.save(job.id, cert.id, content, self.renderer.file_extension)
        except Exception as exc:
            # Isolate the failure to this certificate and keep going.
            logger.warning(
                "Job %s: certificate %s (row %s) failed: %r", job.id, cert.id, cert.row_index, exc
            )
            cert.status = CertificateStatus.FAILED
            cert.errors = [{"field": None, "message": _describe(exc)}]
            return

        cert.status = CertificateStatus.GENERATED
        cert.file_path = key
        cert.file_size_bytes = len(content)
        cert.generated_at = _now()
        cert.errors = None

    def _finalize(self, db: Session, job_id: str) -> None:
        counts = dict(
            db.execute(
                select(Certificate.status, func.count())
                .where(Certificate.job_id == job_id)
                .group_by(Certificate.status)
            ).all()
        )
        status = final_status(counts)
        db.execute(update(Job).where(Job.id == job_id).values(status=status, completed_at=_now()))
        db.commit()
        logger.info(
            "Job %s: finished with status %s %s",
            job_id,
            status.value,
            {s.value: n for s, n in counts.items()},
        )

    def _mark_failed(self, db: Session, job_id: str, exc: Exception) -> None:
        try:
            db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status=JobStatus.FAILED, error=_describe(exc), completed_at=_now())
            )
            db.commit()
        except Exception:
            logger.exception("Job %s: could not record failure", job_id)

    def _certificate_data(self, job: Job, cert: Certificate) -> CertificateData:
        return CertificateData(
            recipient_name=cert.recipient_name,
            title=job.title,
            course_name=job.course_name,
            issuer_name=job.issuer_name,
            issue_date=job.issue_date,
            description=job.description,
            signatory_name=job.signatory_name,
            signatory_title=job.signatory_title,
            verification_code=cert.verification_code,
            verification_url=f"{self.public_base_url}/api/v1/verify/{cert.verification_code}",
        )


def final_status(counts: dict[CertificateStatus, int]) -> JobStatus:
    generated = counts.get(CertificateStatus.GENERATED, 0)
    problems = counts.get(CertificateStatus.FAILED, 0) + counts.get(CertificateStatus.INVALID, 0)
    if generated == 0:
        return JobStatus.FAILED
    if problems:
        return JobStatus.COMPLETED_WITH_ERRORS
    return JobStatus.COMPLETED


def recover_interrupted_jobs(session_factory: sessionmaker[Session]) -> list[str]:
    """Return the IDs of jobs that should be (re)queued at start-up.

    A job still marked PROCESSING when the app starts was interrupted (crash or
    shutdown). Its finished certificates are already committed, so putting it
    back to PENDING makes the worker continue with only the remaining ones.

    Assumes a single app instance owns the worker pool (see README).
    """
    with session_factory() as db:
        db.execute(
            update(Job).where(Job.status == JobStatus.PROCESSING).values(status=JobStatus.PENDING)
        )
        db.commit()
        return list(
            db.scalars(
                select(Job.id).where(Job.status == JobStatus.PENDING).order_by(Job.created_at)
            )
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _describe(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return message[:MAX_ERROR_LENGTH]
