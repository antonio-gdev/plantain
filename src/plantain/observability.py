"""Redacting console and JSON-lines logging configuration."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING, Any, Protocol, cast

from plantain.errors import AtomicPersistenceError
from plantain.persistence import ensure_private_directory, ensure_private_file
from plantain.security.redaction import RedactionPolicy

if TYPE_CHECKING:
    from plantain.security.secrets import SecretRegistry

LOGGER_NAME = "plantain"
EVENT_LOGGER_NAME = f"{LOGGER_NAME}.events"
EVENT_LOG_RELATIVE_PATH = Path("event") / "splunk-event-json.log"
EVENT_LOG_MAX_BYTES = 100 * 1024 * 1024
EVENT_LOG_BACKUP_COUNT = 7
MAX_EVENT_TEXT_LENGTH = 500
DIAGNOSTIC_LOG_RANDOM_BYTES = 8
DIAGNOSTIC_LOG_FAILURE_MESSAGE = "ERROR Detailed scenario log could not be persisted\n"

_DIAGNOSTIC_FIELDS = (
    "correlation_id",
    "scenario",
    "activity",
    "step_id",
    "duration_ms",
    "status",
    "exception_type",
    "safe_exception",
    "http_status",
    "http_method",
    "response_bytes",
    "response_body",
    "response_body_truncated",
    "failure_stage",
    "schema_path",
    "schema_rule",
    "tags",
    "integration",
    "publication_status",
    "attachment_status",
    "browser",
    "provisioning_status",
    "ui_phase",
    "operation_index",
    "operation_total",
    "operation_type",
    "operation_target",
    "operation_input",
    "operation_expected",
    "operation_actual",
    "operation_error_type",
    "snapshot_activity",
    "snapshot_status",
)

_EVENT_FIELDS = (
    "correlation_id",
    "environment",
    "scenario",
    "source_path",
    "status",
    "duration_ms",
    "jira_ticket",
    "test_case_key",
    "test_run_key",
    "tags",
    "steps_total",
    "steps_passed",
    "steps_failed",
    "failed_activity",
    "failed_step_id",
    "failure_stage",
    "exception_type",
    "failure_reason",
    "operation_index",
    "operation_total",
    "operation_type",
    "operation_target",
    "operation_error_type",
)


class _StepLike(Protocol):
    @property
    def status(self) -> str: ...


class _FailureLike(Protocol):
    @property
    def activity(self) -> str: ...

    @property
    def step_id(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def exception_type(self) -> str: ...

    @property
    def details(self) -> Mapping[str, object] | None: ...


class ScenarioResultLike(Protocol):
    @property
    def correlation_id(self) -> str: ...

    @property
    def scenario(self) -> str: ...

    @property
    def source_path(self) -> str | None: ...

    @property
    def status(self) -> str: ...

    @property
    def duration_ms(self) -> int: ...

    @property
    def jira_ticket(self) -> str | None: ...

    @property
    def test_case_key(self) -> str | None: ...

    @property
    def test_run_key(self) -> str | int | None: ...

    @property
    def tags(self) -> Sequence[str]: ...

    @property
    def steps(self) -> Sequence[_StepLike]: ...

    @property
    def failure(self) -> _FailureLike | None: ...


_ACTIVE_LOG_SECRETS: ContextVar[SecretRegistry | None] = ContextVar(
    "plantain_active_log_secrets",
    default=None,
)
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "message",
}


def bind_log_secrets(secrets: SecretRegistry) -> Token[SecretRegistry | None]:
    """Bind one scenario's value-aware sanitizer to the current async context."""

    return _ACTIVE_LOG_SECRETS.set(secrets)


def reset_log_secrets(token: Token[SecretRegistry | None]) -> None:
    """Restore the previous sanitizer without affecting concurrent scenarios."""

    _ACTIVE_LOG_SECRETS.reset(token)


def _redact_log_value(policy: RedactionPolicy, value: Any) -> Any:
    secrets = _ACTIVE_LOG_SECRETS.get()
    return policy.redact_log(value) if secrets is None else secrets.redact_log(value)


class RedactingFilter(logging.Filter):
    """Sanitize messages, arguments, and structured fields before formatting."""

    def __init__(self, policy: RedactionPolicy) -> None:
        super().__init__()
        self._policy = policy

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_log_value(self._policy, record.msg)
        if isinstance(record.args, dict):
            record.args = cast(
                "dict[str, Any]",
                _redact_log_value(self._policy, record.args),
            )
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_log_value(self._policy, item) for item in record.args)
        for key, value in tuple(vars(record).items()):
            if key not in _STANDARD_LOG_RECORD_FIELDS:
                setattr(record, key, _redact_log_value(self._policy, value))
        if record.stack_info:
            record.stack_info = str(_redact_log_value(self._policy, record.stack_info))
        record.exc_text = None
        return True


class SafeConsoleFormatter(logging.Formatter):
    """Console formatter that also sanitizes exception text produced by logging."""

    def __init__(self, policy: RedactionPolicy) -> None:
        super().__init__("%(levelname)s %(message)s")
        self._policy = policy

    def formatException(self, exc_info: Any) -> str:  # noqa: N802
        return str(_redact_log_value(self._policy, super().formatException(exc_info)))


class JsonFormatter(logging.Formatter):
    """Minimal structured formatter for machine-readable execution telemetry."""

    def __init__(self, policy: RedactionPolicy) -> None:
        super().__init__()
        self._policy = policy

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _DIAGNOSTIC_FIELDS:
            if hasattr(record, key):
                payload[key] = _redact_log_value(self._policy, getattr(record, key))
        if record.exc_info:
            payload["exception"] = _redact_log_value(
                self._policy,
                super().formatException(record.exc_info),
            )
        return json.dumps(
            _redact_log_value(self._policy, payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )


class EventJsonFormatter(logging.Formatter):
    """Strict terminal-event schema for continuous telemetry ingestion."""

    def __init__(self, policy: RedactionPolicy) -> None:
        super().__init__()
        self._policy = policy

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "schema_version": 1,
            "event_type": "scenario.completed",
            "message": record.getMessage(),
        }
        for key in _EVENT_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(
            _redact_log_value(self._policy, payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _prepare_private_log(handler: logging.FileHandler) -> Path:
    path = Path(handler.baseFilename)
    ensure_private_directory(path.parent)
    ensure_private_file(path, missing_ok=True)
    return path


def _verify_private_log(
    path: Path,
    stream: TextIOWrapper[Any],
) -> TextIOWrapper[Any]:
    try:
        ensure_private_file(path)
    except AtomicPersistenceError:
        stream.close()
        raise
    return stream


class PrivateFileHandler(logging.FileHandler):
    """Create detailed evidence privately and report write failures safely."""

    def _open(self) -> TextIOWrapper[Any]:
        path = _prepare_private_log(self)
        return _verify_private_log(path, super()._open())

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        del record
        stream = sys.stderr
        if stream is None:
            return
        with suppress(OSError, ValueError):
            stream.write(DIAGNOSTIC_LOG_FAILURE_MESSAGE)
            stream.flush()


class FailClosedRotatingFileHandler(RotatingFileHandler):
    """Surface telemetry persistence failures without exposing OS error details."""

    def _open(self) -> TextIOWrapper[Any]:
        path = _prepare_private_log(self)
        return _verify_private_log(path, super()._open())

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        del record
        raise OSError("Scenario telemetry could not be persisted") from None


def _replace_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def configure_logging(
    output_dir: Path,
    level: str = "INFO",
    *,
    sensitive_keys: tuple[str, ...] = (),
) -> Path:
    """Configure idempotent redacting logs and return the JSON log path."""
    ensure_private_directory(output_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    log_name = f"logger-{timestamp}-p{os.getpid()}-{token_hex(DIAGNOSTIC_LOG_RANDOM_BYTES)}.jsonl"
    log_path = output_dir / log_name
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    _replace_handlers(logger)

    policy = RedactionPolicy(sensitive_keys)
    redactor = RedactingFilter(policy)
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(SafeConsoleFormatter(policy))
    console.addFilter(redactor)

    file_handler = PrivateFileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(JsonFormatter(policy))
    file_handler.addFilter(redactor)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    event_dir = output_dir / EVENT_LOG_RELATIVE_PATH.parent
    ensure_private_directory(event_dir)
    event_logger = logging.getLogger(EVENT_LOGGER_NAME)
    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False
    _replace_handlers(event_logger)
    event_handler = FailClosedRotatingFileHandler(
        output_dir / EVENT_LOG_RELATIVE_PATH,
        maxBytes=EVENT_LOG_MAX_BYTES,
        backupCount=EVENT_LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    event_handler.setLevel(logging.INFO)
    event_handler.setFormatter(EventJsonFormatter(policy))
    event_handler.addFilter(RedactingFilter(policy))
    event_logger.addHandler(event_handler)
    return log_path


def get_logger(name: str | None = None) -> logging.Logger:
    suffix = f".{name}" if name else ""
    return logging.getLogger(f"{LOGGER_NAME}{suffix}")


def get_event_logger() -> logging.Logger:
    """Return the isolated terminal-scenario telemetry logger."""

    return logging.getLogger(EVENT_LOGGER_NAME)


def emit_scenario_completed(
    result: ScenarioResultLike,
    *,
    environment: str,
    secrets: SecretRegistry,
) -> None:
    """Emit exactly one bounded terminal record without test data or stack traces."""

    fields: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "environment": _safe_event_text(secrets, environment),
        "scenario": _safe_event_text(secrets, result.scenario),
        "source_path": _safe_optional_event_text(secrets, result.source_path),
        "status": result.status,
        "duration_ms": result.duration_ms,
        "jira_ticket": _safe_optional_event_text(secrets, result.jira_ticket),
        "test_case_key": _safe_optional_event_text(secrets, result.test_case_key),
        "test_run_key": result.test_run_key,
        "tags": [_safe_event_text(secrets, tag, limit=64) for tag in result.tags],
        "steps_total": len(result.steps),
        "steps_passed": sum(step.status == "passed" for step in result.steps),
        "steps_failed": sum(step.status == "failed" for step in result.steps),
    }
    if result.failure is not None:
        fields.update(_event_failure_fields(result.failure, secrets))
    get_event_logger().info(
        "Scenario completed",
        extra=cast("dict[str, object]", secrets.redact_log(fields)),
    )


def _event_failure_fields(
    failure: _FailureLike,
    secrets: SecretRegistry,
) -> dict[str, object]:
    fields: dict[str, object] = {
        "failed_activity": _safe_event_text(secrets, failure.activity),
        "failed_step_id": _safe_event_text(secrets, failure.step_id),
        "exception_type": _safe_event_text(secrets, failure.exception_type),
        "failure_reason": _safe_event_text(secrets, failure.message),
    }
    details = failure.details or {}
    for key in (
        "failure_stage",
        "operation_index",
        "operation_total",
        "operation_type",
        "operation_target",
        "operation_error_type",
    ):
        value = details.get(key)
        if type(value) is int:
            fields[key] = value
        elif isinstance(value, str):
            fields[key] = _safe_event_text(secrets, value)
    return fields


def _safe_optional_event_text(
    secrets: SecretRegistry,
    value: object | None,
) -> str | None:
    return None if value is None else _safe_event_text(secrets, value)


def _safe_event_text(
    secrets: SecretRegistry,
    value: object,
    *,
    limit: int = MAX_EVENT_TEXT_LENGTH,
) -> str:
    sanitized = secrets.redact_text(str(value))
    return " ".join(sanitized.split())[:limit]
