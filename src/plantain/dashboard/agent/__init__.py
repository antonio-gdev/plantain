"""Provider-neutral agent support for the local Plantain dashboard."""

from plantain.dashboard.agent.connection import (
    AgentConnection,
    AgentConnectionState,
    AgentProvider,
    load_agent_connection,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    AgentTokenUsage,
    IntentAction,
    IntentDecision,
    IntentRouteResult,
    IntentRoutingContext,
    UserIntent,
)
from plantain.dashboard.agent.router import IntentRouter
from plantain.dashboard.agent.transport import AgentTransport, AgentTransportError

__all__ = [
    "AgentCapability",
    "AgentConnection",
    "AgentConnectionState",
    "AgentProvider",
    "AgentResponseMetadata",
    "AgentTokenUsage",
    "AgentTransport",
    "AgentTransportError",
    "IntentAction",
    "IntentDecision",
    "IntentRouteResult",
    "IntentRouter",
    "IntentRoutingContext",
    "UserIntent",
    "load_agent_connection",
]
