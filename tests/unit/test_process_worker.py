"""Killable process-worker security and lifecycle tests."""

from __future__ import annotations

import os
import time
from threading import Event
from typing import Any, cast

import pytest

from plantain.engine import process_worker
from plantain.engine.process_worker import (
    ProcessLimits,
    ProcessWorkerError,
    ProcessWorkerTimeoutError,
    run_process_worker,
)

MEBIBYTE = 1_048_576
EXPECTED_POWER = 8
MAX_TIMEOUT_ELAPSED_SECONDS = 2.0
SYNTHETIC_ENV_NAME = "PLANTAIN_PROCESS_WORKER_TEST_SECRET"
SYNTHETIC_ENV_VALUE = "synthetic-child-secret"
DARWIN_PAGE_BYTES = 100
DARWIN_BASELINE_BYTES = 700
DARWIN_BUDGET_BYTES = 200
DARWIN_HARD_BYTES = 2_000
DARWIN_EXPECTED_CEILING_BYTES = 900


class _DarwinAddressSpaceResource:
    RLIMIT_AS = 9

    def __init__(
        self,
        *,
        baseline_bytes: int,
        hard_bytes: int,
        page_bytes: int,
    ) -> None:
        self._baseline_bytes = baseline_bytes
        self._hard_bytes = hard_bytes
        self._page_bytes = page_bytes
        self.calls: list[tuple[int, tuple[int, int]]] = []

    def getpagesize(self) -> int:
        return self._page_bytes

    def getrlimit(self, resource: int) -> tuple[int, int]:
        assert resource == self.RLIMIT_AS
        return (self._hard_bytes, self._hard_bytes)

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None:
        assert resource == self.RLIMIT_AS
        self.calls.append((resource, limits))
        soft_bytes, hard_bytes = limits
        if (
            hard_bytes > self._hard_bytes
            or soft_bytes > hard_bytes
            or soft_bytes < self._baseline_bytes
        ):
            raise ValueError("limit is incompatible with the current address space")
        self._hard_bytes = hard_bytes


class _RecordingSender:
    def __init__(self) -> None:
        self.envelope: tuple[str, Any] | None = None
        self.closed = False

    def send(self, envelope: tuple[str, Any]) -> None:
        self.envelope = envelope

    def close(self) -> None:
        self.closed = True


def _raise_limit_error(_limits: ProcessLimits) -> None:
    raise ValueError("synthetic limit setup failure")


def _limits(*, wall_timeout_seconds: float = 5.0) -> ProcessLimits:
    return ProcessLimits(
        wall_timeout_seconds=wall_timeout_seconds,
        cpu_timeout_seconds=2,
        memory_bytes=512 * MEBIBYTE,
    )


def test_worker_returns_picklable_result_and_strips_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SYNTHETIC_ENV_NAME, SYNTHETIC_ENV_VALUE)

    assert run_process_worker(pow, (2, 3), {}, _limits(), Event()) == EXPECTED_POWER
    assert (
        run_process_worker(
            os.getenv,
            (SYNTHETIC_ENV_NAME,),
            {},
            _limits(),
            Event(),
        )
        is None
    )


def test_worker_exposes_only_unexpected_exception_type() -> None:
    with pytest.raises(ProcessWorkerError, match="ValueError") as captured:
        run_process_worker(
            int,
            (SYNTHETIC_ENV_VALUE,),
            {},
            _limits(),
            Event(),
        )

    assert SYNTHETIC_ENV_VALUE not in str(captured.value)
    assert captured.value.failure_type == "ValueError"


def test_non_darwin_memory_limit_remains_an_absolute_ceiling() -> None:
    resource = _DarwinAddressSpaceResource(
        baseline_bytes=0,
        hard_bytes=DARWIN_HARD_BYTES,
        page_bytes=DARWIN_PAGE_BYTES,
    )

    process_worker._apply_address_space_limit(
        resource,
        DARWIN_BUDGET_BYTES,
        platform="linux",
    )

    assert resource.calls == [
        (
            resource.RLIMIT_AS,
            (DARWIN_BUDGET_BYTES, DARWIN_BUDGET_BYTES),
        )
    ]


def test_darwin_memory_limit_preserves_budget_above_existing_map() -> None:
    resource = _DarwinAddressSpaceResource(
        baseline_bytes=DARWIN_BASELINE_BYTES,
        hard_bytes=DARWIN_HARD_BYTES,
        page_bytes=DARWIN_PAGE_BYTES,
    )

    process_worker._apply_address_space_limit(
        resource,
        DARWIN_BUDGET_BYTES,
        platform="darwin",
    )

    assert resource.calls[-1] == (
        resource.RLIMIT_AS,
        (DARWIN_EXPECTED_CEILING_BYTES, DARWIN_EXPECTED_CEILING_BYTES),
    )
    assert (
        resource.RLIMIT_AS,
        (DARWIN_HARD_BYTES, DARWIN_HARD_BYTES),
    ) in resource.calls


def test_child_reports_resource_limit_setup_failure_by_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _RecordingSender()
    monkeypatch.setattr(process_worker, "_apply_child_limits", _raise_limit_error)

    process_worker._child_main(
        cast("Any", sender),
        pow,
        (2, 3),
        {},
        _limits(),
    )

    assert sender.envelope == ("error", "ValueError")
    assert sender.closed is True


def test_worker_wall_timeout_confirms_process_termination() -> None:
    started = time.monotonic()

    with pytest.raises(ProcessWorkerTimeoutError, match="wall-time"):
        run_process_worker(
            time.sleep,
            (5,),
            {},
            _limits(wall_timeout_seconds=0.05),
            Event(),
        )

    assert time.monotonic() - started < MAX_TIMEOUT_ELAPSED_SECONDS


def test_worker_rejects_pre_cancelled_and_invalid_execution() -> None:
    cancelled = Event()
    cancelled.set()

    with pytest.raises(ProcessWorkerError, match="cancelled") as captured:
        run_process_worker(pow, (2, 3), {}, _limits(), cancelled)
    assert captured.value.failure_type == "Cancelled"

    with pytest.raises(ValueError, match="positive"):
        ProcessLimits(
            wall_timeout_seconds=0,
            cpu_timeout_seconds=1,
            memory_bytes=MEBIBYTE,
        )
