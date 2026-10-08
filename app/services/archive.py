"""Builds the ZIP archive returned by GET /jobs/{id}/download."""

import csv
import io
import re
import tempfile
import unicodedata
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, Job
from app.storage import LocalStorage

MANIFEST_COLUMNS = [
    "row_index",
    "status",
    "recipient_name",
    "recipient_email",
    "certificate_id",
    "verification_code",
    "file",
    "errors",
]


def slugify(value: str | None, max_length: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:max_length].rstrip("-") or "certificate"


def certificate_filename(cert: Certificate) -> str:
    return f"{slugify(cert.recipient_name)}-{cert.verification_code}.pdf"


def build_job_archive(db: Session, job: Job, storage: LocalStorage) -> Path:
    """Write every generated PDF plus a manifest.csv (one line per submitted row,
    including failed and invalid ones) to a temporary ZIP file and return its path.

    The archive is built on disk rather than in memory so that large jobs do not
    blow up the API process's memory. The caller deletes the file after sending.
    """
    # delete=False: the file must outlive this function; the endpoint removes it.
    tmp = tempfile.NamedTemporaryFile(prefix=f"job-{job.id}-", suffix=".zip", delete=False)  # noqa: SIM115
    try:
        with tmp, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _write_archive(db, job, storage, archive)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    return Path(tmp.name)


def _write_archive(db: Session, job: Job, storage: LocalStorage, archive: zipfile.ZipFile) -> None:
    manifest = io.StringIO()
    writer = csv.DictWriter(manifest, fieldnames=MANIFEST_COLUMNS)
    writer.writeheader()

    certificates = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job.id)
        .order_by(Certificate.row_index)
        .execution_options(yield_per=500)
    )
    for cert in certificates:
        entry_name = ""
        errors = "; ".join(
            f"{e['field']}: {e['message']}" if e.get("field") else e["message"]
            for e in cert.errors or []
        )
        if cert.status == CertificateStatus.GENERATED:
            if cert.file_path and storage.exists(cert.file_path):
                entry_name = f"certificates/{cert.row_index:05d}-{certificate_filename(cert)}"
                archive.write(storage.path_for(cert.file_path), arcname=entry_name)
            else:
                errors = "generated file is missing from storage"

        writer.writerow(
            {
                "row_index": cert.row_index,
                "status": cert.status.value,
                "recipient_name": cert.recipient_name or "",
                "recipient_email": cert.recipient_email or "",
                "certificate_id": cert.id,
                "verification_code": cert.verification_code or "",
                "file": entry_name,
                "errors": errors,
            }
        )

    # utf-8-sig adds a BOM so Excel shows accented names correctly.
    archive.writestr("manifest.csv", manifest.getvalue().encode("utf-8-sig"))
