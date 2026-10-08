"""FastAPI dependencies that read shared objects created in app.main.create_app."""

from fastapi import Request

from app.config import Settings
from app.database import get_db  # noqa: F401  (re-exported for routers)
from app.storage import LocalStorage
from app.worker import JobQueue


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_queue(request: Request) -> JobQueue:
    return request.app.state.queue


def get_storage(request: Request) -> LocalStorage:
    return request.app.state.storage
