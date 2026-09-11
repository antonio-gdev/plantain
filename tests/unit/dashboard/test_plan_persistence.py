"""Create-only persistence coverage for reviewed critical-decision plans."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from plantain.dashboard.agent.models import (
    DecisionDimensionKind,
    DecisionEvidenceKind,
    DecisionEvidenceSource,
    DecisionPlanDimension,
    DecisionPlanDraft,
    DecisionPlanTest,
    DecisionPriority,
)
from plantain.dashboard.plan_persistence import (
    PLAN_DOCUMENT_SCHEMA_VERSION,
    DashboardPlanSaveError,
    save_decision_plan,
)
from plantain.dashboard.plan_store import (
    DashboardPlanError,
    discard_decision_plan,
    load_decision_plan,
    store_decision_plan,
)
from plantain.errors import AtomicPersistenceError

SNAPSHOT_EVIDENCE_ID = f"snapshot-{'d' * 20}"


def _draft() -> DecisionPlanDraft:
    return DecisionPlanDraft(
        feature="Checkout",
        sources=[
            DecisionEvidenceSource(
                evidence_id=SNAPSHOT_EVIDENCE_ID,
                kind=DecisionEvidenceKind.VERIFIED_UI,
                label="Checkout",
                reference="checkout.semantic.json",
                truncated=True,
            )
        ],
        assumptions=["The verified state represents the current application."],
        gaps=["Payment-provider behavior was not supplied."],
        dimensions=[
            DecisionPlanDimension(
                kind=kind,
                summary=f"Coverage for {kind.value}.",
                evidence_ids=[SNAPSHOT_EVIDENCE_ID],
            )
            for kind in DecisionDimensionKind
        ],
        tests=[
            DecisionPlanTest(
                case_id="checkout-path",
                title="Complete checkout",
                objective="Verify the observed checkout path.",
                priority=DecisionPriority.CRITICAL,
                dimensions=[
                    DecisionDimensionKind.CRITICAL_PATH,
                    DecisionDimensionKind.STATE_TRANSITION,
                ],
                preconditions=["An item is available."],
                actions=["Follow the observed checkout flow."],
                expected_results=["Checkout completes."],
                evidence_ids=[SNAPSHOT_EVIDENCE_ID],
            )
        ],
    )


def test_save_writes_versioned_grouped_plan_and_discards_memory(
    tmp_path: Path,
) -> None:
    plan_id = store_decision_plan(_draft())

    saved = save_decision_plan(tmp_path, plan_id)

    target = tmp_path / saved.relative_path
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert saved.relative_path == "test-plans/checkout.yaml"
    assert saved.feature == "Checkout"
    assert saved.test_count == 1
    assert saved.source_count == 1
    assert document["schemaVersion"] == PLAN_DOCUMENT_SCHEMA_VERSION
    assert document["feature"] == "Checkout"
    assert document["sources"]["snapshots"] == [
        {
            "evidenceId": SNAPSHOT_EVIDENCE_ID,
            "label": "Checkout",
            "reference": "checkout.semantic.json",
            "partial": True,
        }
    ]
    assert document["sources"]["requirements"] == []
    assert set(document["dimensions"]) == {
        "critical_paths",
        "pairwise",
        "boundaries",
        "state_transitions",
        "failure_resilience",
        "accessibility",
    }
    assert document["tests"][0]["evidence"] == [SNAPSHOT_EVIDENCE_ID]
    with pytest.raises(DashboardPlanError):
        load_decision_plan(plan_id)


def test_save_collision_preserves_existing_plan_and_uses_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "test-plans"
    destination.mkdir()
    existing = destination / "checkout.yaml"
    existing.write_text("original", encoding="utf-8")
    monkeypatch.setattr(
        "plantain.dashboard.plan_persistence.secrets.token_hex",
        lambda _count: "cafebabe",
    )
    plan_id = store_decision_plan(_draft())

    saved = save_decision_plan(tmp_path, plan_id)

    assert existing.read_text(encoding="utf-8") == "original"
    assert saved.relative_path == "test-plans/checkout-cafebabe.yaml"


def test_persistence_failure_is_value_free_and_retains_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_id = store_decision_plan(_draft())

    def reject_write(_path: Path, _payload: bytes) -> None:
        raise AtomicPersistenceError("synthetic private plan path")

    monkeypatch.setattr(
        "plantain.dashboard.plan_persistence.write_source_bytes_atomic_new",
        reject_write,
    )
    with pytest.raises(
        DashboardPlanSaveError,
        match="could not be saved safely",
    ) as captured:
        save_decision_plan(tmp_path, plan_id)

    assert "synthetic private plan path" not in str(captured.value)
    assert load_decision_plan(plan_id).draft.feature == "Checkout"
    discard_decision_plan(plan_id)


def test_invalid_or_expired_plan_identifier_is_value_free(tmp_path: Path) -> None:
    with pytest.raises(DashboardPlanSaveError, match="expired") as captured:
        save_decision_plan(tmp_path, "../outside")

    assert "../outside" not in str(captured.value)
