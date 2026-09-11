"""Verified database discovery evidence for dashboard agent continuation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from plantain.config import Settings
from plantain.dashboard.agent.database_models import (
    DATABASE_DISCOVERY_OUTPUT,
    DatabaseDiscoveryRequest,
    DatabaseRunEvidence,
    DatabaseSourceReferences,
)
from plantain.dashboard.run_catalog import RunCatalogError, load_verified_run_document
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogError,
    resolve_scenario_path,
    scenario_id_for_source,
)
from plantain.engine.loader import load_scenario
from plantain.errors import ConfigurationError, PlantainError, ScenarioLoadError
from plantain.models.database import DatabaseDiscoveryResult, DiscoverDatabaseParams
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.security.redaction import REDACTED


class DatabaseEvidenceError(PlantainError):
    """Raised when a run cannot be trusted as database discovery evidence."""


def load_database_run_evidence(
    project_root: Path,
    run_id: str,
) -> DatabaseRunEvidence:
    """Load one passed metadata-only run without exposing its document to Reflex."""

    try:
        document = load_verified_run_document(project_root, run_id)
        report = document.value
        _require_passed_report(report)
        scenario = _load_report_scenario(project_root, report)
        step, params = _canonical_discovery_step(scenario)
        _require_matching_report_step(report, step)
        raw_result = _discovery_output(report)
        sanitized = document.redaction.redact_artifact(raw_result)
        if _contains_redaction(sanitized):
            raise DatabaseEvidenceError("Database discovery evidence contains protected metadata")
        discovery = DatabaseDiscoveryResult.model_validate(sanitized)
        _require_matching_scope(params, discovery)
        source = _source_references(step)
        return DatabaseRunEvidence(
            run_id=run_id,
            scenario=scenario.scenario,
            source_path=scenario.source_path or "",
            source=source,
            request=DatabaseDiscoveryRequest(
                phase=params.phase,
                schema_name=params.schema_name,
                table=params.table,
                include_views=params.include_views,
                include_system_schemas=params.include_system_schemas,
                page_size=params.page_size,
            ),
            discovery=discovery,
        )
    except (
        ConfigurationError,
        RunCatalogError,
        ScenarioCatalogError,
        ScenarioLoadError,
        ValidationError,
        OSError,
    ) as exc:
        raise DatabaseEvidenceError(
            "The selected database discovery run is unavailable or invalid"
        ) from exc


def _require_passed_report(report: Mapping[str, Any]) -> None:
    if report.get("status") != "passed":
        raise DatabaseEvidenceError("Database discovery evidence must come from a passed run")


def _load_report_scenario(
    project_root: Path,
    report: Mapping[str, Any],
) -> ScenarioDefinition:
    source_path = report.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise DatabaseEvidenceError("Database discovery report has no scenario source")
    scenario_id = scenario_id_for_source(source_path)
    path = resolve_scenario_path(project_root, scenario_id)
    settings = Settings.from_env(project_root)
    scenario = load_scenario(path, settings)
    if scenario.source_path != source_path or report.get("scenario") != scenario.scenario:
        raise DatabaseEvidenceError("Database discovery report does not match its scenario")
    return scenario


def _canonical_discovery_step(
    scenario: ScenarioDefinition,
) -> tuple[StepDefinition, DiscoverDatabaseParams]:
    if len(scenario.steps) != 1 or scenario.steps[0].activity != "discoverDatabase":
        raise DatabaseEvidenceError("Database continuation requires one metadata discovery step")
    step = scenario.steps[0]
    params = DiscoverDatabaseParams.model_validate(step.params)
    if scenario.outputs != {
        DATABASE_DISCOVERY_OUTPUT: f"${{{params.id}}}",
    }:
        raise DatabaseEvidenceError(
            "Database discovery scenario does not expose its complete evidence"
        )
    return step, params


def _require_matching_report_step(
    report: Mapping[str, Any],
    source_step: StepDefinition,
) -> None:
    steps = report.get("steps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise DatabaseEvidenceError("Database discovery report has invalid steps")
    step = steps[0]
    if not isinstance(step, Mapping) or (
        step.get("activity") != source_step.activity
        or step.get("step_id") != source_step.step_id
        or step.get("status") != "passed"
    ):
        raise DatabaseEvidenceError("Database discovery report does not match its step")


def _discovery_output(report: Mapping[str, Any]) -> Any:
    outputs = report.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {DATABASE_DISCOVERY_OUTPUT}:
        raise DatabaseEvidenceError("Database discovery report has invalid outputs")
    return outputs[DATABASE_DISCOVERY_OUTPUT]


def _source_references(step: StepDefinition) -> DatabaseSourceReferences:
    source = step.params.get("source")
    if not isinstance(source, Mapping):
        raise DatabaseEvidenceError("Database discovery source is invalid")
    return DatabaseSourceReferences.model_validate(dict(source))


def _require_matching_scope(
    params: DiscoverDatabaseParams,
    result: DatabaseDiscoveryResult,
) -> None:
    if result.phase is not params.phase or result.schema_name != params.schema_name:
        raise DatabaseEvidenceError("Database discovery result does not match its scope")
    if result.table_metadata is not None and result.table_metadata.name != params.table:
        raise DatabaseEvidenceError("Database table evidence does not match its scope")


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return REDACTED in value
    if isinstance(value, Mapping):
        return any(
            _contains_redaction(key) or _contains_redaction(item) for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_redaction(item) for item in value)
    return False


__all__ = ["DatabaseEvidenceError", "load_database_run_evidence"]
