"""Dependency-free Allure result emission for Plantain CLI scenarios."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

from plantain.persistence import write_json_atomic
from plantain.security.secrets import SecretRegistry

ALLURE_RESULTS_DIRECTORY = "allure"
MAX_ALLURE_INTEGRATIONS = 16
MAX_ALLURE_PROVIDER_LENGTH = 64
MAX_ALLURE_TEXT_LENGTH = 2_000
MAX_ALLURE_VALUE_LENGTH = 500


def write_allure_result(
    output_dir: Path,
    result: Mapping[str, Any],
    *,
    environment: str,
    secrets: SecretRegistry,
) -> Path:
    """Write one sanitized Allure test-result file and return its path."""

    safe_result = cast("dict[str, Any]", secrets.redact(dict(result)))
    safe_environment = secrets.redact_text(environment)
    scenario = _safe_text(
        safe_result.get("scenario"),
        fallback="Plantain scenario",
    )
    source_path = _safe_text(safe_result.get("source_path"), fallback="")
    full_name = _full_name(source_path, scenario)
    started_at_ms = _integer(safe_result.get("started_at_ms"), default=0)
    duration_ms = max(
        _integer(safe_result.get("duration_ms"), default=0),
        0,
    )
    stopped_at_ms = started_at_ms + duration_ms
    status = _allure_status(safe_result.get("status"))
    result_uuid = str(uuid4())

    payload: dict[str, Any] = {
        "uuid": result_uuid,
        "historyId": _stable_id(
            "history",
            full_name,
            safe_environment,
        ),
        "testCaseId": _stable_id("test-case", full_name),
        "name": scenario,
        "fullName": full_name,
        "status": status,
        "stage": "finished",
        "start": started_at_ms,
        "stop": stopped_at_ms,
        "labels": _labels(
            safe_result,
            source_path,
            safe_environment,
        ),
        "parameters": _parameters(
            safe_result,
            safe_environment,
        ),
        "steps": _steps(
            safe_result,
            started_at_ms,
            stopped_at_ms,
        ),
    }

    status_details = _status_details(safe_result)
    if status_details:
        payload["statusDetails"] = status_details

    destination = output_dir / ALLURE_RESULTS_DIRECTORY / f"{result_uuid}-result.json"
    write_json_atomic(destination, payload)
    return destination


def _labels(
    result: Mapping[str, Any],
    source_path: str,
    environment: str,
) -> list[dict[str, str]]:
    labels = [
        {"name": "language", "value": "python"},
        {"name": "framework", "value": "plantain"},
        {"name": "parentSuite", "value": "Plantain"},
        {"name": "suite", "value": _suite_name(source_path)},
        {"name": "environment", "value": _bounded(environment)},
    ]

    tags = result.get("tags")
    if isinstance(tags, Sequence) and not isinstance(
        tags,
        (str, bytes, bytearray),
    ):
        for tag in tags:
            safe_tag = _safe_text(tag)
            if safe_tag:
                labels.append({"name": "tag", "value": safe_tag})

    return labels


def _parameters(
    result: Mapping[str, Any],
    environment: str,
) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = [
        {
            "name": "environment",
            "value": _bounded(environment),
        },
    ]

    for field, display_name in (
        ("correlation_id", "correlationId"),
        ("jira_ticket", "jiraTicket"),
        ("test_case_key", "testCaseKey"),
        ("test_run_key", "testRunKey"),
    ):
        value = result.get(field)
        if value is None or value == "":
            continue

        parameters.append(
            {
                "name": display_name,
                "value": _format_value(value),
                "excluded": True,
            }
        )

    parameters.extend(_integration_parameters(result.get("integrations")))
    return parameters


def _integration_parameters(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    parameters: list[dict[str, Any]] = []
    providers = sorted(key for key in value if isinstance(key, str))[:MAX_ALLURE_INTEGRATIONS]
    for provider in providers:
        outcome = value[provider]
        if not isinstance(outcome, Mapping):
            continue
        details: dict[str, str | int] = {}
        for field in (
            "status",
            "cleanup_status",
            "cleanup_failure_stage",
            "delivery_state",
            "retry_status",
            "http_status",
            "failure_stage",
            "attachment_status",
            "attachment_http_status",
        ):
            item = outcome.get(field)
            if isinstance(item, str) or type(item) is int:
                details[field] = item
        safe_provider = _bounded(_safe_text(provider), MAX_ALLURE_PROVIDER_LENGTH)
        if details and safe_provider:
            parameters.append(
                {
                    "name": f"integration.{safe_provider}",
                    "value": _format_value(details),
                    "excluded": True,
                }
            )
    return parameters


def _steps(
    result: Mapping[str, Any],
    scenario_start: int,
    scenario_stop: int,
) -> list[dict[str, Any]]:
    operations = result.get("operations")

    if isinstance(operations, Sequence) and not isinstance(
        operations,
        (str, bytes, bytearray),
    ):
        operation_steps = [
            _operation_step(
                operation,
                scenario_start,
                scenario_stop,
            )
            for operation in operations
            if isinstance(operation, Mapping)
        ]
        if operation_steps:
            return operation_steps

    return _activity_steps(
        result,
        scenario_start,
        scenario_stop,
    )


def _operation_step(
    operation: Mapping[str, Any],
    scenario_start: int,
    scenario_stop: int,
) -> dict[str, Any]:
    operation_type = _safe_text(
        operation.get("operation_type"),
        fallback="operation",
    )
    domain = _safe_text(
        operation.get("domain"),
        fallback="activity",
    ).upper()
    target = _safe_text(
        operation.get("operation_target"),
        fallback="",
    )

    name_parts = [f"{domain} {operation_type}"]

    if target:
        name_parts.append(f"target={target}")

    if "operation_input" in operation:
        name_parts.append(f"input={_format_value(operation.get('operation_input'))}")

    if "operation_expected" in operation:
        name_parts.append(f"expected={_format_value(operation.get('operation_expected'))}")
    if "operation_actual" in operation:
        name_parts.append(f"actual={_format_value(operation.get('operation_actual'))}")

    started = _bounded_timestamp(
        operation.get("started_at_ms"),
        scenario_start,
    )
    stopped = _bounded_timestamp(
        operation.get("stopped_at_ms"),
        started,
    )
    stopped = min(
        max(stopped, started),
        max(scenario_stop, started),
    )

    step: dict[str, Any] = {
        "name": _bounded(
            " | ".join(name_parts),
            MAX_ALLURE_TEXT_LENGTH,
        ),
        "status": _allure_status(operation.get("status")),
        "stage": "finished",
        "start": started,
        "stop": stopped,
    }

    error = operation.get("operation_error")
    if error not in (None, ""):
        step["statusDetails"] = {
            "message": _safe_text(error),
        }

    return step


def _activity_steps(
    result: Mapping[str, Any],
    scenario_start: int,
    scenario_stop: int,
) -> list[dict[str, Any]]:
    raw_steps = result.get("steps")

    if not isinstance(raw_steps, Sequence) or isinstance(
        raw_steps,
        (str, bytes, bytearray),
    ):
        return []

    cursor = scenario_start
    steps: list[dict[str, Any]] = []

    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            continue

        activity = _safe_text(
            raw_step.get("activity"),
            fallback="activity",
        )
        step_id = _safe_text(
            raw_step.get("step_id"),
            fallback="step",
        )
        duration = max(
            _integer(raw_step.get("duration_ms"), default=0),
            0,
        )
        stopped = min(
            cursor + duration,
            max(scenario_stop, cursor),
        )

        steps.append(
            {
                "name": _bounded(f"{activity} [{step_id}]"),
                "status": _allure_status(raw_step.get("status")),
                "stage": "finished",
                "start": cursor,
                "stop": stopped,
            }
        )
        cursor = stopped

    return steps


def _status_details(
    result: Mapping[str, Any],
) -> dict[str, str]:
    failure = result.get("failure")

    if not isinstance(failure, Mapping):
        return {}

    message = _safe_text(
        failure.get("message"),
        fallback="",
    )
    exception_type = _safe_text(
        failure.get("exception_type"),
        fallback="",
    )

    if not message and not exception_type:
        return {}

    if exception_type and message:
        return {"message": _bounded(f"{exception_type}: {message}")}

    return {"message": message or exception_type}


def _suite_name(source_path: str) -> str:
    if not source_path:
        return "Scenarios"

    parent = PurePosixPath(source_path.replace("\\", "/")).parent.as_posix()

    if parent in {"", "."}:
        return "Scenarios"

    return _bounded(parent)


def _full_name(
    source_path: str,
    scenario: str,
) -> str:
    source = source_path.replace("\\", "/") if source_path else "scenario"
    return _bounded(
        f"{source}::{scenario}",
        MAX_ALLURE_TEXT_LENGTH,
    )


def _stable_id(
    namespace: str,
    *parts: str,
) -> str:
    material = "\x00".join((namespace, *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _allure_status(value: Any) -> str:
    normalized = str(value).strip().lower()

    if normalized in {
        "passed",
        "failed",
        "broken",
        "skipped",
        "unknown",
    }:
        return normalized

    if normalized in {"error", "errored"}:
        return "broken"

    return "unknown"


def _bounded_timestamp(
    value: Any,
    fallback: int,
) -> int:
    return max(
        _integer(value, default=fallback),
        0,
    )


def _integer(
    value: Any,
    *,
    default: int,
) -> int:
    if type(value) is int:
        return value

    return default


def _safe_text(
    value: Any,
    *,
    fallback: str = "",
) -> str:
    if value is None:
        return fallback

    if isinstance(value, str):
        return _bounded(value)

    if isinstance(value, (int, float, bool)):
        return _bounded(str(value))

    return fallback


def _format_value(value: Any) -> str:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        serialized = "<unavailable>"

    return _bounded(
        serialized,
        MAX_ALLURE_VALUE_LENGTH,
    )


def _bounded(
    value: str,
    limit: int = MAX_ALLURE_VALUE_LENGTH,
) -> str:
    if len(value) <= limit:
        return value

    retained_length = max(limit - 14, 0)
    return f"{value[:retained_length]}...[truncated]"


__all__ = [
    "ALLURE_RESULTS_DIRECTORY",
    "write_allure_result",
]
