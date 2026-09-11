"""Killable process-worker security and lifecycle tests."""

from __future__ import annotations

import os
import time
from threading import Event

import pytest

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
