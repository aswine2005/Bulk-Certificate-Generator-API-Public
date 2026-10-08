"""Endpoints for individual certificates and public verification."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_storage
from app.errors import ConflictError, NotFoundError
from app.models import Certificate, CertificateStatus
from app.schemas import CertificateOut, ErrorOut, VerificationOut
from app.services import job_service
from app.services.archive import certificate_filename
from app.storage import LocalStorage

router = APIRouter(tags=["Certificates"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get(
    "/certificates/{certificate_id}",
    response_model=CertificateOut,
    summary="Get one certificate's status and metadata",
    responses={404: {"model": ErrorOut}},
)
def get_certificate(certificate_id: str, db: DbSession) -> CertificateOut:
    return CertificateOut.build(job_service.get_certificate(db, certificate_id))


@router.get(
    "/certificates/{certificate_id}/download",
    summary="Download one certificate as PDF",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {}}},
        404: {"model": ErrorOut},
        409: {
            "description": "Certificate was not generated (pending/failed/invalid).",
            "model": ErrorOut,
        },
    },
)
def download_certificate(
    certificate_id: str,
    db: DbSession,
    storage: Annotated[LocalStorage, Depends(get_storage)],
) -> FileResponse:
    cert = job_service.get_certificate(db, certificate_id)
    if cert.status != CertificateStatus.GENERATED:
        raise ConflictError(f"Certificate is {cert.status.value}, not generated.")
    if not cert.file_path or not storage.exists(cert.file_path):
        raise NotFoundError("Certificate file is missing from storage.")
    return FileResponse(
        storage.path_for(cert.file_path),
        media_type="application/pdf",
        filename=certificate_filename(cert),
    )


@router.get(
    "/verify/{verification_code}",
    response_model=VerificationOut,
    tags=["Verification"],
    summary="Verify a certificate by the code printed on it / encoded in its QR code",
    responses={404: {"model": ErrorOut}},
)
def verify_certificate(verification_code: str, db: DbSession) -> VerificationOut:
    code = job_service.normalize_verification_code(verification_code)
    cert = db.scalar(select(Certificate).where(Certificate.verification_code == code))
    if cert is None or cert.status != CertificateStatus.GENERATED:
        raise NotFoundError("No valid certificate matches this verification code.")
    job = cert.job
    return VerificationOut(
        valid=True,
        verification_code=code,
        certificate_id=cert.id,
        recipient_name=cert.recipient_name,
        title=job.title,
        course_name=job.course_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        generated_at=cert.generated_at,
    )
