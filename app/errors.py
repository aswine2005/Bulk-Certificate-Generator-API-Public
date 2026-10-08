"""Domain errors raised by the service layer and mapped to HTTP responses in main.py."""

from typing import Any


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail: str, **extra: Any):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


class UnprocessableError(ServiceError):
    status_code = 422
