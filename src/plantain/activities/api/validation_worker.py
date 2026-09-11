"""Safe transport for API validation across the killable worker boundary."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from plantain.activities.api.client import ApiActivityError
from plantain.engine.admission import ResourceAdmission
from plantain.engine.process_worker import (
    ProcessLimits,
    ProcessWorkerError,
    ProcessWorkerTimeoutError,
)

_MEBIBYTE = 1_048_576
_MAX_SAFE_MESSAGE_LENGTH = 500
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ApiValidationOutcome:
    """Value-free validation result safe to return over local IPC."""

    error_type: str | None = None
    message: str | None = None
    safe_details: dict[str, str | int] = field(default_factory=dict)


def capture_api_validation(
    validator: Callable[..., None],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> ApiValidationOutcome:
    """Capture only explicitly safe framework validation failures."""

    try:
        validator(*args, **kwargs)
    except ApiActivityError as exc:
        message = _CONTROL_CHARACTER.sub(" ", str(exc))[:_MAX_SAFE_MESSAGE_LENGTH]
        return ApiValidationOutcome(
            error_type=type(exc).__name__,
            message=message or "API schema validation failed safely",
            safe_details=dict(exc.safe_details),
        )
    return ApiValidationOutcome()


async def run_isolated_api_validation(
    admission: ResourceAdmission,
    settings: object,
    validator: Callable[..., None],
    /,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Validate in a bounded process and reconstruct safe parent exceptions."""

    # Defaults preserve compatibility with lightweight test settings and external embeddings.
    limits = ProcessLimits(
        wall_timeout_seconds=float(getattr(settings, "schema_validation_timeout_seconds", 10.0)),
        cpu_timeout_seconds=int(getattr(settings, "schema_validation_cpu_seconds", 5)),
        memory_bytes=int(getattr(settings, "schema_validation_memory_mib", 512)) * _MEBIBYTE,
    )
    try:
        outcome = await admission.run_process(
            capture_api_validation,
            validator,
            tuple(args),
            dict(kwargs),
            limits=limits,
        )
    except ProcessWorkerTimeoutError as exc:
        raise ApiActivityError(
            "Schema validation exceeded PLANTAIN_SCHEMA_VALIDATION_TIMEOUT_SECONDS",
            safe_details={"failure_stage": "schema_validation_worker"},
        ) from exc
    except ProcessWorkerError as exc:
        raise ApiActivityError(
            f"Schema validation worker failed safely ({exc.failure_type})",
            safe_details={"failure_stage": "schema_validation_worker"},
        ) from exc
    _raise_outcome(outcome)


def _raise_outcome(outcome: ApiValidationOutcome) -> None:
    if outcome.error_type is None:
        return
    message = outcome.message or "API schema validation failed safely"
    if outcome.error_type == "SchemaConformanceError":
        from plantain.activities.api import schema_validation  # noqa: PLC0415

        raise schema_validation.SchemaConformanceError(
            message,
            safe_details=outcome.safe_details,
        )
    raise ApiActivityError(message, safe_details=outcome.safe_details)


__all__ = [
    "ApiValidationOutcome",
    "capture_api_validation",
    "run_isolated_api_validation",
]
