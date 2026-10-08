"""Endpoints for bulk generation jobs."""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import get_db, get_queue, get_settings, get_storage
from app.config import Settings
from app.errors import ConflictError, NotFoundError
from app.models import CertificateStatus, JobStatus
from app.schemas import (
    API_PREFIX,
    CertificateOut,
    CertificatePage,
    ErrorOut,
    JobCreate,
    JobCreatedOut,
    JobOut,
    JobPage,
)
from app.services import job_service
from app.services.archive import build_job_archive, slugify
from app.storage import LocalStorage
from app.worker import JobQueue

router = APIRouter(prefix="/jobs", tags=["Jobs"])

DbSession = Annotated[Session, Depends(get_db)]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreatedOut,
    summary="Submit a bulk certificate generation job",
    responses={
        200: {
            "description": "Idempotent replay: the job created earlier with this "
            "Idempotency-Key is returned.",
            "model": JobCreatedOut,
        },
        409: {"description": "Idempotency-Key reused with a different body.", "model": ErrorOut},
        422: {
            "description": "Invalid request, or no valid recipients / strict mode "
            "rejected invalid rows.",
            "model": ErrorOut,
        },
    },
)
def create_job(
    payload: JobCreate,
    response: Response,
    db: DbSession,
    queue: Annotated[JobQueue, Depends(get_queue)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=255,
            description="Optional. Retrying with the same key returns the original job "
            "instead of creating a duplicate.",
        ),
    ] = None,
) -> JobCreatedOut:
    """Validates every recipient, stores the job and returns **202 Accepted**
    immediately. Certificates are generated in the background; poll
    `GET /jobs/{job_id}` for progress."""
    result = job_service.create_job(db, payload, settings, idempotency_key)
    job = result.job

    # Snapshot the response before handing the job to a worker so the client
    # sees a consistent "accepted" state.
    progress = job_service.get_progress(db, [job.id])[job.id]
    body = JobCreatedOut.build(job, progress, invalid_recipients=result.invalid_recipients)

    if result.created:
        queue.enqueue(job.id)
    else:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"{API_PREFIX}/jobs/{job.id}"
    return body


@router.get("", response_model=JobPage, summary="List jobs (newest first)")
def list_jobs(
    db: DbSession,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobPage:
    total, jobs = job_service.list_jobs(db, status_filter, limit, offset)
    progress = job_service.get_progress(db, [j.id for j in jobs])
    items = [JobOut.build(j, progress[j.id]) for j in jobs]
    return JobPage(total=total, limit=limit, offset=offset, items=items)


@router.get(
    "/{job_id}",
    response_model=JobOut,
    summary="Get job status and progress",
    responses={404: {"model": ErrorOut}},
)
def get_job(job_id: str, db: DbSession) -> JobOut:
    job = job_service.get_job(db, job_id)
    progress = job_service.get_progress(db, [job.id])[job.id]
    return JobOut.build(job, progress)


@router.get(
    "/{job_id}/certificates",
    response_model=CertificatePage,
    summary="List the per-recipient results of a job",
    responses={404: {"model": ErrorOut}},
)
def list_job_certificates(
    job_id: str,
    db: DbSession,
    status_filter: Annotated[
        CertificateStatus | None,
        Query(alias="status", description="e.g. `failed` or `invalid` to see only problems."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CertificatePage:
    total, certs = job_service.list_certificates(db, job_id, status_filter, limit, offset)
    return CertificatePage(
        total=total, limit=limit, offset=offset, items=[CertificateOut.build(c) for c in certs]
    )


@router.get(
    "/{job_id}/download",
    summary="Download all generated certificates as a ZIP",
    response_class=FileResponse,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "ZIP with `certificates/*.pdf` and a `manifest.csv` "
            "describing every submitted row.",
        },
        404: {"model": ErrorOut},
        409: {"description": "Job has not finished yet.", "model": ErrorOut},
    },
)
def download_job(
    job_id: str,
    db: DbSession,
    storage: Annotated[LocalStorage, Depends(get_storage)],
) -> FileResponse:
    job = job_service.get_job(db, job_id)
    if not job.status.is_terminal:
        raise ConflictError(f"Job is still {job.status.value}; download it once it has finished.")
    progress = job_service.get_progress(db, [job.id])[job.id]
    if progress.generated == 0:
        raise NotFoundError("Job has no generated certificates to download.")

    path = build_job_archive(db, job, storage)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"certificates-{slugify(job.course_name, 40)}-{job.id[:8]}.zip",
        background=BackgroundTask(os.remove, path),  # delete temp file after sending
    )


@router.post(
    "/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobOut,
    summary="Retry the failed certificates of a finished job",
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}},
)
def retry_job(
    job_id: str,
    db: DbSession,
    queue: Annotated[JobQueue, Depends(get_queue)],
) -> JobOut:
    """Re-queues certificates with status `failed`. Rows with status `invalid`
    are not retried: the input itself is wrong and must be corrected."""
    job = job_service.retry_failed(db, job_id)
    progress = job_service.get_progress(db, [job.id])[job.id]
    body = JobOut.build(job, progress)
    queue.enqueue(job.id)
    return body
