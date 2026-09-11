"""Loop-safe global resource-admission tests."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from threading import Event, Lock
from types import SimpleNamespace

import pytest

import plantain.engine.admission as admission_module
from plantain.engine.admission import ResourceAdmission, ResourceKind
from plantain.engine.process_worker import ProcessLimits, ProcessWorkerError
from plantain.errors import ConfigurationError

EXPECTED_DEFAULT_LIMITS = (4, 2, 16, 8, 8, 2)
EXPECTED_POWER = 8


def test_admission_uses_safe_defaults_for_partial_test_settings() -> None:
    admission = ResourceAdmission(SimpleNamespace())

    assert tuple(admission.limit(kind) for kind in ResourceKind) == EXPECTED_DEFAULT_LIMITS


def test_admission_rejects_invalid_direct_settings() -> None:
    with pytest.raises(ConfigurationError, match="max_worker_threads"):
        ResourceAdmission(SimpleNamespace(max_worker_threads=0))


def test_admission_bounds_concurrent_holders() -> None:
    async def exercise() -> None:
        admission = ResourceAdmission(SimpleNamespace(max_api_requests=2))
        active = 0
        peak = 0
        two_entered = asyncio.Event()
        release = asyncio.Event()
        state_lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal active, peak
            async with admission.acquire(ResourceKind.API_REQUEST):
                async with state_lock:
                    active += 1
                    peak = max(peak, active)
                    if active == admission.limit(ResourceKind.API_REQUEST):
                        two_entered.set()
                await release.wait()
                async with state_lock:
                    active -= 1

        tasks = [asyncio.create_task(worker()) for _ in range(3)]
        await asyncio.wait_for(two_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == admission.limit(ResourceKind.API_REQUEST)
        release.set()
        await asyncio.gather(*tasks)
        assert active == 0

    asyncio.run(exercise())


def test_cancelled_waiter_does_not_leak_a_permit() -> None:
    async def exercise() -> None:
        admission = ResourceAdmission(SimpleNamespace(max_browser_sessions=1))
        held = await admission.reserve(ResourceKind.BROWSER)
        waiting = asyncio.create_task(admission.reserve(ResourceKind.BROWSER))
        await asyncio.sleep(0)
        assert waiting.done() is False

        waiting.cancel()
        with suppress(asyncio.CancelledError):
            await waiting
        held.release()

        replacement = await asyncio.wait_for(
            admission.reserve(ResourceKind.BROWSER),
            timeout=1,
        )
        replacement.release()
        replacement.release()

    asyncio.run(exercise())


def test_admission_can_be_reused_across_sequential_event_loops() -> None:
    admission = ResourceAdmission(SimpleNamespace(max_database_operations=1))

    async def contend() -> None:
        held = await admission.reserve(ResourceKind.DATABASE_OPERATION)
        waiting = asyncio.create_task(admission.reserve(ResourceKind.DATABASE_OPERATION))
        await asyncio.sleep(0)
        assert waiting.done() is False
        held.release()
        acquired = await asyncio.wait_for(waiting, timeout=1)
        acquired.release()

    asyncio.run(contend())
    asyncio.run(contend())


def test_cancelled_database_work_holds_both_slots_until_thread_finishes() -> None:
    release = Event()

    async def exercise() -> None:
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocking() -> None:
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=1)

        admission = ResourceAdmission(
            SimpleNamespace(
                max_database_operations=1,
                max_worker_threads=1,
            )
        )
        running = asyncio.create_task(admission.run_database_operation(blocking))
        await asyncio.wait_for(started.wait(), timeout=1)
        running.cancel()
        with suppress(asyncio.CancelledError):
            await running

        waiting = [
            asyncio.create_task(admission.reserve(ResourceKind.DATABASE_OPERATION)),
            asyncio.create_task(admission.reserve(ResourceKind.WORKER_THREAD)),
        ]
        await asyncio.sleep(0)
        assert all(task.done() is False for task in waiting)
        release.set()
        permits = await asyncio.wait_for(asyncio.gather(*waiting), timeout=1)
        for permit in permits:
            permit.release()

    asyncio.run(exercise())
    assert release.is_set()
    # The queued reserve completed only after the retained worker permit was released.


def test_sync_process_gate_cancels_waiter_without_starting_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_started = Event()
    release_first = Event()
    waiter_started = Event()
    calls = 0
    calls_lock = Lock()

    def fake_worker(*_args: object, **_kwargs: object) -> int:
        nonlocal calls
        with calls_lock:
            calls += 1
        first_started.set()
        release_first.wait(timeout=1)
        return EXPECTED_POWER

    monkeypatch.setattr(admission_module, "run_process_worker", fake_worker)
    admission = ResourceAdmission(SimpleNamespace(max_worker_processes=1))
    limits = ProcessLimits(
        wall_timeout_seconds=1,
        cpu_timeout_seconds=1,
        memory_bytes=1,
    )
    first_cancelled = Event()
    waiting_cancelled = Event()

    def run_waiter() -> int:
        waiter_started.set()
        return admission.run_process_sync(
            pow,
            (2, 3),
            {},
            limits=limits,
            cancelled=waiting_cancelled,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            admission.run_process_sync,
            pow,
            (2, 3),
            {},
            limits=limits,
            cancelled=first_cancelled,
        )
        assert first_started.wait(timeout=1)
        waiting = executor.submit(run_waiter)
        assert waiter_started.wait(timeout=1)
        waiting_cancelled.set()
        with pytest.raises(ProcessWorkerError, match="cancelled"):
            waiting.result(timeout=1)
        release_first.set()
        assert first.result(timeout=1) == EXPECTED_POWER

    assert calls == 1
