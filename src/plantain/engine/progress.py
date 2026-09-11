"""Typed, value-free lifecycle events for optional execution observers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

RunProgressKind = Literal["scenario", "step", "operation"]
RunProgressStatus = Literal["running", "passed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class RunProgressEvent:
    """One bounded lifecycle transition with no runtime values or raw evidence."""

    correlation_id: str
    kind: RunProgressKind
    status: RunProgressStatus
    completed_steps: int | None = None
    total_steps: int | None = None
    activity: str = ""
    step_id: str = ""
    position: int | None = None
    domain: str = ""
    operation_phase: str = ""
    operation_type: str = ""
    operation_position: int | None = None
    operation_total: int | None = None
    duration_ms: int | None = None
    error_type: str = ""


RunProgressObserver: TypeAlias = Callable[[RunProgressEvent], None]

__all__ = [
    "RunProgressEvent",
    "RunProgressKind",
    "RunProgressObserver",
    "RunProgressStatus",
]
