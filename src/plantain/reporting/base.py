"""Typed boundary for post-scenario result reporting integrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from plantain.models.scenario import ScenarioDefinition
from plantain.security.secrets import SecretRegistry


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """Secret-free outcome retained in the local scenario report."""

    provider: str
    status: str
    http_status: int | None = None
    execution_id: str | None = None
    failure_stage: str | None = None
    delivery_state: str | None = None
    retry_status: str | None = None
    outbox_id: str | None = None
    attachment_status: str | None = None
    attachment_http_status: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        """Return the complete stable reporting envelope."""

        return asdict(self)


class ResultReporter(Protocol):
    """Lifecycle implemented by an internal result-reporting integration."""

    @property
    def provider(self) -> str:
        """Return the stable provider identifier used in reports."""

    def validate_scenario(self, scenario: ScenarioDefinition) -> None:
        """Validate static configuration and scenario metadata without network access."""

    async def publish(
        self,
        scenario: ScenarioDefinition,
        *,
        status: str,
        duration_ms: int,
        report: Mapping[str, Any],
        secrets: SecretRegistry,
    ) -> PublicationResult:
        """Publish one result and return a secret-free, non-raising outcome."""

    async def close(self) -> None:
        """Release pooled transport resources."""


__all__ = ["PublicationResult", "ResultReporter"]
