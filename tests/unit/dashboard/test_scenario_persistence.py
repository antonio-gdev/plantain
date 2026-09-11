"""Collision-safe persistence for reviewed dashboard scenario drafts."""

from __future__ import annotations

from pathlib import Path

import pytest

from plantain.dashboard.agent.models import AgentCapability, ScenarioDraft
from plantain.dashboard.draft_store import (
    DashboardDraftError,
    discard_scenario_draft,
    load_scenario_draft,
    store_scenario_draft,
)
from plantain.dashboard.scenario_catalog import resolve_scenario_path
from plantain.dashboard.scenario_persistence import (
    DashboardScenarioSaveError,
    save_scenario_draft,
)
from plantain.errors import AtomicPersistenceError

VALID_SCENARIO = """\
scenario: Generated health check
steps:
  - sendRequest:
      id: health
      endpoint: https://api.example.test/health
      method: GET
      expectedStatus: 200
"""


def _draft(
    source: str = VALID_SCENARIO,
    *,
    activities: list[str] | None = None,
    directory: str | None = None,
) -> ScenarioDraft:
    return ScenarioDraft(
        scenario="Generated health check",
        yaml_text=source,
        step_count=1,
        activities=activities or ["sendRequest"],
        suggested_directory=directory,
    )


def test_save_uses_custom_folder_and_catalog_identity(tmp_path: Path) -> None:
    draft_id = store_scenario_draft(
        _draft(directory="agent-suggestion"),
        AgentCapability.API_CONTRACT,
    )

    saved = save_scenario_draft(
        tmp_path,
        draft_id,
        directory="Payments Team / Regression Cases",
    )

    target = tmp_path / "scenarios" / saved.relative_path
    assert saved.relative_path == ("payments-team/regression-cases/generated-health-check.yaml")
    assert target.read_text(encoding="utf-8") == VALID_SCENARIO
    assert resolve_scenario_path(tmp_path, saved.scenario_id) == target.resolve()
    assert saved.activities == ("sendRequest",)
    with pytest.raises(DashboardDraftError):
        load_scenario_draft(draft_id)


def test_save_uses_suggestion_then_default_when_folder_is_omitted(
    tmp_path: Path,
) -> None:
    suggested_id = store_scenario_draft(
        _draft(directory="team-api"),
        AgentCapability.API_CONTRACT,
    )
    default_id = store_scenario_draft(
        _draft(),
        AgentCapability.API_CONTRACT,
    )

    suggested = save_scenario_draft(tmp_path, suggested_id)
    default = save_scenario_draft(tmp_path, default_id)

    assert suggested.relative_path.startswith("team-api/")
    assert default.relative_path.startswith("generated/api/")


def test_save_collision_uses_atomic_random_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "scenarios" / "api"
    destination.mkdir(parents=True)
    existing = destination / "generated-health-check.yaml"
    existing.write_text("original", encoding="utf-8")
    monkeypatch.setattr(
        "plantain.dashboard.scenario_persistence.secrets.token_hex",
        lambda _count: "cafebabe",
    )
    draft_id = store_scenario_draft(
        _draft(),
        AgentCapability.API_CONTRACT,
    )

    saved = save_scenario_draft(tmp_path, draft_id, directory="api")

    assert existing.read_text(encoding="utf-8") == "original"
    assert saved.relative_path == "api/generated-health-check-cafebabe.yaml"


def test_unsafe_folder_is_rejected_and_draft_is_retained(tmp_path: Path) -> None:
    draft_id = store_scenario_draft(
        _draft(),
        AgentCapability.API_CONTRACT,
    )

    with pytest.raises(
        DashboardScenarioSaveError,
        match="inside the scenarios collection",
    ):
        save_scenario_draft(tmp_path, draft_id, directory="../outside")

    assert load_scenario_draft(draft_id).draft.scenario == "Generated health check"
    assert not (tmp_path / "outside").exists()
    discard_scenario_draft(draft_id)


def test_save_revalidates_activity_contracts(tmp_path: Path) -> None:
    source = "scenario: Generated health check\nsteps:\n  - unknownActivity: {}\n"
    draft_id = store_scenario_draft(
        _draft(source, activities=["unknownActivity"]),
        AgentCapability.API_CONTRACT,
    )

    with pytest.raises(
        DashboardScenarioSaveError,
        match="no longer passes local validation",
    ):
        save_scenario_draft(tmp_path, draft_id)

    discard_scenario_draft(draft_id)


def test_persistence_failure_is_value_free_and_retains_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = store_scenario_draft(
        _draft(),
        AgentCapability.API_CONTRACT,
    )

    def reject_write(_path: Path, _payload: bytes) -> None:
        raise AtomicPersistenceError("synthetic private path")

    monkeypatch.setattr(
        "plantain.dashboard.scenario_persistence.write_source_bytes_atomic_new",
        reject_write,
    )
    with pytest.raises(
        DashboardScenarioSaveError,
        match="could not be saved safely",
    ) as captured:
        save_scenario_draft(tmp_path, draft_id)

    assert "synthetic private path" not in str(captured.value)
    assert load_scenario_draft(draft_id).draft.scenario == "Generated health check"
    discard_scenario_draft(draft_id)
