"""Global browser lifetime admission tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plantain.activities.ui_browser import BrowserOptions, BrowserSession
from plantain.activities.ui_errors import BrowserLifecycleError
from plantain.engine.admission import ResourceAdmission, ResourceKind


class Telemetry:
    def close(self) -> None:
        pass


async def _allow(url: str) -> str:
    return url


def test_sessions_share_browser_lifetime_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = ResourceAdmission(SimpleNamespace(max_browser_sessions=1))
    sessions = [
        BrowserSession(
            BrowserOptions(auto_install=False),
            _allow,
            _allow,
            Telemetry(),  # type: ignore[arg-type]
            admission=admission,
        )
        for _ in range(2)
    ]
    active = 0
    peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare(_session: BrowserSession) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        raise BrowserLifecycleError("Synthetic preparation failure")

    monkeypatch.setattr(BrowserSession, "prepare", prepare)

    async def exercise() -> list[object]:
        tasks = [asyncio.create_task(session.start()) for session in sessions]
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == admission.limit(ResourceKind.BROWSER)
        release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(exercise())

    assert all(isinstance(result, BrowserLifecycleError) for result in results)
