"""Application factory and entry point (``uvicorn app.main:app``)."""

import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import certificates, jobs
from app.config import Settings, get_settings
from app.database import create_db_engine, create_session_factory, init_db
from app.errors import ServiceError
from app.services.processor import JobProcessor, recover_interrupted_jobs
from app.services.renderer import CertificateRenderer
from app.storage import LocalStorage
from app.worker import InlineJobQueue, JobHandler, JobQueue, ThreadPoolJobQueue

logger = logging.getLogger(__name__)

QueueFactory = Callable[[JobHandler], JobQueue]

DESCRIPTION = """
Generate certificates for many recipients with **one request**.

1. `POST /api/v1/jobs` with the certificate details and a list of recipients
   (**202 Accepted**; each recipient row is validated individually).
2. Poll `GET /api/v1/jobs/{job_id}` for status and progress.
3. Inspect per-recipient results with `GET /api/v1/jobs/{job_id}/certificates`.
4. Download one PDF (`GET /api/v1/certificates/{id}/download`) or everything as a
   ZIP (`GET /api/v1/jobs/{job_id}/download`).
"""


def create_app(
    settings: Settings | None = None, queue_factory: QueueFactory | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    stop_event = threading.Event()
    processor = JobProcessor(
        session_factory=session_factory,
        renderer=CertificateRenderer(),
        storage=storage,
        public_base_url=settings.public_base_url,
        batch_size=settings.batch_size,
        stop_event=stop_event,
    )
    if queue_factory is None:
        queue_factory = _default_queue_factory(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        storage.root.mkdir(parents=True, exist_ok=True)
        queue = queue_factory(processor)
        app.state.queue = queue

        # Resume anything that was queued or running when the app last stopped.
        recovered = recover_interrupted_jobs(session_factory)
        if recovered:
            logger.info("Re-queuing %d unfinished job(s) from a previous run", len(recovered))
        for job_id in recovered:
            queue.enqueue(job_id)

        yield

        stop_event.set()  # workers stop after their current batch
        queue.shutdown()
        engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.state.processor = processor

    app.include_router(jobs.router, prefix="/api/v1")
    app.include_router(certificates.router, prefix="/api/v1")

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        content = {"detail": exc.detail}
        for key, value in exc.extra.items():
            content[key] = [v.model_dump() for v in value] if isinstance(value, list) else value
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.get("/health", tags=["Health"], summary="Liveness check")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _default_queue_factory(settings: Settings) -> QueueFactory:
    if settings.queue_backend == "inline":
        return InlineJobQueue
    return lambda handler: ThreadPoolJobQueue(handler, max_workers=settings.worker_threads)


app = create_app()
