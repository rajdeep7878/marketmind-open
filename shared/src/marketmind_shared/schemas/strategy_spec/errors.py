"""Custom error types for strategy spec validation.

We wrap Pydantic's ValidationError so the rest of the system can rely on
a stable shape: an error_code (a slug a tester or UI can match against),
a field_path (dotted location), and a message. Custom validators raise
PydanticCustomError with a slug as `type`, which becomes the error_code
when we convert at the boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySpecValidationError:
    """A single, user-facing validation problem."""

    error_code: str
    field_path: str
    message: str

    def __str__(self) -> str:
        location = self.field_path if self.field_path else "<root>"
        return f"[{self.error_code}] {location}: {self.message}"


class StrategySpecValidationErrorGroup(Exception):  # noqa: N818  (group-of-errors naming, not a singular *Error)
    """Raised when validate_spec encounters one or more errors.

    Exposes `errors` as the ordered list of problems found. The string form
    summarises count + first error so logs are useful without iterating.
    """

    def __init__(self, errors: Sequence[StrategySpecValidationError]) -> None:
        self._errors: tuple[StrategySpecValidationError, ...] = tuple(errors)
        first = self._errors[0] if self._errors else None
        summary = f"{len(self._errors)} validation error(s)"
        if first is not None:
            summary = f"{summary}; first: {first}"
        super().__init__(summary)

    @property
    def errors(self) -> tuple[StrategySpecValidationError, ...]:
        return self._errors

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._errors)


__all__ = [
    "StrategySpecValidationError",
    "StrategySpecValidationErrorGroup",
]
