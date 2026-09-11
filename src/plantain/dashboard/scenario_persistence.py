"""Validated, collision-safe persistence for dashboard scenario drafts."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from plantain.activities import register_framework_activities
from plantain.config import Settings
from plantain.dashboard.agent.models import AgentCapability
from plantain.dashboard.draft_store import (
    DashboardDraftError,
    ScenarioDraftRecord,
    discard_scenario_draft,
    load_scenario_draft,
)
from plantain.dashboard.scenario_catalog import scenario_id_for_source
from plantain.dashboard.scenario_paths import (
    DashboardScenarioPathError,
    normalize_scenario_directory,
    scenario_filename_stem,
)
from plantain.engine.loader import parse_scenario_text
from plantain.engine.registry import ActivityRegistry
from plantain.errors import (
    ActivityRegistrationError,
    ActivityValidationError,
    AtomicCommitUncertainError,
    AtomicPersistenceError,
    AtomicTargetExistsError,
    ConfigurationError,
    PlantainError,
    ScenarioLoadError,
)
from plantain.models.scenario import ScenarioDefinition
from plantain.persistence import write_source_bytes_atomic_new

SAVE_COLLISION_ATTEMPTS = 8
SAVE_SUFFIX_BYTES = 4


class DashboardScenarioSaveError(PlantainError):
    """Raised when a reviewed dashboard draft cannot be saved safely."""


@dataclass(frozen=True, slots=True)
class SavedScenario:
    """Browser-safe identity for one newly persisted scenario."""

    scenario_id: str
    scenario: str
    relative_path: str
    capability: AgentCapability
    step_count: int
    activities: tuple[str, ...]


def save_scenario_draft(
    project_root: Path,
    draft_id: str,
    *,
    directory: str | None = None,
) -> SavedScenario:
    """Revalidate and atomically save one backend-owned scenario draft."""

    settings = _save_settings(project_root)
    record = _load_draft(draft_id)
    definition = _validate_draft(record, settings)
    requested = directory
    if requested is None:
        requested = record.draft.suggested_directory
    try:
        relative_directory = normalize_scenario_directory(
            requested,
            record.capability,
        )
    except DashboardScenarioPathError as exc:
        raise DashboardScenarioSaveError("Choose a folder inside the scenarios collection") from exc
    return _persist_draft(
        settings,
        record,
        definition,
        relative_directory,
    )


def _save_settings(project_root: Path) -> Settings:
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise DashboardScenarioSaveError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise DashboardScenarioSaveError("The dashboard workspace is unavailable")
    try:
        return Settings.from_env(root)
    except (ConfigurationError, OSError) as exc:
        raise DashboardScenarioSaveError("The dashboard scenario configuration is invalid") from exc


def _load_draft(draft_id: str) -> ScenarioDraftRecord:
    try:
        return load_scenario_draft(draft_id)
    except DashboardDraftError as exc:
        raise DashboardScenarioSaveError(
            "This scenario draft expired; create it again to continue"
        ) from exc


def _validate_draft(
    record: ScenarioDraftRecord,
    settings: Settings,
) -> ScenarioDefinition:
    try:
        definition = parse_scenario_text(record.draft.yaml_text, settings)
        registry = ActivityRegistry()
        register_framework_activities(registry)
        for step in definition.steps:
            registry.validate(step.activity, step.params)
    except (
        ActivityRegistrationError,
        ActivityValidationError,
        ScenarioLoadError,
    ) as exc:
        raise DashboardScenarioSaveError(
            "The reviewed scenario no longer passes local validation"
        ) from exc
    activities = list(dict.fromkeys(step.activity for step in definition.steps))
    if (
        definition.scenario != record.draft.scenario
        or len(definition.steps) != record.draft.step_count
        or activities != record.draft.activities
    ):
        raise DashboardScenarioSaveError(
            "The reviewed scenario metadata no longer matches its content"
        )
    return definition


def _persist_draft(
    settings: Settings,
    record: ScenarioDraftRecord,
    definition: ScenarioDefinition,
    relative_directory: str,
) -> SavedScenario:
    stem = scenario_filename_stem(definition.scenario)
    source = record.draft.yaml_text
    payload = (source if source.endswith("\n") else f"{source}\n").encode()
    for attempt in range(SAVE_COLLISION_ATTEMPTS):
        filename = _candidate_filename(stem, attempt)
        relative_path = (Path(relative_directory) / filename).as_posix()
        target = settings.scenarios_dir / relative_path
        try:
            write_source_bytes_atomic_new(target, payload)
        except AtomicTargetExistsError:
            continue
        except AtomicCommitUncertainError as exc:
            raise DashboardScenarioSaveError(
                "The scenario may have been saved, but durability could not be confirmed"
            ) from exc
        except AtomicPersistenceError as exc:
            raise DashboardScenarioSaveError("The scenario could not be saved safely") from exc
        discard_scenario_draft(record.draft_id)
        return SavedScenario(
            scenario_id=scenario_id_for_source(relative_path),
            scenario=definition.scenario,
            relative_path=relative_path,
            capability=record.capability,
            step_count=len(definition.steps),
            activities=tuple(step.activity for step in definition.steps),
        )
    raise DashboardScenarioSaveError("Plantain could not allocate a unique scenario filename")


def _candidate_filename(stem: str, attempt: int) -> str:
    if attempt == 0:
        return f"{stem}.yaml"
    return f"{stem}-{secrets.token_hex(SAVE_SUFFIX_BYTES)}.yaml"


__all__ = [
    "DashboardScenarioSaveError",
    "SavedScenario",
    "save_scenario_draft",
]
