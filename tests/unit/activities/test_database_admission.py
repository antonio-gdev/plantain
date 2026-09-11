"""Global database-operation admission tests."""

from __future__ import annotations

import asyncio
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, cast

from plantain.activities.database.service import DatabaseSession
from plantain.engine.admission import ResourceAdmission, ResourceKind

EXPECTED_RESULTS = ["complete", "complete"]


def test_sessions_share_global_database_operation_admission() -> None:
    settings = SimpleNamespace(
        db_query_timeout_seconds=1.0,
        max_database_operations=1,
        max_worker_threads=2,
    )
    admission = ResourceAdmission(settings)
    sessions = [
        DatabaseSession(
            cast("Any", settings),
            cast("Any", SimpleNamespace()),
            admission=admission,
        )
        for _ in range(2)
    ]
    release = Event()
    state_lock = Lock()
    active = 0
    peak = 0

    async def exercise() -> list[str]:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def operation(_state: object) -> str:
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait(timeout=1)
                return "complete"
            finally:
                with state_lock:
                    active -= 1

        tasks = [
            asyncio.create_task(session._bounded_operation("Database probe", operation))
            for session in sessions
        ]
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == admission.limit(ResourceKind.DATABASE_OPERATION)
        release.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(exercise())

    assert results == EXPECTED_RESULTS
