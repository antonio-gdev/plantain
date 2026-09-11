"""UI-specific failures with safe, actionable messages."""

from __future__ import annotations

from collections.abc import Mapping


class UiAutomationError(RuntimeError):
    """Base class for browser activity failures."""

    def __init__(
        self,
        message: str,
        *,
        safe_details: Mapping[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_details = dict(safe_details or {})


class BrowserLifecycleError(UiAutomationError):
    """Raised when the browser or context cannot be initialized safely."""


class LocatorResolutionError(UiAutomationError):
    """Raised when a locator is absent or ambiguous."""


class UiActionError(UiAutomationError):
    """Raised when a declarative UI action cannot complete."""


class UiAssertionError(UiAutomationError):
    """Raised when a declarative verification fails."""


class SnapshotError(UiAutomationError):
    """Raised when semantic DOM capture or registration fails."""


class SnapshotConsistencyError(SnapshotError):
    """Raised when a semantic capture attempt observes a changing document."""
