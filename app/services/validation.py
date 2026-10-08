"""Row-by-row recipient validation.

The request envelope (certificate details, list size) is validated strictly by
FastAPI/Pydantic. Recipients are validated here one at a time so that a single
bad row becomes a per-row error instead of failing the whole bulk request.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas import FieldError, RecipientIn


@dataclass(frozen=True)
class ValidRow:
    row_index: int
    recipient: RecipientIn


@dataclass(frozen=True)
class InvalidRow:
    row_index: int
    raw: dict[str, Any]
    errors: list[FieldError]


def validate_recipients(rows: list[dict[str, Any]]) -> tuple[list[ValidRow], list[InvalidRow]]:
    valid: list[ValidRow] = []
    invalid: list[InvalidRow] = []
    first_row_for_email: dict[str, int] = {}

    for index, raw in enumerate(rows):
        try:
            recipient = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            invalid.append(InvalidRow(index, raw, _to_field_errors(exc)))
            continue

        # One certificate per person per job: a repeated email is almost always
        # a copy/paste mistake in the source list.
        email_key = recipient.email.lower()
        if email_key in first_row_for_email:
            first = first_row_for_email[email_key]
            message = f"duplicate email; already used by the recipient at row_index {first}"
            invalid.append(InvalidRow(index, raw, [FieldError(field="email", message=message)]))
            continue

        first_row_for_email[email_key] = index
        valid.append(ValidRow(index, recipient))

    return valid, invalid


def _to_field_errors(exc: ValidationError) -> list[FieldError]:
    errors = []
    for err in exc.errors(include_url=False):
        field = ".".join(str(part) for part in err["loc"]) or None
        message = err["msg"]
        # Pydantic prefixes custom validator messages with "Value error, ".
        message = message.removeprefix("Value error, ")
        errors.append(FieldError(field=field, message=message))
    return errors
