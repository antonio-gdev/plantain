"""Verified semantic UI evidence for dashboard discovery continuation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from plantain.activities.snapshot_registry_complete import (
    CompleteSnapshotRegistry,
    DiagnosticSnapshotExcerpt,
    VerifiedSnapshotExcerpt,
)
from plantain.config import Settings
from plantain.dashboard.agent.ui_models import (
    MAX_UI_DOM_EXCERPT_BYTES,
    UI_DISCOVERY_OUTPUT,
    UiEvidenceState,
    UiFailureEvidence,
    UiFailureStage,
    UiRunEvidence,
)
from plantain.dashboard.run_catalog import (
    RunCatalogError,
    VerifiedRunDocument,
    load_verified_run_document,
)
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogError,
    resolve_scenario_path,
    scenario_id_for_source,
)
from plantain.engine.loader import load_scenario
from plantain.errors import ConfigurationError, PlantainError, ScenarioLoadError, SnapshotError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.models.ui import CapturePageSnapshotParams
from plantain.security.redaction import RedactionPolicy

_SNAPSHOT_OUTPUT = re.compile(r"^\$\{([A-Za-z][A-Za-z0-9_-]*)\.snapshots\[0\]\.canonicalFile\}$")
_MAX_CANONICAL_FILENAME_LENGTH = 255
_SNAPSHOT_PREFIX_PART_COUNT = 2
_REPORT_TO_REGISTRY_STAGE: dict[str, UiFailureStage] = {
    "ui_action": "action",
    "ui_verification": "verification",
}


class UiEvidenceError(PlantainError):
    """Raised when a run cannot be trusted as UI discovery evidence."""


def load_ui_run_evidence(
    project_root: Path,
    run_id: str,
) -> UiRunEvidence:
    """Load exact verified or repairable UI evidence outside Reflex state."""

    try:
        document = load_verified_run_document(project_root, run_id)
        settings = Settings.from_env(project_root)
        scenario = _load_report_scenario(project_root, document.value, settings)
        registry = CompleteSnapshotRegistry(settings.snapshots_dir)
        if document.value.get("status") == "passed":
            return _passed_evidence(document, scenario, registry, run_id)
        if document.value.get("status") == "failed":
            return _diagnostic_evidence(document, scenario, registry, run_id)
        raise UiEvidenceError("UI discovery evidence has an unsupported run status")
    except (
        ConfigurationError,
        RunCatalogError,
        ScenarioCatalogError,
        ScenarioLoadError,
        SnapshotError,
        ValidationError,
        OSError,
    ) as exc:
        raise UiEvidenceError("The selected UI discovery run is unavailable or invalid") from exc


def _load_report_scenario(
    project_root: Path,
    report: Mapping[str, Any],
    settings: Settings,
) -> ScenarioDefinition:
    source_path = report.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise UiEvidenceError("UI discovery report has no scenario source")
    path = resolve_scenario_path(project_root, scenario_id_for_source(source_path))
    scenario = load_scenario(path, settings)
    if scenario.source_path != source_path or report.get("scenario") != scenario.scenario:
        raise UiEvidenceError("UI discovery report does not match its scenario")
    return scenario


def _passed_evidence(
    document: VerifiedRunDocument,
    scenario: ScenarioDefinition,
    registry: CompleteSnapshotRegistry,
    run_id: str,
) -> UiRunEvidence:
    if document.value.get("failure") is not None:
        raise UiEvidenceError("Passed UI discovery evidence contains a failure")
    step, params = _output_step(scenario)
    _require_report_step(document.value, step, "passed")
    outputs = document.value.get("outputs")
    if not isinstance(outputs, Mapping):
        raise UiEvidenceError("UI discovery report has invalid outputs")
    canonical_file = _canonical_filename(outputs.get(UI_DISCOVERY_OUTPUT))
    excerpt = registry.read_verified_excerpt(
        canonical_file,
        max_bytes=MAX_UI_DOM_EXCERPT_BYTES,
    )
    activity = _activity_name(step, params)
    if activity not in excerpt.summary.activities:
        raise UiEvidenceError("Verified UI evidence does not match its activity")
    return _verified_model(
        document=document,
        scenario=scenario,
        run_id=run_id,
        step=step,
        parameters=params,
        activity=activity,
        excerpt=excerpt,
    )


def _diagnostic_evidence(
    document: VerifiedRunDocument,
    scenario: ScenarioDefinition,
    registry: CompleteSnapshotRegistry,
    run_id: str,
) -> UiRunEvidence:
    failure = document.value.get("failure")
    if not isinstance(failure, Mapping) or failure.get("activity") != "capturePageSnapshot":
        raise UiEvidenceError("Failed UI discovery evidence has invalid attribution")
    step_id = failure.get("step_id")
    if not isinstance(step_id, str):
        raise UiEvidenceError("Failed UI discovery evidence has no step identity")
    step, params = _scenario_step(scenario, step_id)
    _require_report_step(document.value, step, "failed")
    details = failure.get("details")
    if not isinstance(details, Mapping) or details.get("evidence_state") != "diagnostic":
        raise UiEvidenceError("Failed UI discovery run has no repairable diagnostic")
    report_stage = details.get("failure_stage")
    if not isinstance(report_stage, str) or report_stage not in _REPORT_TO_REGISTRY_STAGE:
        raise UiEvidenceError("Failed UI discovery stage is not repairable")
    failure_stage = _REPORT_TO_REGISTRY_STAGE[report_stage]
    canonical_file = _canonical_filename(details.get("diagnostic_snapshot"))
    activity = _activity_name(step, params)
    excerpt = registry.read_diagnostic_excerpt(
        canonical_file,
        activity,
        failure_stage,
        max_bytes=MAX_UI_DOM_EXCERPT_BYTES,
    )
    failure_evidence = _failure_evidence(details, document.redaction)
    return _diagnostic_model(
        document=document,
        scenario=scenario,
        run_id=run_id,
        step=step,
        parameters=params,
        activity=activity,
        excerpt=excerpt,
        failure=failure_evidence,
    )


def _output_step(
    scenario: ScenarioDefinition,
) -> tuple[StepDefinition, CapturePageSnapshotParams]:
    reference = scenario.outputs.get(UI_DISCOVERY_OUTPUT)
    if not isinstance(reference, str):
        raise UiEvidenceError("UI discovery scenario has no snapshot evidence output")
    match = _SNAPSHOT_OUTPUT.fullmatch(reference)
    if match is None:
        raise UiEvidenceError("UI discovery snapshot output is invalid")
    return _scenario_step(scenario, match.group(1))


def _scenario_step(
    scenario: ScenarioDefinition,
    step_id: str,
) -> tuple[StepDefinition, CapturePageSnapshotParams]:
    matches = [step for step in scenario.steps if step.step_id == step_id]
    if len(matches) != 1 or matches[0].activity != "capturePageSnapshot":
        raise UiEvidenceError("UI discovery step identity is invalid")
    step = matches[0]
    return step, CapturePageSnapshotParams.model_validate(step.params)


def _require_report_step(
    report: Mapping[str, Any],
    source_step: StepDefinition,
    status: str,
) -> None:
    steps = report.get("steps")
    if not isinstance(steps, list):
        raise UiEvidenceError("UI discovery report has invalid steps")
    matches = [
        step
        for step in steps
        if isinstance(step, Mapping) and step.get("step_id") == source_step.step_id
    ]
    if len(matches) != 1 or (
        matches[0].get("activity") != source_step.activity or matches[0].get("status") != status
    ):
        raise UiEvidenceError("UI discovery report does not match its step")


def _activity_name(
    step: StepDefinition,
    params: CapturePageSnapshotParams,
) -> str:
    activity = params.activity or step.step_id
    if activity is None:
        raise UiEvidenceError("UI discovery step has no stable activity identity")
    return activity


def _canonical_filename(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise UiEvidenceError("UI discovery snapshot reference is invalid")
    parts = PurePosixPath(value).parts
    if len(parts) == 1:
        filename = parts[0]
    elif len(parts) == _SNAPSHOT_PREFIX_PART_COUNT and parts[0] == "snapshots":
        filename = parts[1]
    else:
        raise UiEvidenceError("UI discovery snapshot reference is invalid")
    if (
        filename in {".", ".."}
        or not filename.endswith(".semantic.json")
        or len(filename) > _MAX_CANONICAL_FILENAME_LENGTH
    ):
        raise UiEvidenceError("UI discovery snapshot reference is invalid")
    return filename


def _failure_evidence(
    details: Mapping[str, Any],
    redaction: RedactionPolicy,
) -> UiFailureEvidence:
    return UiFailureEvidence(
        operation_index=_required_integer(details, "operation_index"),
        operation_total=_required_integer(details, "operation_total"),
        operation_type=_redacted_string(details, "operation_type", redaction),
        operation_target=_redacted_string(details, "operation_target", redaction),
        operation_error_type=_redacted_string(
            details,
            "operation_error_type",
            redaction,
        ),
    )


def _required_integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise UiEvidenceError("UI diagnostic operation metadata is invalid")
    return item


def _redacted_string(
    value: Mapping[str, Any],
    key: str,
    redaction: RedactionPolicy,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise UiEvidenceError("UI diagnostic operation metadata is invalid")
    sanitized = redaction.redact_artifact(item)
    if not isinstance(sanitized, str):
        raise UiEvidenceError("UI diagnostic operation metadata is invalid")
    return sanitized


def _verified_model(
    *,
    document: VerifiedRunDocument,
    scenario: ScenarioDefinition,
    run_id: str,
    step: StepDefinition,
    parameters: CapturePageSnapshotParams,
    activity: str,
    excerpt: VerifiedSnapshotExcerpt,
) -> UiRunEvidence:
    return _evidence_model(
        document=document,
        scenario=scenario,
        run_id=run_id,
        step=step,
        parameters=parameters,
        activity=activity,
        canonical_file=excerpt.summary.canonical_file,
        url=excerpt.summary.url,
        page_title=excerpt.summary.page_title,
        content=excerpt.content,
        truncated=excerpt.truncated,
        evidence_state="verified",
    )


def _diagnostic_model(
    *,
    document: VerifiedRunDocument,
    scenario: ScenarioDefinition,
    run_id: str,
    step: StepDefinition,
    parameters: CapturePageSnapshotParams,
    activity: str,
    excerpt: DiagnosticSnapshotExcerpt,
    failure: UiFailureEvidence,
) -> UiRunEvidence:
    return _evidence_model(
        document=document,
        scenario=scenario,
        run_id=run_id,
        step=step,
        parameters=parameters,
        activity=activity,
        canonical_file=excerpt.canonical_file,
        url=excerpt.url,
        page_title=excerpt.page_title,
        content=excerpt.content,
        truncated=excerpt.truncated,
        evidence_state="diagnostic",
        failure_stage=excerpt.failure_stage,
        failure=failure,
    )


def _evidence_model(
    *,
    document: VerifiedRunDocument,
    scenario: ScenarioDefinition,
    run_id: str,
    step: StepDefinition,
    parameters: CapturePageSnapshotParams,
    activity: str,
    canonical_file: str,
    url: str,
    page_title: str,
    content: str,
    truncated: bool,
    evidence_state: UiEvidenceState,
    failure_stage: UiFailureStage | None = None,
    failure: UiFailureEvidence | None = None,
) -> UiRunEvidence:
    sanitized_content = document.redaction.redact_artifact(content)
    sanitized_title = document.redaction.redact_artifact(page_title)
    if not isinstance(sanitized_content, str) or not isinstance(sanitized_title, str):
        raise UiEvidenceError("UI evidence contains invalid text")
    return UiRunEvidence(
        run_id=run_id,
        scenario=scenario.scenario,
        source_path=scenario.source_path or "",
        step_id=cast("str", step.step_id),
        activity=activity,
        canonical_file=canonical_file,
        evidence_state=evidence_state,
        failure_stage=failure_stage,
        failure=failure,
        parameters=parameters,
        url=document.redaction.redact_url(url),
        page_title=sanitized_title,
        content=sanitized_content,
        truncated=truncated,
    )


__all__ = ["UiEvidenceError", "load_ui_run_evidence"]
