"""Bounded live projections for one dashboard-owned scenario run."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field

from plantain.engine.progress import RunProgressEvent

MAX_LIVE_PROGRESS_EVENTS = 6
MAX_LIVE_PROGRESS_TEXT_LENGTH = 120
MAX_LIVE_PROGRESS_STEPS = 1_000
MILLISECONDS_PER_SECOND = 1_000
PERCENT_COMPLETE = 100
_CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class DashboardRunProgress:
    """Latest browser-safe projection of one independently running job."""

    correlation_id: str
    status: str
    phase_label: str
    completed_steps: int
    total_steps: int
    progress_percent: int
    activity: str
    step_id: str
    latest_operation: str
    timeline: tuple[str, ...]
    elapsed_ms: int


@dataclass(slots=True)
class _ProgressAccumulator:
    started: float = field(default_factory=time.monotonic)
    correlation_id: str = ""
    status: str = "queued"
    phase_label: str = "Waiting for runtime capacity"
    completed_steps: int = 0
    total_steps: int = 0
    activity: str = ""
    step_id: str = ""
    latest_operation: str = ""
    timeline: deque[str] = field(
        default_factory=lambda: deque(
            ("Waiting for available runtime capacity.",),
            maxlen=MAX_LIVE_PROGRESS_EVENTS,
        )
    )

    def apply(self, event: RunProgressEvent) -> DashboardRunProgress:
        """Apply one trusted engine event and return the latest safe snapshot."""

        if _CORRELATION_ID.fullmatch(event.correlation_id):
            self.correlation_id = event.correlation_id
        self.status = event.status
        self._apply_counts(event)
        if event.kind == "step":
            self.activity = _safe_text(event.activity)
            self.step_id = _safe_text(event.step_id)
        elif event.kind == "operation":
            self.latest_operation = _operation_label(event)
        self.phase_label = _phase_label(event, self.total_steps)
        self.timeline.append(_event_label(event, self.total_steps))
        return self.snapshot()

    def _apply_counts(self, event: RunProgressEvent) -> None:
        total = _bounded_count(event.total_steps)
        if total is not None:
            self.total_steps = total
        completed = _bounded_count(event.completed_steps)
        if completed is not None:
            self.completed_steps = min(completed, self.total_steps or completed)

    def snapshot(self) -> DashboardRunProgress:
        """Return an immutable projection of the accumulated lifecycle."""

        percent = 0
        if self.total_steps:
            percent = min(
                PERCENT_COMPLETE,
                round(self.completed_steps * PERCENT_COMPLETE / self.total_steps),
            )
        return DashboardRunProgress(
            correlation_id=self.correlation_id,
            status=self.status,
            phase_label=self.phase_label,
            completed_steps=self.completed_steps,
            total_steps=self.total_steps,
            progress_percent=percent,
            activity=self.activity,
            step_id=self.step_id,
            latest_operation=self.latest_operation,
            timeline=tuple(self.timeline),
            elapsed_ms=round((time.monotonic() - self.started) * MILLISECONDS_PER_SECOND),
        )


class DashboardRunProgressChannel:
    """Coalesce engine events into one latest snapshot without blocking a run."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._updates: asyncio.Queue[DashboardRunProgress] = asyncio.Queue(maxsize=1)
        self._accumulator = _ProgressAccumulator()

    def observe(self, event: RunProgressEvent) -> None:
        """Accept an event from any execution thread without retaining raw values."""

        try:
            self._loop.call_soon_threadsafe(self._record, event)
        except RuntimeError:
            return

    async def next(self) -> DashboardRunProgress:
        """Wait for the next coalesced progress snapshot."""

        return await self._updates.get()

    def _record(self, event: RunProgressEvent) -> None:
        snapshot = self._accumulator.apply(event)
        if self._updates.full():
            self._updates.get_nowait()
        self._updates.put_nowait(snapshot)


def _bounded_count(value: int | None) -> int | None:
    if value is None or isinstance(value, bool) or value < 0:
        return None
    return min(value, MAX_LIVE_PROGRESS_STEPS)


def _safe_text(value: str) -> str:
    rendered = " ".join(value.split())
    if len(rendered) <= MAX_LIVE_PROGRESS_TEXT_LENGTH:
        return rendered
    return f"{rendered[: MAX_LIVE_PROGRESS_TEXT_LENGTH - 1]}…"


def _humanize(value: str, fallback: str) -> str:
    rendered = _safe_text(value).replace("_", " ").replace("-", " ").strip()
    return rendered.title() if rendered else fallback


def _phase_label(event: RunProgressEvent, total_steps: int) -> str:
    if event.kind == "step" and event.position is not None:
        return f"Step {event.position} of {total_steps or '?'}"
    if event.kind == "operation":
        return _humanize(event.operation_phase, "Operation evidence")
    return "Execution started" if event.status == "running" else "Finalizing evidence"


def _operation_label(event: RunProgressEvent) -> str:
    parts = (
        _humanize(event.domain, "Activity"),
        _humanize(event.operation_phase, "Operation"),
        _safe_text(event.operation_type) or "Completed",
        _humanize(event.status, "Updated"),
    )
    return " · ".join(parts)


def _event_label(event: RunProgressEvent, total_steps: int) -> str:
    if event.kind == "step":
        position = event.position if event.position is not None else "?"
        activity = _safe_text(event.activity) or "activity"
        return (
            f"Step {position}/{total_steps or '?'} · {activity} · "
            f"{_humanize(event.status, 'Updated')}"
        )
    if event.kind == "operation":
        return _operation_label(event)
    return (
        "Scenario execution started."
        if event.status == "running"
        else f"Scenario {_humanize(event.status, 'finished').lower()}."
    )


__all__ = [
    "MAX_LIVE_PROGRESS_EVENTS",
    "DashboardRunProgress",
    "DashboardRunProgressChannel",
]
