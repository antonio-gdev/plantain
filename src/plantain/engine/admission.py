"""Runner-owned admission control for globally bounded runtime resources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from threading import BoundedSemaphore, Event, Lock
from typing import Any, ParamSpec, TypeVar
from weakref import WeakKeyDictionary

from plantain.config import (
    MAX_API_REQUESTS,
    MAX_BROWSER_SESSIONS,
    MAX_DATABASE_OPERATIONS,
    MAX_SCENARIO_CONCURRENCY,
    MAX_WORKER_PROCESSES,
    MAX_WORKER_THREADS,
)
from plantain.engine.process_worker import (
    ProcessLimits,
    ProcessWorkerError,
    run_process_worker,
)
from plantain.errors import ConfigurationError

_P = ParamSpec("_P")
_T = TypeVar("_T")
_PROCESS_GATE_POLL_SECONDS = 0.02


class ResourceKind(StrEnum):
    """Independently bounded resource classes shared by a runner execution."""

    SCENARIO = "scenario"
    BROWSER = "browser"
    API_REQUEST = "api_request"
    DATABASE_OPERATION = "database_operation"
    WORKER_THREAD = "worker_thread"
    WORKER_PROCESS = "worker_process"


_LIMIT_SPECS: dict[ResourceKind, tuple[str, int, int]] = {
    ResourceKind.SCENARIO: (
        "max_scenario_concurrency",
        4,
        MAX_SCENARIO_CONCURRENCY,
    ),
    ResourceKind.BROWSER: ("max_browser_sessions", 2, MAX_BROWSER_SESSIONS),
    ResourceKind.API_REQUEST: ("max_api_requests", 16, MAX_API_REQUESTS),
    ResourceKind.DATABASE_OPERATION: (
        "max_database_operations",
        8,
        MAX_DATABASE_OPERATIONS,
    ),
    ResourceKind.WORKER_THREAD: ("max_worker_threads", 8, MAX_WORKER_THREADS),
    ResourceKind.WORKER_PROCESS: ("max_worker_processes", 2, MAX_WORKER_PROCESSES),
}


class ResourcePermit:
    """One cancellation-safe, idempotently released admission permit."""

    def __init__(self, semaphore: asyncio.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()


class ResourceAdmission:
    """Share hard resource limits across every scenario owned by one runner."""

    def __init__(self, settings: object) -> None:
        self._limits = {
            kind: _validated_limit(settings, name, default, maximum)
            for kind, (name, default, maximum) in _LIMIT_SPECS.items()
        }
        self._states: WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            dict[ResourceKind, asyncio.BoundedSemaphore],
        ] = WeakKeyDictionary()
        self._state_lock = Lock()
        self._process_gate = BoundedSemaphore(self._limits[ResourceKind.WORKER_PROCESS])

    def limit(self, kind: ResourceKind) -> int:
        return self._limits[kind]

    async def reserve(self, kind: ResourceKind) -> ResourcePermit:
        semaphore = self._semaphore(kind)
        await semaphore.acquire()
        return ResourcePermit(semaphore)

    @asynccontextmanager
    async def acquire(self, kind: ResourceKind) -> AsyncIterator[None]:
        permit = await self.reserve(kind)
        try:
            yield
        finally:
            permit.release()

    async def run_blocking(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        return await self._run_blocking(
            (ResourceKind.WORKER_THREAD,),
            function,
            *args,
            **kwargs,
        )

    async def run_database_operation(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        return await self._run_blocking(
            (ResourceKind.DATABASE_OPERATION, ResourceKind.WORKER_THREAD),
            function,
            *args,
            **kwargs,
        )

    def run_process_sync(
        self,
        function: Callable[..., _T],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        limits: ProcessLimits,
        cancelled: Event,
    ) -> _T:
        """Run one process worker under the admission shared by every event loop."""

        while not cancelled.is_set():
            if self._process_gate.acquire(timeout=_PROCESS_GATE_POLL_SECONDS):
                break
        else:
            raise ProcessWorkerError(
                "Worker process was cancelled safely",
                failure_type="Cancelled",
            )
        try:
            return run_process_worker(function, args, kwargs, limits, cancelled)
        finally:
            self._process_gate.release()

    async def run_process(
        self,
        function: Callable[..., _T],
        /,
        *args: Any,
        limits: ProcessLimits,
        **kwargs: Any,
    ) -> _T:
        permits = await self._reserve_many(
            (ResourceKind.WORKER_PROCESS, ResourceKind.WORKER_THREAD)
        )
        cancelled = Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                self.run_process_sync,
                function,
                tuple(args),
                dict(kwargs),
                limits=limits,
                cancelled=cancelled,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            task.add_done_callback(lambda completed: _release_background(completed, permits))
            raise
        finally:
            if task.done():
                _release_permits(permits)

    async def _run_blocking(
        self,
        kinds: tuple[ResourceKind, ...],
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        permits = await self._reserve_many(kinds)
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda completed: _release_background(completed, permits))
            raise
        finally:
            if task.done():
                _release_permits(permits)

    async def _reserve_many(
        self,
        kinds: tuple[ResourceKind, ...],
    ) -> tuple[ResourcePermit, ...]:
        permits: list[ResourcePermit] = []
        try:
            for kind in kinds:
                # Incremental retention makes partial cancellation releasable.
                permits.append(await self.reserve(kind))  # noqa: PERF401
        except BaseException:
            _release_permits(tuple(permits))
            raise
        return tuple(permits)

    def _semaphore(self, kind: ResourceKind) -> asyncio.BoundedSemaphore:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            state = self._states.get(loop)
            if state is None:
                state = {
                    item: asyncio.BoundedSemaphore(limit) for item, limit in self._limits.items()
                }
                self._states[loop] = state
            return state[kind]


def _release_background(
    future: asyncio.Future[_T],
    permits: tuple[ResourcePermit, ...],
) -> None:
    if not future.cancelled():
        future.exception()
    _release_permits(permits)


def _release_permits(permits: tuple[ResourcePermit, ...]) -> None:
    for permit in reversed(permits):
        permit.release()


def _validated_limit(
    settings: object,
    name: str,
    default: int,
    maximum: int,
) -> int:
    value = getattr(settings, name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > maximum:
        raise ConfigurationError(f"{name} must be an integer between 1 and {maximum}")
    return value


__all__ = ["ResourceAdmission", "ResourceKind", "ResourcePermit"]
# The permit is public so long-lived resources can explicitly own their slot.
