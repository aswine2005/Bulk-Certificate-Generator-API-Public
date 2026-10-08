"""Test data builders and helpers."""

from collections.abc import Callable, Iterable
from typing import Any

from app.services.renderer import CertificateData


def recipient(name: str = "Ada Lovelace", email: str | None = None, **extra: Any) -> dict:
    email = email or f"{name.lower().replace(' ', '.')}@example.com"
    return {"name": name, "email": email, **extra}


def recipients(count: int) -> list[dict]:
    return [
        recipient(f"Person {chr(65 + i % 26)}{i}", f"person{i}@example.com") for i in range(count)
    ]


def job_payload(rows: list[dict] | None = None, **certificate_overrides: Any) -> dict:
    validation_mode = certificate_overrides.pop("validation_mode", None)
    payload: dict[str, Any] = {
        "certificate": {
            "course_name": "Advanced Backend Engineering",
            "issuer_name": "Acme Academy",
            "issue_date": "2026-10-08",
            "signatory_name": "Priya Raman",
            "signatory_title": "Director of Learning",
            **certificate_overrides,
        },
        "recipients": rows if rows is not None else recipients(3),
    }
    if validation_mode:
        payload["validation_mode"] = validation_mode
    return payload


def failing_render(
    original: Callable[[CertificateData], bytes],
    names: Iterable[str],
    exc: Exception | None = None,
    times: int | None = None,
) -> Callable:
    """Wrap ``CertificateRenderer.render`` so it raises for the given recipients.

    ``times`` limits how often each name fails, to simulate a transient error.
    """
    names = set(names)
    failures: dict[str, int] = {}

    def render(data: CertificateData) -> bytes:
        name = data.recipient_name
        if name in names and (times is None or failures.get(name, 0) < times):
            failures[name] = failures.get(name, 0) + 1
            raise exc or RuntimeError(f"simulated failure for {name}")
        return original(data)

    return render
