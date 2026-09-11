"""Isolated API-validation transport and safe error reconstruction tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api.client import ApiActivityError
from plantain.activities.api.schema_validation import (
    SchemaConformanceError,
    validate_instance,
)
from plantain.activities.api.validation_worker import run_isolated_api_validation
from plantain.engine.admission import ResourceAdmission
from plantain.engine.process_worker import (
    ProcessLimits,
    ProcessWorkerError,
    ProcessWorkerTimeoutError,
)


# This inline controller models only the admission contract exercised by the adapter.
class _InlineAdmission:
    def __init__(self, failure: ProcessWorkerError | None = None) -> None:
        self.failure = failure
        self.limits: ProcessLimits | None = None

    async def run_process(
        self,
        function: Callable[..., Any],
        *args: Any,
        limits: ProcessLimits,
        **kwargs: Any,
    ) -> Any:
        self.limits = limits
        if self.failure is not None:
            raise self.failure
        return function(*args, **kwargs)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        schema_validation_timeout_seconds=1.5,
        schema_validation_cpu_seconds=3,
        schema_validation_memory_mib=64,
    )


def _accept(_value: object) -> None:
    pass


def _reject(_value: object) -> None:
    raise SchemaConformanceError(
        "invalid\x00schema",
        safe_details={"schema_path": "$.token"},
    )


def test_isolated_validation_forwards_limits_and_success() -> None:
    controller = _InlineAdmission()

    asyncio.run(
        run_isolated_api_validation(
            cast("ResourceAdmission", controller),
            _settings(),
            _accept,
            {"value": "safe"},
        )
    )

    assert controller.limits == ProcessLimits(
        wall_timeout_seconds=1.5,
        cpu_timeout_seconds=3,
        memory_bytes=64 * 1_048_576,
    )


def test_isolated_validation_reconstructs_safe_conformance_error() -> None:
    controller = _InlineAdmission()

    with pytest.raises(SchemaConformanceError, match="invalid schema") as captured:
        asyncio.run(
            run_isolated_api_validation(
                cast("ResourceAdmission", controller),
                _settings(),
                _reject,
                object(),
            )
        )

    assert captured.value.safe_details == {"schema_path": "$.token"}


def test_isolated_validation_maps_wall_timeout() -> None:
    controller = _InlineAdmission(
        ProcessWorkerTimeoutError("internal timeout", failure_type="Timeout")
    )

    with pytest.raises(
        ApiActivityError,
        match="PLANTAIN_SCHEMA_VALIDATION_TIMEOUT_SECONDS",
    ) as captured:
        asyncio.run(
            run_isolated_api_validation(
                cast("ResourceAdmission", controller),
                _settings(),
                _accept,
                object(),
            )
        )

    assert captured.value.safe_details == {"failure_stage": "schema_validation_worker"}


def test_isolated_validation_terminates_catastrophic_regex() -> None:
    settings = _settings()
    settings.schema_validation_timeout_seconds = 0.1
    settings.schema_validation_memory_mib = 256
    admission = ResourceAdmission(settings)
    rejected = ("a" * 30_000) + "!"

    async def exercise() -> None:
        await asyncio.wait_for(
            run_isolated_api_validation(
                admission,
                settings,
                validate_instance,
                {"openapi": "3.1.0"},
                {"type": "string", "pattern": "^(a+)+$"},
                rejected,
                label="regex-constrained value",
            ),
            timeout=3,
        )

    with pytest.raises(
        ApiActivityError,
        match="PLANTAIN_SCHEMA_VALIDATION_TIMEOUT_SECONDS",
    ):
        asyncio.run(exercise())


def test_isolated_validation_exposes_only_unexpected_failure_type() -> None:
    controller = _InlineAdmission(
        ProcessWorkerError("sensitive child detail", failure_type="ValueError")
    )

    with pytest.raises(ApiActivityError, match=r"failed safely \(ValueError\)") as captured:
        asyncio.run(
            run_isolated_api_validation(
                cast("ResourceAdmission", controller),
                _settings(),
                _accept,
                object(),
            )
        )

    assert "sensitive child detail" not in str(captured.value)
    assert captured.value.safe_details == {"failure_stage": "schema_validation_worker"}
