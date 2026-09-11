"""Dependency-free types for observing dashboard agent usage."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from plantain.dashboard.agent.models import AgentResponseMetadata


class AgentUsageOperation(StrEnum):
    """Observable purpose of one dashboard-owned provider response."""

    CONNECTION_CHECK = "connection_check"
    INTENT_ROUTING = "intent_routing"
    SCENARIO_AUTHORING = "scenario_authoring"
    SCENARIO_REPAIR = "scenario_repair"
    DECISION_PLANNING = "decision_planning"
    DECISION_REPAIR = "decision_repair"


class AgentUsageObserver(Protocol):
    """Receive browser-safe metadata after one typed provider response."""

    def __call__(
        self,
        operation: AgentUsageOperation,
        metadata: AgentResponseMetadata,
        /,
    ) -> None: ...


__all__ = ["AgentUsageObserver", "AgentUsageOperation"]
