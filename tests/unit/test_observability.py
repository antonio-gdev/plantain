"""Structured telemetry remains actionable without exposing sensitive values."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.observability import (
    DIAGNOSTIC_LOG_FAILURE_MESSAGE,
    EVENT_LOG_BACKUP_COUNT,
    EVENT_LOG_MAX_BYTES,
    EVENT_LOG_RELATIVE_PATH,
    EventJsonFormatter,
    JsonFormatter,
    PrivateFileHandler,
    RedactingFilter,
    bind_log_secrets,
    configure_logging,
    emit_scenario_completed,
    get_event_logger,
    get_logger,
    reset_log_secrets,
)
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE
from plantain.security.redaction import REDACTED, RedactionPolicy
from plantain.security.secrets import SecretRegistry

HTTP_OK = 200
RESPONSE_BYTES = 42
EXPECTED_STEP_TOTAL = 2
EXPECTED_DIAGNOSTIC_RANDOM_HEX_LENGTH = 16


def test_schema_failure_fields_are_structured_and_redacted() -> None:
    formatter = JsonFormatter(RedactionPolicy(("ip_address",)))
    record = logging.makeLogRecord(
        {
            "levelname": "ERROR",
            "name": "plantain.runner",
            "msg": "Activity failed: HTTP 200 response failed schema validation",
            "http_status": HTTP_OK,
            "http_method": "GET",
            "response_bytes": RESPONSE_BYTES,
            "response_body": {
                "message": "Denied",
                "api_key": "private-value",
            },
            "response_body_truncated": False,
            "failure_stage": "response_schema_validation",
            "schema_path": "45/ip_address",
            "schema_rule": "required",
        }
    )

    payload = cast("dict[str, Any]", json.loads(formatter.format(record)))

    assert payload["http_status"] == HTTP_OK
    assert payload["http_method"] == "GET"
    assert payload["response_bytes"] == RESPONSE_BYTES
    assert payload["response_body"] == {
        "message": "Denied",
        "api_key": REDACTED,
    }
    assert payload["response_body_truncated"] is False
    assert payload["failure_stage"] == "response_schema_validation"
    assert payload["schema_path"] == REDACTED
    assert payload["schema_rule"] == "required"


def test_terminal_event_formatter_excludes_diagnostic_only_fields() -> None:
    formatter = EventJsonFormatter(RedactionPolicy(()))
    record = logging.makeLogRecord(
        {
            "levelname": "INFO",
            "name": "plantain.events",
            "msg": "Scenario completed",
            "correlation_id": "run-1",
            "scenario": "Inventory checkout",
            "status": "failed",
            "failure_reason": "UI action 2/4 failed",
            "response_body": {"password": "must-never-enter-event-stream"},
            "operation_input": "must-never-enter-event-stream",
            "operation_expected": "must-never-enter-event-stream",
            "operation_actual": "must-never-enter-event-stream",
        }
    )

    payload = cast("dict[str, Any]", json.loads(formatter.format(record)))

    assert payload["schema_version"] == 1
    assert payload["event_type"] == "scenario.completed"
    assert payload["correlation_id"] == "run-1"
    assert "response_body" not in payload
    assert "operation_input" not in payload
    assert "operation_expected" not in payload
    assert "operation_actual" not in payload
    assert "exception" not in payload


def test_redacting_filter_uses_scenario_values_for_every_diagnostic_field() -> None:
    observed_value = "scenario-observed-secret"
    policy = RedactionPolicy(())
    secrets = SecretRegistry()
    secrets.observe_environment("APP_PASSWORD", observed_value)
    record = logging.makeLogRecord(
        {
            "levelname": "INFO",
            "name": "plantain.ui",
            "msg": "Entered %s",
            "args": (observed_value,),
            "operation_input": observed_value,
            "operation_actual": {"nested": observed_value},
            "response_body": {"nested": [observed_value]},
        }
    )

    token = bind_log_secrets(secrets)
    try:
        assert RedactingFilter(policy).filter(record)
        rendered = JsonFormatter(policy).format(record)
    finally:
        reset_log_secrets(token)

    payload = cast("dict[str, Any]", json.loads(rendered))
    assert observed_value not in rendered
    assert payload["message"] == f"Entered {REDACTED}"
    assert payload["operation_input"] == REDACTED
    assert payload["operation_actual"] == {"nested": REDACTED}
    assert payload["response_body"] == {"nested": [REDACTED]}


def test_scenario_log_secrets_are_isolated_across_concurrent_tasks() -> None:
    async def render(secret: str) -> str:
        secrets = SecretRegistry()
        secrets.observe_environment("APP_PASSWORD", secret)
        token = bind_log_secrets(secrets)
        try:
            await asyncio.sleep(0)
            record = logging.makeLogRecord(
                {
                    "levelname": "INFO",
                    "name": "plantain.concurrent",
                    "msg": "Entered %s",
                    "args": (secret,),
                    "operation_input": secret,
                }
            )
            policy = RedactionPolicy(())
            assert RedactingFilter(policy).filter(record)
            return JsonFormatter(policy).format(record)
        finally:
            reset_log_secrets(token)

    async def run_both() -> tuple[str, str]:
        return await asyncio.gather(
            render("first-scenario-secret"),
            render("second-scenario-secret"),
        )

    first, second = asyncio.run(run_both())
    for rendered in (first, second):
        assert "first-scenario-secret" not in rendered
        assert "second-scenario-secret" not in rendered
        assert REDACTED in rendered


def test_configure_logging_isolates_rotating_terminal_events(tmp_path: Path) -> None:
    observed_value = "event-secret-value"
    first_diagnostic_path = configure_logging(tmp_path, sensitive_keys=("ip_address",))
    diagnostic_path = configure_logging(tmp_path, sensitive_keys=("ip_address",))
    secrets = SecretRegistry(sensitive_keys=("ip_address",))
    secrets.observe_environment("APP_PASSWORD", observed_value)
    result = SimpleNamespace(
        correlation_id="run-2",
        scenario="Checkout",
        source_path="ui/checkout.yaml",
        status="failed",
        duration_ms=123,
        jira_ticket="QA-1",
        test_case_key="CASE-1",
        test_run_key="RUN-1",
        tags=["ui"],
        steps=[SimpleNamespace(status="passed"), SimpleNamespace(status="failed")],
        failure=SimpleNamespace(
            activity="capturePageSnapshot",
            step_id="checkout",
            message=f"Rejected {observed_value}",
            exception_type="UiActionError",
            details={
                "failure_stage": "ui_action",
                "operation_index": 2,
                "operation_total": 4,
                "operation_type": "click",
                "operation_target": f'css:[data-account="{observed_value}"]',
            },
        ),
    )
    try:
        get_logger("runner").info("Diagnostic-only progress")
        emit_scenario_completed(
            cast("Any", result),
            environment="qa",
            secrets=secrets,
        )

        event_path = tmp_path / EVENT_LOG_RELATIVE_PATH
        payload = cast(
            "dict[str, Any]",
            json.loads(event_path.read_text(encoding="utf-8").strip()),
        )
        diagnostic_text = diagnostic_path.read_text(encoding="utf-8")
        event_handler = get_event_logger().handlers[0]

        assert first_diagnostic_path != diagnostic_path
        assert f"-p{os.getpid()}-" in diagnostic_path.name
        assert (
            len(diagnostic_path.stem.rsplit("-", maxsplit=1)[-1])
            == EXPECTED_DIAGNOSTIC_RANDOM_HEX_LENGTH
        )
        assert isinstance(event_handler, RotatingFileHandler)
        assert event_handler.maxBytes == EVENT_LOG_MAX_BYTES
        assert event_handler.backupCount == EVENT_LOG_BACKUP_COUNT
        assert payload["failure_reason"] == f"Rejected {REDACTED}"
        assert payload["operation_target"] == f'css:[data-account="{REDACTED}"]'
        assert payload["steps_total"] == EXPECTED_STEP_TOTAL
        assert payload["steps_failed"] == 1
        assert observed_value not in event_path.read_text(encoding="utf-8")
        assert "Diagnostic-only progress" not in event_path.read_text(encoding="utf-8")
        assert "Scenario completed" not in diagnostic_text
        if os.name != "nt":
            assert stat.S_IMODE(tmp_path.stat().st_mode) == PRIVATE_DIRECTORY_MODE
            assert stat.S_IMODE(event_path.parent.stat().st_mode) == PRIVATE_DIRECTORY_MODE
            assert stat.S_IMODE(diagnostic_path.stat().st_mode) == PRIVATE_FILE_MODE
            assert stat.S_IMODE(event_path.stat().st_mode) == PRIVATE_FILE_MODE
    finally:
        for configured_logger in (get_event_logger(), get_logger()):
            for handler in list(configured_logger.handlers):
                handler.close()
                configured_logger.removeHandler(handler)


def test_detailed_log_write_failure_is_value_free_and_nonfatal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = PrivateFileHandler(tmp_path / "diagnostic.jsonl", encoding="utf-8")
    try:
        handler.handleError(logging.makeLogRecord({"msg": "private record value"}))
        error = capsys.readouterr().err
        assert error == DIAGNOSTIC_LOG_FAILURE_MESSAGE
        assert "private record value" not in error
    finally:
        handler.close()
