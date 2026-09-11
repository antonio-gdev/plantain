"""Bounded, redacted scenario-step detail for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from plantain.config import Settings
from plantain.dashboard.reporting_profile import dashboard_reporting_runtime
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogError,
    is_valid_scenario_id,
    resolve_scenario_path,
)
from plantain.engine.loader import load_scenario
from plantain.errors import ConfigurationError, PlantainError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.security.redaction import RedactionPolicy

DEFAULT_SCENARIO_DETAIL_PAGE_SIZE = 12
MAX_SCENARIO_DETAIL_PAGE_SIZE = 24
MAX_SCENARIO_STEP_DETAIL_BYTES = 16_384
MAX_SCENARIO_DETAIL_LABEL_LENGTH = 256
_LIMIT_NOTICE = "\n# Preview limited by the dashboard safety boundary."


class ScenarioDetailError(RuntimeError):
    """Raised when one scenario cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class ScenarioStepDetail:
    """One browser-safe, reconstructed YAML step."""

    position: int
    activity: str
    step_id: str
    yaml_text: str
    content_limited: bool


@dataclass(frozen=True, slots=True)
class ScenarioDetailPage:
    """One deterministic page of scenario steps."""

    scenario_id: str
    name: str
    source: str
    steps: tuple[ScenarioStepDetail, ...]
    total_steps: int
    page: int
    page_count: int
    has_previous: bool
    has_next: bool
    notice: str


def load_scenario_detail(
    project_root: Path,
    scenario_id: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_SCENARIO_DETAIL_PAGE_SIZE,
) -> ScenarioDetailPage:
    """Resolve and reconstruct one bounded page from a current opaque ID."""

    _validate_request(scenario_id, page, page_size)
    root = _workspace_root(project_root)
    try:
        reporting = dashboard_reporting_runtime(Settings.from_env(root))
        settings = reporting.settings
        path = resolve_scenario_path(root, scenario_id)
        scenario = load_scenario(path, settings)
        return _detail_page(
            path,
            scenario_id,
            scenario,
            settings,
            page=page,
            page_size=page_size,
        )
    except (
        ConfigurationError,
        OSError,
        PlantainError,
        RuntimeError,
        ScenarioCatalogError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        raise ScenarioDetailError("The selected test details could not be loaded safely") from exc


def _validate_request(scenario_id: str, page: int, page_size: int) -> None:
    if not is_valid_scenario_id(scenario_id):
        raise ScenarioDetailError("The selected test identifier is invalid")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ScenarioDetailError("The test detail page must be a positive integer")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_SCENARIO_DETAIL_PAGE_SIZE
    ):
        raise ScenarioDetailError(
            f"The test detail page size must be between 1 and {MAX_SCENARIO_DETAIL_PAGE_SIZE}"
        )


def _workspace_root(project_root: Path) -> Path:
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise ScenarioDetailError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise ScenarioDetailError("The dashboard workspace is unavailable")
    return root


def _detail_page(
    path: Path,
    scenario_id: str,
    scenario: ScenarioDefinition,
    settings: Settings,
    *,
    page: int,
    page_size: int,
) -> ScenarioDetailPage:
    total_steps = len(scenario.steps)
    page_count = (total_steps + page_size - 1) // page_size
    effective_page = min(page, page_count)
    start = (effective_page - 1) * page_size
    redaction = RedactionPolicy(settings.sensitive_key_names)
    steps = tuple(
        _step_detail(step, start + offset + 1, redaction)
        for offset, step in enumerate(scenario.steps[start : start + page_size])
    )
    limited_count = sum(step.content_limited for step in steps)
    return ScenarioDetailPage(
        scenario_id=scenario_id,
        name=_safe_label(scenario.scenario, redaction, "Unnamed test"),
        source=_safe_source(path, settings, redaction),
        steps=steps,
        total_steps=total_steps,
        page=effective_page,
        page_count=page_count,
        has_previous=effective_page > 1,
        has_next=effective_page < page_count,
        notice=_detail_notice(start, len(steps), total_steps, limited_count),
    )


def _step_detail(
    step: StepDefinition,
    position: int,
    redaction: RedactionPolicy,
) -> ScenarioStepDetail:
    parameters = redaction.redact_artifact(step.params)
    yaml_text = yaml.safe_dump(
        [{step.activity: parameters}],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    bounded, limited = _bounded_yaml(yaml_text)
    return ScenarioStepDetail(
        position=position,
        activity=_safe_label(step.activity, redaction, "Activity"),
        step_id=_safe_label(step.step_id or "", redaction, ""),
        yaml_text=bounded,
        content_limited=limited,
    )


def _bounded_yaml(value: str) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= MAX_SCENARIO_STEP_DETAIL_BYTES:
        return value, False
    marker = _LIMIT_NOTICE.encode()
    allowance = max(0, MAX_SCENARIO_STEP_DETAIL_BYTES - len(marker))
    prefix = encoded[:allowance].decode(errors="ignore").rstrip()
    return f"{prefix}{_LIMIT_NOTICE}", True


def _safe_source(
    path: Path,
    settings: Settings,
    redaction: RedactionPolicy,
) -> str:
    relative = path.relative_to(settings.scenarios_dir.resolve()).as_posix()
    return _safe_label(relative, redaction, "Unnamed test file")


def _safe_label(
    value: str,
    redaction: RedactionPolicy,
    fallback: str,
) -> str:
    sanitized = redaction.redact_text(value).strip()
    return sanitized[:MAX_SCENARIO_DETAIL_LABEL_LENGTH] or fallback


def _detail_notice(
    start: int,
    visible_count: int,
    total_steps: int,
    limited_count: int,
) -> str:
    notice = f"Showing steps {start + 1}-{start + visible_count} of {total_steps}."
    if limited_count:
        noun = "preview" if limited_count == 1 else "previews"
        notice += f" {limited_count} {noun} reached the explicit byte limit."
    return notice


__all__ = [
    "DEFAULT_SCENARIO_DETAIL_PAGE_SIZE",
    "MAX_SCENARIO_DETAIL_PAGE_SIZE",
    "MAX_SCENARIO_STEP_DETAIL_BYTES",
    "ScenarioDetailError",
    "ScenarioDetailPage",
    "ScenarioStepDetail",
    "load_scenario_detail",
]
