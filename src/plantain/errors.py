"""Framework exception hierarchy with safe, actionable messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PlantainError(Exception):
    """Base class for expected framework failures."""


class ConfigurationError(PlantainError):
    """Raised when runtime configuration is missing or invalid."""


class ScenarioLoadError(PlantainError):
    """Raised when a scenario cannot be safely loaded or validated."""


class ExpressionResolutionError(PlantainError):
    """Raised when an environment, random, or context expression cannot be resolved."""


class ActivityRegistrationError(PlantainError):
    """Raised when an internal activity conflicts with the registry contract."""


class ActivityValidationError(PlantainError):
    """Raised when an activity's YAML parameters violate its schema."""


class ActivityExecutionError(PlantainError):
    """Raised when an activity fails after its parameters have been validated."""


class AtomicPersistenceError(PlantainError):
    """Raised when durable atomic persistence cannot be guaranteed."""


class AtomicTargetExistsError(AtomicPersistenceError):
    """Raised when create-only atomic persistence finds an existing target."""


class AtomicCommitUncertainError(AtomicPersistenceError):
    """Raised after replacement when directory durability cannot be confirmed."""


class BrowserError(PlantainError):
    """Raised for browser lifecycle or page interaction failures."""


class LocatorResolutionError(BrowserError):
    """Raised when a locator is missing or ambiguous."""


class AssertionFailureError(BrowserError, AssertionError):
    """Raised when a declarative UI assertion fails."""


class SnapshotError(PlantainError):
    """Raised when semantic capture or registry reconciliation fails."""


class ApiError(PlantainError):
    """Raised for safe HTTP client failures."""


class OpenApiError(ApiError):
    """Raised when an OpenAPI document or operation is invalid."""


class DatabasePolicyError(PlantainError):
    """Raised when a database operation violates an execution policy."""


class DatabaseDiscoveryError(PlantainError):
    """Raised when database inspection or a read-only query fails."""


@dataclass(frozen=True, slots=True)
class StepFailure:
    """Serializable, non-secret failure details for one scenario step."""

    activity: str
    step_id: str
    message: str
    exception_type: str
    details: dict[str, Any] | None = None
