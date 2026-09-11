"""Killable spawn-process execution with OS-enforced resource ceilings."""

from __future__ import annotations

import multiprocessing
import os
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from threading import Event
from typing import Any, Protocol, TypeVar, cast

_T = TypeVar("_T")
_POLL_INTERVAL_SECONDS = 0.02
_TERMINATE_GRACE_SECONDS = 0.25
_MAX_FAILURE_TYPE_LENGTH = 100


class _ManagedProcess(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class _AddressSpaceResource(Protocol):
    RLIMIT_AS: int

    def getpagesize(self) -> int: ...

    def getrlimit(self, resource: int) -> tuple[int, int]: ...

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None: ...


class ProcessWorkerError(RuntimeError):
    """Raised with type-only diagnostics when isolated work fails safely."""

    def __init__(self, message: str, *, failure_type: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type[:_MAX_FAILURE_TYPE_LENGTH]


class ProcessWorkerTimeoutError(ProcessWorkerError):
    """Raised after a worker exceeds its parent-enforced wall deadline."""


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    """Hard ceilings applied to one short-lived spawned worker."""

    wall_timeout_seconds: float
    cpu_timeout_seconds: int
    memory_bytes: int

    def __post_init__(self) -> None:
        # Every bound is strict because zero would disable at least one isolation control.
        if self.wall_timeout_seconds <= 0 or self.cpu_timeout_seconds < 1 or self.memory_bytes < 1:
            raise ValueError("Process worker limits must be positive")


def run_process_worker(
    function: Callable[..., _T],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    limits: ProcessLimits,
    cancelled: Event,
) -> _T:
    """Run picklable work and confirm child termination before returning."""

    if cancelled.is_set():
        raise ProcessWorkerError(
            "Worker process was cancelled safely",
            failure_type="Cancelled",
        )
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_main,
        args=(sender, function, args, kwargs, limits),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        sender.close()
        return cast(
            "_T",
            _await_process_result(receiver, process, limits, cancelled),
        )
    except ProcessWorkerError:
        raise
    except Exception as exc:
        raise ProcessWorkerError(
            f"Worker process could not be managed safely ({type(exc).__name__})",
            failure_type=type(exc).__name__,
        ) from exc
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                _stop_process(process)
            process.close()


def _await_process_result(
    receiver: Connection,
    process: _ManagedProcess,
    limits: ProcessLimits,
    cancelled: Event,
) -> Any:
    deadline = time.monotonic() + limits.wall_timeout_seconds
    while process.is_alive():
        if cancelled.is_set():
            _stop_process(process)
            raise ProcessWorkerError(
                "Worker process was cancelled safely",
                failure_type="Cancelled",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            raise ProcessWorkerTimeoutError(
                "Worker process exceeded its wall-time limit",
                failure_type="Timeout",
            )
        process.join(min(_POLL_INTERVAL_SECONDS, remaining))

    process.join()
    if not receiver.poll():
        raise ProcessWorkerError(
            "Worker process exited without a safe result",
            failure_type="WorkerExit",
        )
    try:
        status, payload = receiver.recv()
    except (EOFError, OSError) as exc:
        raise ProcessWorkerError(
            "Worker process result channel failed safely",
            failure_type=type(exc).__name__,
        ) from exc
    if status == "result":
        return payload
    raise ProcessWorkerError(
        f"Worker process failed safely ({payload})",
        failure_type=str(payload),
    )


def _child_main(
    sender: Connection,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    limits: ProcessLimits,
) -> None:
    try:
        try:
            _apply_child_limits(limits)
            envelope: tuple[str, Any] = ("result", function(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - only the exception type crosses IPC.
            envelope = ("error", type(exc).__name__[:_MAX_FAILURE_TYPE_LENGTH])
        with suppress(Exception):
            sender.send(envelope)
    finally:
        sender.close()


def _apply_child_limits(limits: ProcessLimits) -> None:
    os.environ.clear()
    try:
        import resource  # noqa: PLC0415 - unavailable platforms must fail closed in the child.
    except ImportError as exc:
        raise RuntimeError("ProcessResourceLimitsUnavailable") from exc

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (limits.cpu_timeout_seconds, limits.cpu_timeout_seconds),
    )
    _apply_address_space_limit(
        cast("_AddressSpaceResource", resource),
        limits.memory_bytes,
        platform=sys.platform,
    )


def _apply_address_space_limit(
    resource_module: _AddressSpaceResource,
    memory_bytes: int,
    *,
    platform: str,
) -> None:
    if platform != "darwin":
        resource_module.setrlimit(
            resource_module.RLIMIT_AS,
            (memory_bytes, memory_bytes),
        )
        return

    original_limits = resource_module.getrlimit(resource_module.RLIMIT_AS)
    page_bytes = resource_module.getpagesize()
    if page_bytes < 1:
        raise RuntimeError("ProcessResourceLimitsUnavailable")
    baseline_bytes = _darwin_address_space_floor(
        resource_module,
        original_limits=original_limits,
        page_bytes=page_bytes,
    )
    ceiling_bytes = min(baseline_bytes + memory_bytes, original_limits[1])
    if ceiling_bytes < baseline_bytes:
        raise RuntimeError("ProcessResourceLimitsUnavailable")
    resource_module.setrlimit(
        resource_module.RLIMIT_AS,
        (ceiling_bytes, ceiling_bytes),
    )


def _darwin_address_space_floor(
    resource_module: _AddressSpaceResource,
    *,
    original_limits: tuple[int, int],
    page_bytes: int,
) -> int:
    hard_bytes = original_limits[1]
    if hard_bytes < page_bytes:
        raise RuntimeError("ProcessResourceLimitsUnavailable")

    rejected_bytes = 0
    accepted_bytes = page_bytes
    while not _darwin_limit_is_accepted(
        resource_module,
        candidate_bytes=accepted_bytes,
        original_limits=original_limits,
    ):
        rejected_bytes = accepted_bytes
        accepted_bytes = min(accepted_bytes * 2, hard_bytes)
        if accepted_bytes <= rejected_bytes:
            raise RuntimeError("ProcessResourceLimitsUnavailable")

    while accepted_bytes - rejected_bytes > page_bytes:
        candidate_bytes = (((accepted_bytes + rejected_bytes) // 2) // page_bytes) * page_bytes
        if candidate_bytes <= rejected_bytes:
            break
        if _darwin_limit_is_accepted(
            resource_module,
            candidate_bytes=candidate_bytes,
            original_limits=original_limits,
        ):
            accepted_bytes = candidate_bytes
        else:
            rejected_bytes = candidate_bytes
    return accepted_bytes


def _darwin_limit_is_accepted(
    resource_module: _AddressSpaceResource,
    *,
    candidate_bytes: int,
    original_limits: tuple[int, int],
) -> bool:
    try:
        resource_module.setrlimit(
            resource_module.RLIMIT_AS,
            (candidate_bytes, original_limits[1]),
        )
    except ValueError:
        return False
    resource_module.setrlimit(resource_module.RLIMIT_AS, original_limits)
    return True


def _stop_process(process: _ManagedProcess) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()


__all__ = [
    "ProcessLimits",
    "ProcessWorkerError",
    "ProcessWorkerTimeoutError",
    "run_process_worker",
]
