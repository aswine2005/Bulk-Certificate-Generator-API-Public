"""Job queues: how a job ID gets from the API to the processor.

The API only depends on the ``JobQueue`` protocol, so moving to a distributed
queue (Celery, RQ, Arq, SQS, ...) means adding one class here; the API and the
processor stay as they are.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

logger = logging.getLogger(__name__)

JobHandler = Callable[[str], None]


class JobQueue(Protocol):
    def enqueue(self, job_id: str) -> None: ...

    def shutdown(self) -> None: ...


class ThreadPoolJobQueue:
    """Runs jobs on a bounded pool of background threads in the API process.

    The bound (``worker_threads``) caps how many jobs are generated at once, so
    a burst of large requests queues up instead of overloading the server.
    """

    def __init__(self, handler: JobHandler, max_workers: int):
        self._handler = handler
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="certificate-worker"
        )

    def enqueue(self, job_id: str) -> None:
        self._executor.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        try:
            self._handler(job_id)
        except Exception:  # never let a job kill the worker thread silently
            logger.exception("Unhandled error while processing job %s", job_id)

    def shutdown(self) -> None:
        # Jobs still waiting in the executor are dropped; they are still PENDING
        # in the database and get re-queued on the next start-up.
        self._executor.shutdown(wait=True, cancel_futures=True)


class InlineJobQueue:
    """Processes the job synchronously inside ``enqueue`` (debugging/tests)."""

    def __init__(self, handler: JobHandler):
        self._handler = handler

    def enqueue(self, job_id: str) -> None:
        self._handler(job_id)

    def shutdown(self) -> None:
        pass
