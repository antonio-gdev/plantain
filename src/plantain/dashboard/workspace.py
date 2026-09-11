"""Bounded read-only projections of a local Plantain workspace."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plantain.config import Settings
from plantain.dashboard.agent_usage import (
    AgentUsageOverview,
    DashboardAgentUsageError,
    load_agent_usage_overview,
    unavailable_agent_usage_overview,
)
from plantain.engine.loader import discover_scenario_paths
from plantain.errors import AtomicPersistenceError
from plantain.persistence import open_binary_read_no_follow
from plantain.security.redaction import RedactionPolicy

MAX_DASHBOARD_REPORT_BYTES = 4_194_304
MAX_INDEXED_FILES = 10_000
MAX_RECENT_RUNS = 6
MAX_DISPLAY_TEXT = 160
MILLISECONDS_PER_SECOND = 1_000
KNOWN_RUN_STATUSES = frozenset({"passed", "failed", "cancelled"})


class DashboardWorkspaceError(RuntimeError):
    """Raised when bounded workspace metadata cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A deliberately narrow browser-safe projection of one native report."""

    scenario: str
    status: str
    duration: str
    completed: str


@dataclass(frozen=True, slots=True)
class RunAnalytics:
    """Honest bounded quality metrics derived from valid native results."""

    analyzed_run_count: int
    passed_count: int
    failed_count: int
    cancelled_count: int
    pass_rate_percent: int
    average_duration_ms: int | None
    mixed_outcome_scenario_count: int


@dataclass(frozen=True, slots=True)
class _ParsedRunSummary:
    summary: RunSummary
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class _BoundedFileCollection:
    paths: tuple[Path, ...]
    limited: bool


@dataclass(frozen=True, slots=True)
class WorkspaceOverview:
    """Bounded metadata required by the dashboard overview."""

    scenario_count: int
    run_count: int
    run_count_limited: bool
    evidence_count: int
    evidence_count_limited: bool
    run_analytics: RunAnalytics
    agent_usage: AgentUsageOverview
    recent_runs: tuple[RunSummary, ...]
    notice: str


def load_workspace_overview(project_root: Path) -> WorkspaceOverview:
    """Load bounded workspace counts and recent sanitized report metadata."""

    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise DashboardWorkspaceError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise DashboardWorkspaceError("The dashboard workspace is unavailable")

    try:
        settings = Settings.from_env(root)
        scenario_count = _scenario_count(root, settings)
        report_files = _bounded_files(
            settings.output_dir / "results",
            suffix=".result.json",
        )
        evidence_files = _bounded_files(
            settings.snapshots_dir,
            suffix=".semantic.json",
        )
        redaction = RedactionPolicy(settings.sensitive_key_names)
        summaries, analytics, invalid_count = _recent_runs(
            report_files.paths,
            redaction,
        )
    except (AtomicPersistenceError, OSError) as exc:
        raise DashboardWorkspaceError("Workspace metadata could not be read safely") from exc
    try:
        usage = load_agent_usage_overview(
            settings.output_dir,
            sensitive_keys=settings.sensitive_key_names,
        )
    except DashboardAgentUsageError:
        usage = unavailable_agent_usage_overview()

    notice = _overview_notice(
        invalid_count,
        run_limited=report_files.limited,
        evidence_limited=evidence_files.limited,
    )
    return WorkspaceOverview(
        scenario_count=scenario_count,
        run_count=len(report_files.paths),
        run_count_limited=report_files.limited,
        evidence_count=len(evidence_files.paths),
        evidence_count_limited=evidence_files.limited,
        run_analytics=analytics,
        agent_usage=usage,
        recent_runs=summaries,
        notice=notice,
    )


def _scenario_count(root: Path, settings: Settings) -> int:
    scenario_root = root / "scenarios"
    if not scenario_root.is_dir():
        return 0
    return len(discover_scenario_paths([scenario_root], settings, allow_empty=True))


def _overview_notice(
    invalid_count: int,
    *,
    run_limited: bool,
    evidence_limited: bool,
) -> str:
    notices: list[str] = []
    if run_limited or evidence_limited:
        scope = (
            "Run and evidence"
            if run_limited and evidence_limited
            else ("Run" if run_limited else "Evidence")
        )
        notices.append(
            f"{scope} overview scanning reached its local display budget; "
            "counts marked + are lower bounds. No files were changed."
        )
    if invalid_count:
        notices.append(
            f"{invalid_count} invalid or incompatible run "
            f"{'report was' if invalid_count == 1 else 'reports were'} excluded."
        )
    return " ".join(notices)


def _bounded_files(directory: Path, *, suffix: str) -> _BoundedFileCollection:
    if not directory.exists():
        return _BoundedFileCollection(paths=(), limited=False)
    if directory.is_symlink() or not directory.is_dir():
        raise DashboardWorkspaceError("A workspace collection has an invalid location")

    pending = [directory]
    found: list[Path] = []
    inspected = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanner:
                entries = sorted(scanner, key=lambda entry: entry.name)
        except OSError as exc:
            raise DashboardWorkspaceError(
                "A workspace collection could not be enumerated safely"
            ) from exc
        for entry in entries:
            if inspected >= MAX_INDEXED_FILES:
                return _BoundedFileCollection(
                    paths=tuple(found),
                    limited=True,
                )
            inspected += 1
            if entry.is_symlink():
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(suffix):
                found.append(path)
    return _BoundedFileCollection(paths=tuple(found), limited=False)


def _recent_runs(
    paths: tuple[Path, ...],
    redaction: RedactionPolicy,
) -> tuple[tuple[RunSummary, ...], RunAnalytics, int]:
    ordered = sorted(paths, key=_modified_time, reverse=True)
    summaries: list[RunSummary] = []
    parsed: list[_ParsedRunSummary] = []
    invalid_count = 0
    for path in ordered:
        result = _read_report_summary(path, redaction)
        if result is None:
            invalid_count += 1
            continue
        parsed.append(result)
        if len(summaries) < MAX_RECENT_RUNS:
            summaries.append(result.summary)
    return tuple(summaries), _run_analytics(parsed), invalid_count


def _run_analytics(values: list[_ParsedRunSummary]) -> RunAnalytics:
    passed_count = sum(item.summary.status == "passed" for item in values)
    failed_count = sum(item.summary.status == "failed" for item in values)
    cancelled_count = sum(item.summary.status == "cancelled" for item in values)
    durations = [item.duration_ms for item in values if item.duration_ms is not None]
    outcomes: dict[str, set[str]] = {}
    for item in values:
        outcomes.setdefault(item.summary.scenario, set()).add(item.summary.status)
    mixed_count = sum({"passed", "failed"}.issubset(statuses) for statuses in outcomes.values())
    analyzed_count = len(values)
    return RunAnalytics(
        analyzed_run_count=analyzed_count,
        passed_count=passed_count,
        failed_count=failed_count,
        cancelled_count=cancelled_count,
        pass_rate_percent=(round(passed_count * 100 / analyzed_count) if analyzed_count else 0),
        average_duration_ms=(round(sum(durations) / len(durations)) if durations else None),
        mixed_outcome_scenario_count=mixed_count,
    )


def _modified_time(path: Path) -> int:
    try:
        return path.stat(follow_symlinks=False).st_mtime_ns
    except OSError:
        return 0


def _read_report_summary(
    path: Path,
    redaction: RedactionPolicy,
) -> _ParsedRunSummary | None:
    try:
        with open_binary_read_no_follow(path, private=True) as stream:
            raw = stream.read(MAX_DASHBOARD_REPORT_BYTES + 1)
        if len(raw) > MAX_DASHBOARD_REPORT_BYTES:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (
        AtomicPersistenceError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        return None
    if not isinstance(value, dict):
        return None

    status = value.get("status")
    if not isinstance(status, str) or status.lower() not in KNOWN_RUN_STATUSES:
        return None
    scenario = _scenario_name(value, redaction)
    if scenario is None:
        return None
    duration_ms = _duration_value(value.get("duration_ms"))
    return _ParsedRunSummary(
        summary=RunSummary(
            scenario=scenario,
            status=status.lower(),
            duration=_duration_label(duration_ms),
            completed=_completed_label(value, path, redaction),
        ),
        duration_ms=duration_ms,
    )


def _scenario_name(
    value: dict[str, Any],
    redaction: RedactionPolicy,
) -> str | None:
    candidate = value.get("scenario")
    if isinstance(candidate, dict):
        candidate = candidate.get("name")
    if not isinstance(candidate, str):
        candidate = value.get("name")
    if not isinstance(candidate, str):
        return None
    return _display_text(candidate, redaction)


def _display_text(value: str, redaction: RedactionPolicy) -> str:
    rendered = " ".join(redaction.redact_text(value).split())
    if not rendered:
        return "Unnamed scenario"
    if len(rendered) <= MAX_DISPLAY_TEXT:
        return rendered
    return f"{rendered[: MAX_DISPLAY_TEXT - 1]}…"


def _duration_value(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _duration_label(value: Any) -> str:
    duration_ms = _duration_value(value)
    if duration_ms is None:
        return "—"
    if duration_ms < MILLISECONDS_PER_SECOND:
        return f"{duration_ms} ms"
    return f"{duration_ms / MILLISECONDS_PER_SECOND:.2f} s"


def _completed_label(
    value: dict[str, Any],
    path: Path,
    redaction: RedactionPolicy,
) -> str:
    for key in ("finished_at", "completed_at", "ended_at"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return _display_text(candidate, redaction)
    try:
        modified = path.stat(follow_symlinks=False).st_mtime
    except OSError:
        return "—"
    return datetime.fromtimestamp(modified, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


__all__ = [
    "DashboardWorkspaceError",
    "RunAnalytics",
    "RunSummary",
    "WorkspaceOverview",
    "load_workspace_overview",
]
