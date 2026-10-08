import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.config import Settings
from app.database import Base
from app.main import create_app
from app.worker import InlineJobQueue, JobHandler


class ManualQueue:
    """Records enqueued jobs; the test decides when they run.

    Lets tests observe a job in its PENDING state before any worker touches it.
    """

    def __init__(self, handler: JobHandler):
        self.handler = handler
        self.queued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.queued.append(job_id)

    def run_all(self) -> None:
        while self.queued:
            self.handler(self.queued.pop(0))

    def shutdown(self) -> None:
        pass


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """A fresh SQLite file per test by default.

    Set TEST_DATABASE_URL (e.g. postgresql+psycopg://...) to run the same suite
    against another database; its tables are dropped before every test.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    return url


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        storage_dir=tmp_path / "certificates",
        public_base_url="https://certs.example.com",
        batch_size=2,  # small batches so multi-batch behaviour is exercised
        log_level="WARNING",
    )


@pytest.fixture
def make_client(settings: Settings) -> Iterator[Callable[..., TestClient]]:
    """Factory for app clients that share one database and storage directory."""
    clients: list[TestClient] = []

    def _make(queue_factory=InlineJobQueue, **overrides) -> TestClient:
        app = create_app(settings.model_copy(update=overrides), queue_factory=queue_factory)
        client = TestClient(app)
        client.__enter__()  # runs the lifespan (create tables, recover jobs)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    """Jobs are processed synchronously inside the POST (InlineJobQueue)."""
    return make_client()


@pytest.fixture
def manual_client(make_client) -> TestClient:
    """Jobs are only processed when the test calls ``client.app.state.queue.run_all()``."""
    return make_client(queue_factory=ManualQueue)
