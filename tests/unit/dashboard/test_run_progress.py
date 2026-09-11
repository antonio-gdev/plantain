"""Live dashboard progress remains bounded, coalesced, and value-free."""

from __future__ import annotations

import asyncio

from plantain.dashboard.run_progress import (
    MAX_LIVE_PROGRESS_EVENTS,
    MAX_LIVE_PROGRESS_STEPS,
    DashboardRunProgressChannel,
)
from plantain.engine.progress import RunProgressEvent

CORRELATION_ID = "a" * 32
EXPECTED_PROGRESS_PERCENT = 33


def test_channel_coalesces_lifecycle_into_one_current_projection() -> None:
    async def exercise() -> None:
        channel = DashboardRunProgressChannel()
        channel.observe(
            RunProgressEvent(
                correlation_id=CORRELATION_ID,
                kind="scenario",
                status="running",
                completed_steps=0,
                total_steps=3,
            )
        )
        channel.observe(
            RunProgressEvent(
                correlation_id=CORRELATION_ID,
                kind="step",
                status="running",
                completed_steps=0,
                total_steps=3,
                activity="capturePageSnapshot",
                step_id="discover",
                position=1,
            )
        )
        channel.observe(
            RunProgressEvent(
                correlation_id=CORRELATION_ID,
                kind="operation",
                status="passed",
                total_steps=3,
                activity="capturePageSnapshot",
                step_id="discover",
                domain="ui",
                operation_phase="action",
                operation_type="click",
            )
        )
        await asyncio.sleep(0)
        current = await asyncio.wait_for(channel.next(), timeout=1)

        assert current.correlation_id == CORRELATION_ID
        assert current.phase_label == "Action"
        assert current.activity == "capturePageSnapshot"
        assert current.step_id == "discover"
        assert current.latest_operation == "Ui · Action · click · Passed"
        assert current.timeline[-1] == current.latest_operation

        channel.observe(
            RunProgressEvent(
                correlation_id=CORRELATION_ID,
                kind="step",
                status="passed",
                completed_steps=1,
                total_steps=3,
                activity="capturePageSnapshot",
                step_id="discover",
                position=1,
            )
        )
        await asyncio.sleep(0)
        completed = await asyncio.wait_for(channel.next(), timeout=1)

        assert completed.completed_steps == 1
        assert completed.progress_percent == EXPECTED_PROGRESS_PERCENT

    asyncio.run(exercise())


def test_channel_bounds_counts_timeline_and_untrusted_identifiers() -> None:
    async def exercise() -> None:
        channel = DashboardRunProgressChannel()
        for index in range(MAX_LIVE_PROGRESS_EVENTS + 2):
            channel.observe(
                RunProgressEvent(
                    correlation_id="not-a-correlation-id",
                    kind="operation",
                    status="passed",
                    completed_steps=MAX_LIVE_PROGRESS_STEPS + 1,
                    total_steps=MAX_LIVE_PROGRESS_STEPS + 1,
                    domain="api",
                    operation_phase="request",
                    operation_type=f"operation-{index}",
                )
            )
        await asyncio.sleep(0)
        current = await asyncio.wait_for(channel.next(), timeout=1)

        assert current.correlation_id == ""
        assert current.completed_steps == MAX_LIVE_PROGRESS_STEPS
        assert current.total_steps == MAX_LIVE_PROGRESS_STEPS
        assert len(current.timeline) == MAX_LIVE_PROGRESS_EVENTS
        assert "operation-0" not in "\n".join(current.timeline)

    asyncio.run(exercise())
