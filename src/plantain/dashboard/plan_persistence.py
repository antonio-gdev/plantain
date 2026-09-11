"""Validated create-only persistence for dashboard critical-decision plans."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from plantain.dashboard.agent.models import (
    DecisionDimensionKind,
    DecisionEvidenceKind,
    DecisionPlanDraft,
)
from plantain.dashboard.plan_store import (
    DashboardPlanError,
    DecisionPlanRecord,
    discard_decision_plan,
    load_decision_plan,
)
from plantain.dashboard.scenario_paths import scenario_filename_stem
from plantain.errors import (
    AtomicCommitUncertainError,
    AtomicPersistenceError,
    AtomicTargetExistsError,
    PlantainError,
)
from plantain.persistence import write_source_bytes_atomic_new

PLAN_DOCUMENT_SCHEMA_VERSION = "1.0"
PLAN_SAVE_COLLISION_ATTEMPTS = 8
PLAN_SAVE_SUFFIX_BYTES = 4
_SOURCE_GROUPS = {
    DecisionEvidenceKind.VERIFIED_UI: "snapshots",
    DecisionEvidenceKind.REQUIREMENTS: "requirements",
    DecisionEvidenceKind.APPLICATION_SOURCE: "application",
    DecisionEvidenceKind.API_CONTRACT: "openapi",
    DecisionEvidenceKind.DATABASE_DISCOVERY: "database",
}
_DIMENSION_KEYS = {
    DecisionDimensionKind.CRITICAL_PATH: "critical_paths",
    DecisionDimensionKind.PAIRWISE: "pairwise",
    DecisionDimensionKind.BOUNDARY: "boundaries",
    DecisionDimensionKind.STATE_TRANSITION: "state_transitions",
    DecisionDimensionKind.FAILURE_RESILIENCE: "failure_resilience",
    DecisionDimensionKind.ACCESSIBILITY: "accessibility",
}


class DashboardPlanSaveError(PlantainError):
    """Raised when a reviewed decision plan cannot be saved safely."""


@dataclass(frozen=True, slots=True)
class SavedDecisionPlan:
    """Browser-safe identity for one newly persisted decision plan."""

    feature: str
    relative_path: str
    test_count: int
    source_count: int


def save_decision_plan(
    project_root: Path,
    plan_id: str,
) -> SavedDecisionPlan:
    """Revalidate and atomically save one backend-owned decision plan."""

    root = _workspace_root(project_root)
    record = _load_plan(plan_id)
    draft = _validated_plan(record)
    payload = _canonical_plan_yaml(draft)
    return _persist_plan(root, record, draft, payload)


def _workspace_root(project_root: Path) -> Path:
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise DashboardPlanSaveError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise DashboardPlanSaveError("The dashboard workspace is unavailable")
    return root


def _load_plan(plan_id: str) -> DecisionPlanRecord:
    try:
        return load_decision_plan(plan_id)
    except DashboardPlanError as exc:
        raise DashboardPlanSaveError(
            "This decision plan expired; create it again to continue"
        ) from exc


def _validated_plan(record: DecisionPlanRecord) -> DecisionPlanDraft:
    try:
        return DecisionPlanDraft.model_validate(
            record.draft.model_dump(mode="python"),
        )
    except ValidationError as exc:
        raise DashboardPlanSaveError(
            "The reviewed decision plan no longer passes local validation"
        ) from exc


def _canonical_plan_yaml(draft: DecisionPlanDraft) -> bytes:
    document = _plan_document(draft)
    try:
        source = yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        )
        return source.encode()
    except (UnicodeError, yaml.YAMLError) as exc:
        raise DashboardPlanSaveError(
            "The reviewed decision plan could not be encoded safely"
        ) from exc


def _plan_document(draft: DecisionPlanDraft) -> dict[str, object]:
    source_groups: dict[str, list[dict[str, object]]] = {
        group: [] for group in _SOURCE_GROUPS.values()
    }
    for source in draft.sources:
        item: dict[str, object] = {
            "evidenceId": source.evidence_id,
            "label": source.label,
            "reference": source.reference,
        }
        if source.truncated:
            item["partial"] = True
        source_groups[_SOURCE_GROUPS[source.kind]].append(item)
    dimensions = {
        _DIMENSION_KEYS[item.kind]: {
            "summary": item.summary,
            "evidence": list(item.evidence_ids),
        }
        for item in draft.dimensions
    }
    tests = [
        {
            "id": item.case_id,
            "title": item.title,
            "priority": item.priority.value,
            "objective": item.objective,
            "dimensions": [kind.value for kind in item.dimensions],
            "preconditions": list(item.preconditions),
            "actions": list(item.actions),
            "expectedResults": list(item.expected_results),
            "evidence": list(item.evidence_ids),
        }
        for item in draft.tests
    ]
    return {
        "schemaVersion": PLAN_DOCUMENT_SCHEMA_VERSION,
        "feature": draft.feature,
        "sources": source_groups,
        "assumptions": list(draft.assumptions),
        "gaps": list(draft.gaps),
        "dimensions": dimensions,
        "tests": tests,
    }


def _persist_plan(
    root: Path,
    record: DecisionPlanRecord,
    draft: DecisionPlanDraft,
    payload: bytes,
) -> SavedDecisionPlan:
    stem = scenario_filename_stem(draft.feature)
    for attempt in range(PLAN_SAVE_COLLISION_ATTEMPTS):
        filename = _candidate_filename(stem, attempt)
        relative_path = (Path("test-plans") / filename).as_posix()
        target = root / relative_path
        try:
            write_source_bytes_atomic_new(target, payload)
        except AtomicTargetExistsError:
            continue
        except AtomicCommitUncertainError as exc:
            raise DashboardPlanSaveError(
                "The plan may have been saved, but durability could not be confirmed"
            ) from exc
        except AtomicPersistenceError as exc:
            raise DashboardPlanSaveError("The plan could not be saved safely") from exc
        discard_decision_plan(record.plan_id)
        return SavedDecisionPlan(
            feature=draft.feature,
            relative_path=relative_path,
            test_count=len(draft.tests),
            source_count=len(draft.sources),
        )
    raise DashboardPlanSaveError("Plantain could not allocate a unique plan filename")


def _candidate_filename(stem: str, attempt: int) -> str:
    if attempt == 0:
        return f"{stem}.yaml"
    return f"{stem}-{secrets.token_hex(PLAN_SAVE_SUFFIX_BYTES)}.yaml"


__all__ = [
    "PLAN_DOCUMENT_SCHEMA_VERSION",
    "DashboardPlanSaveError",
    "SavedDecisionPlan",
    "save_decision_plan",
]
