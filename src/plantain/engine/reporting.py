"""Scenario-level coordination for optional external result reporting."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from plantain.config import Settings
from plantain.models.scenario import ScenarioDefinition
from plantain.observability import get_logger
from plantain.reporting import PublicationResult, ResultReporter, ZephyrReporter
from plantain.security.secrets import SecretRegistry

if TYPE_CHECKING:
    from plantain.engine.admission import ResourceAdmission

logger = get_logger("engine.reporting")


class ScenarioReporting:
    """Own one lazy reporter or safely coordinate a shared batch reporter."""

    def __init__(
        self,
        settings: Settings,
        reporter: ResultReporter | None = None,
        *,
        admission: ResourceAdmission | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.reporter = (
            reporter
            if reporter is not None
            else ZephyrReporter(settings, admission=admission, environ=environ)
        )
        self.owns_reporter = reporter is None

    def validate(self, scenario: ScenarioDefinition) -> None:
        self.reporter.validate_scenario(scenario)

    async def publish(
        self,
        scenario: ScenarioDefinition,
        *,
        status: str,
        duration_ms: int,
        report: Mapping[str, Any],
        secrets: SecretRegistry,
    ) -> PublicationResult:
        try:
            return await self.reporter.publish(
                scenario,
                status=status,
                duration_ms=duration_ms,
                report=report,
                secrets=secrets,
            )
        except Exception as exc:  # noqa: BLE001 - reporting must not mask test status.
            logger.error(  # noqa: TRY400 - remote details are intentionally excluded.
                "Result reporting failed at the framework boundary",
                extra={
                    "scenario": scenario.scenario,
                    "integration": self.reporter.provider,
                    "publication_status": "failed",
                    "failure_stage": "framework",
                    "exception_type": type(exc).__name__,
                },
            )
            return PublicationResult(
                provider=self.reporter.provider,
                status="failed",
                failure_stage="framework",
                delivery_state="ambiguous",
                retry_status="manual_reconciliation",
                attachment_status="not_attempted",
            )

    async def close(self) -> bool:
        try:
            await self.reporter.close()
        except Exception as exc:  # noqa: BLE001 - cleanup cannot alter the test result.
            logger.error(  # noqa: TRY400 - transport details are intentionally excluded.
                "Result reporter cleanup failed",
                extra={
                    "integration": self.reporter.provider,
                    "failure_stage": "cleanup",
                    "exception_type": type(exc).__name__,
                },
            )
            return False
        return True


__all__ = ["ScenarioReporting"]
