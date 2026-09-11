"""Bounded, browser-safe catalog of reviewable Plantain scenarios."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from plantain.activities import register_framework_activities
from plantain.config import Settings
from plantain.dashboard.reporting_profile import (
    DashboardReportingRuntime,
    dashboard_reporting_runtime,
)
from plantain.engine.loader import discover_scenario_paths, load_scenario
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioRunner
from plantain.engine.selection import ScenarioTagFilter
from plantain.errors import (
    ActivityRegistrationError,
    ConfigurationError,
    PlantainError,
    ScenarioLoadError,
)
from plantain.models.scenario import ScenarioDefinition
from plantain.security.redaction import RedactionPolicy

DEFAULT_SCENARIO_PAGE_SIZE = 24
MAX_SCENARIO_PAGE_SIZE = 50
MAX_CATALOG_LABEL_LENGTH = 160
MAX_DISPLAY_TAGS = 3
MAX_SCENARIO_SEARCH_MATCHES = 12
MAX_SCENARIO_SEARCH_QUERY_LENGTH = 240
_SCENARIO_ID = re.compile(r"^[0-9a-f]{64}$")
_SEARCH_TOKEN = re.compile(r"[a-z0-9]+")
_ACTIVITY_DOMAINS = {
    "capturePageSnapshot": "UI",
    "sendRequest": "API",
    "loadApiSchema": "API",
    "callSchema": "API",
    "validateSchema": "API",
    "discoverDatabase": "Database",
    "queryDatabase": "Database",
    "verifyDatabaseResult": "Database",
}
_DOMAIN_ORDER = ("UI", "API", "Database", "Framework")


class ScenarioCatalogError(RuntimeError):
    """Raised when the dashboard cannot safely project the scenario catalog."""


@dataclass(frozen=True, slots=True)
class ScenarioCatalogFilter:
    """Canonical search and tag predicates for one dashboard catalog view."""

    query: str
    tag_filter: ScenarioTagFilter

    @classmethod
    def create(
        cls,
        *,
        query: str = "",
        required_all: Iterable[str] = (),
        required_any: Iterable[str] = (),
        excluded: Iterable[str] = (),
    ) -> ScenarioCatalogFilter:
        try:
            tag_filter = ScenarioTagFilter.create(
                required_all=required_all,
                required_any=required_any,
                excluded=excluded,
            )
        except ValueError as exc:
            raise ScenarioCatalogError("The test tag filters are invalid") from exc
        return cls(
            query=_normalized_catalog_query(query),
            tag_filter=tag_filter,
        )

    @property
    def active(self) -> bool:
        """Return whether this view narrows the discovered collection."""

        return bool(self.query or self.tag_filter.active)


@dataclass(frozen=True, slots=True)
class ScenarioCatalogItem:
    """Browser-safe summary of one scenario file."""

    scenario_id: str
    name: str
    source: str
    status: str
    step_count: int
    domains: tuple[str, ...]
    tags: tuple[str, ...]
    additional_tag_count: int
    issue: str


@dataclass(frozen=True, slots=True)
class ScenarioCatalogPage:
    """One deterministic page from the bounded scenario collection."""

    items: tuple[ScenarioCatalogItem, ...]
    total_count: int
    page: int
    page_count: int
    has_previous: bool
    has_next: bool
    notice: str


@dataclass(frozen=True, slots=True)
class ScenarioCatalogSearch:
    """Bounded deterministic matches for one local scenario query."""

    items: tuple[ScenarioCatalogItem, ...]
    total_matches: int
    matches_limited: bool


@dataclass(frozen=True, slots=True)
class ScenarioCatalogSelection:
    """Complete bounded matching set retained only by the backend operation."""

    items: tuple[ScenarioCatalogItem, ...]
    ready_count: int
    review_count: int


@dataclass(frozen=True, slots=True)
class _ScenarioCatalogRecord:
    item: ScenarioCatalogItem
    definition: ScenarioDefinition | None


def load_scenario_catalog(
    project_root: Path,
    *,
    page: int = 1,
    page_size: int = DEFAULT_SCENARIO_PAGE_SIZE,
    catalog_filter: ScenarioCatalogFilter | None = None,
) -> ScenarioCatalogPage:
    """Load and statically validate one bounded page of scenario summaries."""

    _validate_page_request(page, page_size)
    reporting, paths = _catalog_inputs(project_root)
    settings = reporting.settings
    effective_filter = catalog_filter or ScenarioCatalogFilter.create()
    filtered_items: tuple[ScenarioCatalogItem, ...] | None = None
    if effective_filter.active:
        filtered_items = _catalog_selection(reporting, paths, effective_filter).items
    total_count = len(filtered_items) if filtered_items is not None else len(paths)
    page_count = (total_count + page_size - 1) // page_size if total_count else 0
    effective_page = min(page, page_count) if page_count else 1
    start = (effective_page - 1) * page_size
    items: tuple[ScenarioCatalogItem, ...] = ()
    if filtered_items is not None:
        items = filtered_items[start : start + page_size]
    else:
        visible = paths[start : start + page_size]
        if visible:
            validator = _scenario_validator(reporting)
            redaction = RedactionPolicy(settings.sensitive_key_names)
            items = tuple(
                _scenario_summary(path, settings, validator, redaction) for path in visible
            )
    invalid_count = sum(item.status == "needs_review" for item in items)
    notice = _catalog_notice(
        total_count=total_count,
        start=start,
        visible_count=len(items),
        page_size=page_size,
        invalid_count=invalid_count,
    )
    return ScenarioCatalogPage(
        items=items,
        total_count=total_count,
        page=effective_page,
        page_count=page_count,
        has_previous=effective_page > 1,
        has_next=effective_page < page_count,
        notice=notice,
    )


def load_scenario_selection(
    project_root: Path,
    *,
    catalog_filter: ScenarioCatalogFilter | None = None,
) -> ScenarioCatalogSelection:
    """Load the complete bounded matching set for validation or batch execution."""

    reporting, paths = _catalog_inputs(project_root)
    effective_filter = catalog_filter or ScenarioCatalogFilter.create()
    return _catalog_selection(reporting, paths, effective_filter)


def search_scenario_catalog(
    project_root: Path,
    query: str,
    *,
    limit: int = MAX_SCENARIO_SEARCH_MATCHES,
) -> ScenarioCatalogSearch:
    """Return bounded safe matches without granting authority through a path."""

    normalized = _normalized_search_query(query)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > MAX_SCENARIO_SEARCH_MATCHES
    ):
        raise ScenarioCatalogError(
            f"The scenario search limit must be between 1 and {MAX_SCENARIO_SEARCH_MATCHES}"
        )
    reporting, paths = _catalog_inputs(project_root)
    settings = reporting.settings
    broad = normalized.casefold() in {"*", "all", "all scenarios", "all tests"}
    ranked: list[tuple[int, str, Path]] = []
    for path in paths:
        relative = _relative_source(path, settings)
        score = 1 if broad else _scenario_search_score(relative, normalized)
        if score:
            ranked.append((score, relative, path))
    ranked.sort(key=lambda item: (-item[0], item[1].casefold(), item[1]))
    visible = ranked[:limit]
    items: tuple[ScenarioCatalogItem, ...] = ()
    if visible:
        validator = _scenario_validator(reporting)
        redaction = RedactionPolicy(settings.sensitive_key_names)
        items = tuple(
            _scenario_summary(path, settings, validator, redaction)
            for _score, _relative, path in visible
        )
    return ScenarioCatalogSearch(
        items=items,
        total_matches=len(ranked),
        matches_limited=len(ranked) > limit,
    )


def is_valid_scenario_id(value: object) -> bool:
    """Return whether a browser value is a canonical opaque scenario identifier."""

    return isinstance(value, str) and _SCENARIO_ID.fullmatch(value) is not None


def resolve_scenario_path(project_root: Path, scenario_id: str) -> Path:
    """Resolve one opaque browser identifier through current safe discovery."""

    if not is_valid_scenario_id(scenario_id):
        raise ScenarioCatalogError("The selected test identifier is invalid")
    reporting, paths = _catalog_inputs(project_root)
    settings = reporting.settings
    for path in paths:
        relative = _relative_source(path, settings)
        if hmac.compare_digest(scenario_id_for_source(relative), scenario_id):
            return path
    raise ScenarioCatalogError("The selected test is no longer available")


def _validate_page_request(page: int, page_size: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ScenarioCatalogError("The test catalog page must be a positive integer")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_SCENARIO_PAGE_SIZE
    ):
        raise ScenarioCatalogError(
            f"The test catalog page size must be between 1 and {MAX_SCENARIO_PAGE_SIZE}"
        )


def _catalog_inputs(
    project_root: Path,
) -> tuple[DashboardReportingRuntime, tuple[Path, ...]]:
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise ScenarioCatalogError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise ScenarioCatalogError("The dashboard workspace is unavailable")
    try:
        reporting = dashboard_reporting_runtime(Settings.from_env(root))
    except (ConfigurationError, OSError) as exc:
        raise ScenarioCatalogError("The dashboard test catalog configuration is invalid") from exc
    settings = reporting.settings
    scenario_root = settings.scenarios_dir
    if scenario_root.is_symlink():
        raise ScenarioCatalogError("The dashboard test collection is invalid")
    if not scenario_root.exists():
        return reporting, ()
    if not scenario_root.is_dir():
        raise ScenarioCatalogError("The dashboard test collection is invalid")
    try:
        paths = discover_scenario_paths(
            [scenario_root],
            settings,
            allow_empty=True,
        )
    except (OSError, ScenarioLoadError) as exc:
        raise ScenarioCatalogError(
            "The dashboard test collection could not be loaded safely"
        ) from exc
    return reporting, tuple(paths)


def _activity_registry() -> ActivityRegistry:
    registry = ActivityRegistry()
    try:
        register_framework_activities(registry)
    except ActivityRegistrationError as exc:
        raise ScenarioCatalogError("The dashboard activity catalog is unavailable") from exc
    return registry


def _scenario_validator(reporting: DashboardReportingRuntime) -> ScenarioRunner:
    try:
        return ScenarioRunner(
            reporting.settings,
            _activity_registry(),
            reporting_environ=reporting.environment,
        )
    except PlantainError as exc:
        raise ScenarioCatalogError("The dashboard scenario validator is unavailable") from exc


def _catalog_selection(
    reporting: DashboardReportingRuntime,
    paths: tuple[Path, ...],
    catalog_filter: ScenarioCatalogFilter,
) -> ScenarioCatalogSelection:
    settings = reporting.settings
    validator = _scenario_validator(reporting)
    redaction = RedactionPolicy(settings.sensitive_key_names)
    records = tuple(_scenario_record(path, settings, validator, redaction) for path in paths)
    items = tuple(record.item for record in records if _record_matches(record, catalog_filter))
    ready_count = sum(item.status == "ready" for item in items)
    return ScenarioCatalogSelection(
        items=items,
        ready_count=ready_count,
        review_count=len(items) - ready_count,
    )


def _record_matches(
    record: _ScenarioCatalogRecord,
    catalog_filter: ScenarioCatalogFilter,
) -> bool:
    definition = record.definition
    if catalog_filter.tag_filter.active and (
        definition is None or not catalog_filter.tag_filter.matches(definition)
    ):
        return False
    if not catalog_filter.query:
        return True
    item = record.item
    candidate = " ".join((item.name, item.source, *item.domains, *item.tags))
    return _scenario_search_score(candidate, catalog_filter.query) > 0


def _scenario_summary(
    path: Path,
    settings: Settings,
    validator: ScenarioRunner,
    redaction: RedactionPolicy,
) -> ScenarioCatalogItem:
    return _scenario_record(path, settings, validator, redaction).item


def _scenario_record(
    path: Path,
    settings: Settings,
    validator: ScenarioRunner,
    redaction: RedactionPolicy,
) -> _ScenarioCatalogRecord:
    relative = _relative_source(path, settings)
    scenario_id = scenario_id_for_source(relative)
    source = _safe_label(relative, redaction, fallback="Unnamed test file")
    try:
        definition = load_scenario(path, settings)
        validator.validate_scenario(definition)
    except PlantainError:
        item = ScenarioCatalogItem(
            scenario_id=scenario_id,
            name=_safe_label(path.stem, redaction, fallback="Unnamed test"),
            source=source,
            status="needs_review",
            step_count=0,
            domains=(),
            tags=(),
            additional_tag_count=0,
            issue="Scenario YAML or activity parameters need review.",
        )
        return _ScenarioCatalogRecord(item=item, definition=None)
    visible_tags = tuple(
        _safe_label(tag, redaction, fallback="Tag") for tag in definition.tags[:MAX_DISPLAY_TAGS]
    )
    item = ScenarioCatalogItem(
        scenario_id=scenario_id,
        name=_safe_label(definition.scenario, redaction, fallback="Unnamed test"),
        source=source,
        status="ready",
        step_count=len(definition.steps),
        domains=_domain_labels(definition),
        tags=visible_tags,
        additional_tag_count=max(0, len(definition.tags) - len(visible_tags)),
        issue="",
    )
    return _ScenarioCatalogRecord(item=item, definition=definition)


def _domain_labels(definition: ScenarioDefinition) -> tuple[str, ...]:
    found = {_ACTIVITY_DOMAINS.get(step.activity, "Framework") for step in definition.steps}
    return tuple(domain for domain in _DOMAIN_ORDER if domain in found)


def _relative_source(path: Path, settings: Settings) -> str:
    try:
        return path.relative_to(settings.scenarios_dir.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise ScenarioCatalogError(
            "A discovered test is outside the configured collection"
        ) from exc


def scenario_id_for_source(relative: str) -> str:
    """Derive the catalog identifier for one canonical relative scenario source."""

    return hashlib.sha256(relative.encode()).hexdigest()


def _normalized_catalog_query(value: object) -> str:
    if not isinstance(value, str):
        raise ScenarioCatalogError("The scenario search query must be text")
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    return _normalized_search_query(normalized)


def _normalized_search_query(value: object) -> str:
    if not isinstance(value, str):
        raise ScenarioCatalogError("The scenario search query must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > MAX_SCENARIO_SEARCH_QUERY_LENGTH:
        raise ScenarioCatalogError("The scenario search query is invalid")
    if normalized != "*" and not _SEARCH_TOKEN.search(normalized.casefold()):
        raise ScenarioCatalogError("The scenario search query is invalid")
    return normalized


def _scenario_search_score(relative: str, query: str) -> int:
    query_terms = _SEARCH_TOKEN.findall(query.casefold())
    candidate_terms = _SEARCH_TOKEN.findall(relative.casefold())
    candidate_set = set(candidate_terms)
    overlap = sum(term in candidate_set for term in query_terms)
    if overlap == 0:
        return 0
    score = overlap * 10
    if set(query_terms).issubset(candidate_set):
        score += 25
    if "".join(query_terms) in "".join(candidate_terms):
        score += 100
    return score


def _safe_label(
    value: str,
    redaction: RedactionPolicy,
    *,
    fallback: str,
) -> str:
    rendered = " ".join(redaction.redact_text(value).split())
    if not rendered:
        return fallback
    if len(rendered) <= MAX_CATALOG_LABEL_LENGTH:
        return rendered
    return f"{rendered[: MAX_CATALOG_LABEL_LENGTH - 1]}…"


def _catalog_notice(
    *,
    total_count: int,
    start: int,
    visible_count: int,
    page_size: int,
    invalid_count: int,
) -> str:
    messages: list[str] = []
    if total_count > page_size:
        messages.append(f"Showing {start + 1}-{start + visible_count} of {total_count} tests.")
    if invalid_count:
        messages.append(
            f"{invalid_count} "
            f"{'test needs' if invalid_count == 1 else 'tests need'} review on this page."
        )
    return " ".join(messages)


__all__ = [
    "DEFAULT_SCENARIO_PAGE_SIZE",
    "MAX_SCENARIO_PAGE_SIZE",
    "MAX_SCENARIO_SEARCH_MATCHES",
    "MAX_SCENARIO_SEARCH_QUERY_LENGTH",
    "ScenarioCatalogError",
    "ScenarioCatalogFilter",
    "ScenarioCatalogItem",
    "ScenarioCatalogPage",
    "ScenarioCatalogSearch",
    "ScenarioCatalogSelection",
    "is_valid_scenario_id",
    "load_scenario_catalog",
    "load_scenario_selection",
    "resolve_scenario_path",
    "scenario_id_for_source",
    "search_scenario_catalog",
]
