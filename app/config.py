"""Application settings, loaded from environment variables (or a local .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Bulk Certificate Generator"

    # Any SQLAlchemy URL works. SQLite is the zero-setup default; PostgreSQL is
    # used in docker-compose, e.g. postgresql+psycopg://user:pass@db:5432/certs
    database_url: str = "sqlite:///./data/app.db"

    # Where generated PDFs are written: <storage_dir>/<job_id>/<certificate_id>.pdf
    storage_dir: Path = Path("./data/certificates")

    # Base URL encoded into each certificate's QR code for public verification.
    public_base_url: str = "http://localhost:8000"

    # "thread": jobs run on a background thread pool (default).
    # "inline": jobs run synchronously inside the request (handy for debugging).
    queue_backend: Literal["thread", "inline"] = "thread"
    worker_threads: int = Field(default=2, ge=1, le=32)

    # Certificates are generated and committed in batches of this size, which is
    # also how often job progress becomes visible to clients.
    batch_size: int = Field(default=50, ge=1, le=1000)

    max_recipients_per_job: int = Field(default=10_000, ge=1)

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
