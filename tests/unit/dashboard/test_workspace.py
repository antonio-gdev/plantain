"""Isolated coverage for bounded dashboard workspace projections."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plantain.dashboard import state, workspace
from plantain.persistence import write_bytes_atomic, write_json_atomic

EXPECTED_DURATION_MS = 1_250
EXPECTED_REPORT_COUNT = 2
EXPECTED_ANALYZED_RUNS = 3
EXPECTED_PASS_RATE = 33
EXPECTED_AVERAGE_DURATION_MS = 200
MAX_TEST_INDEX_ENTRIES = 1
MAX_TEST_REPORT_BYTES = 256
PRIVATE_REPORT_VALUE = "private-report-value"


def _configure_workspace(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> SimpleNamespace:
    scenarios = root / "scenarios"
    scenarios.mkdir()
    scenario = scenarios / "checkout.yaml"
    scenario.write_text("name: Checkout\nsteps: []\n", encoding="utf-8")
    settings = SimpleNamespace(
        output_dir=root / "output",
        snapshots_dir=root / "snapshots",
        sensitive_key_names=(),
    )
    monkeypatch.setattr(
        workspace.Settings,
        "from_env",
        lambda _root: settings,
    )
    monkeypatch.setattr(
        workspace,
        "discover_scenario_paths",
        lambda *_args, **_kwargs: [scenario],
    )
    return settings


def test_overview_projects_only_bounded_run_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_workspace(monkeypatch, tmp_path)
    report = settings.output_dir / f"results/ui/checkout/{'a' * 32}.result.json"
    evidence = settings.snapshots_dir / "checkout.semantic.json"
    write_json_atomic(
        report,
        {
            "scenario": {"name": "Checkout"},
            "status": "passed",
            "duration_ms": EXPECTED_DURATION_MS,
            "finished_at": "2026-09-03T12:00:00+00:00",
            "error": "must never enter the projection",
        },
    )
    write_json_atomic(evidence, {"schemaVersion": "3.0"})

    overview = workspace.load_workspace_overview(tmp_path)

    assert overview.scenario_count == 1
    assert overview.run_count == 1
    assert overview.run_count_limited is False
    assert overview.evidence_count == 1
    assert overview.evidence_count_limited is False
    assert overview.run_analytics == workspace.RunAnalytics(
        analyzed_run_count=1,
        passed_count=1,
        failed_count=0,
        cancelled_count=0,
        pass_rate_percent=100,
        average_duration_ms=EXPECTED_DURATION_MS,
        mixed_outcome_scenario_count=0,
    )
    assert overview.agent_usage.total_calls == 0
    assert overview.notice == ""
    assert overview.recent_runs == (
        workspace.RunSummary(
            scenario="Checkout",
            status="passed",
            duration="1.25 s",
            completed="2026-09-03T12:00:00+00:00",
        ),
    )
    assert not hasattr(overview.recent_runs[0], "error")


def test_invalid_and_oversized_reports_are_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_workspace(monkeypatch, tmp_path)
    reports = settings.output_dir / "results"
    write_json_atomic(
        reports / "valid.result.json",
        {
            "scenario": "Inventory",
            "status": "failed",
            "duration_ms": 5,
        },
    )
    monkeypatch.setattr(
        workspace,
        "MAX_DASHBOARD_REPORT_BYTES",
        MAX_TEST_REPORT_BYTES,
    )
    write_bytes_atomic(
        reports / "oversized.result.json",
        b"x" * (MAX_TEST_REPORT_BYTES + 1),
    )

    overview = workspace.load_workspace_overview(tmp_path)

    assert overview.run_count == EXPECTED_REPORT_COUNT
    assert len(overview.recent_runs) == 1
    assert overview.recent_runs[0].scenario == "Inventory"
    assert overview.notice == "1 invalid or incompatible run report was excluded."


def test_overview_computes_honest_quality_metrics_in_one_bounded_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_workspace(monkeypatch, tmp_path)
    reports = settings.output_dir / "results"
    values = (
        ("Checkout", "passed", 100),
        ("Checkout", "failed", 300),
        ("Inventory", "cancelled", None),
    )
    for index, (scenario, status, duration_ms) in enumerate(values):
        write_json_atomic(
            reports / f"{index}.result.json",
            {
                "scenario": scenario,
                "status": status,
                "duration_ms": duration_ms,
            },
        )

    analytics = workspace.load_workspace_overview(tmp_path).run_analytics

    assert analytics.analyzed_run_count == EXPECTED_ANALYZED_RUNS
    assert analytics.passed_count == 1
    assert analytics.failed_count == 1
    assert analytics.cancelled_count == 1
    assert analytics.pass_rate_percent == EXPECTED_PASS_RATE
    assert analytics.average_duration_ms == EXPECTED_AVERAGE_DURATION_MS
    assert analytics.mixed_outcome_scenario_count == 1


def test_overview_redacts_modified_report_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_workspace(monkeypatch, tmp_path)
    write_json_atomic(
        settings.output_dir / "results/redacted.result.json",
        {
            "scenario": f"token={PRIVATE_REPORT_VALUE}",
            "status": "passed",
            "finished_at": f"token={PRIVATE_REPORT_VALUE}",
        },
    )

    run = workspace.load_workspace_overview(tmp_path).recent_runs[0]

    assert PRIVATE_REPORT_VALUE not in run.scenario
    assert PRIVATE_REPORT_VALUE not in run.completed


def test_optional_usage_failure_does_not_hide_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_workspace(monkeypatch, tmp_path)

    def reject(*_args: object, **_kwargs: object) -> None:
        raise workspace.DashboardAgentUsageError("synthetic private detail")

    monkeypatch.setattr(workspace, "load_agent_usage_overview", reject)

    overview = workspace.load_workspace_overview(tmp_path)

    assert overview.scenario_count == 1
    assert overview.agent_usage.cost_boundary == "Unavailable"
    assert overview.agent_usage.notice == "Agent usage history is unavailable."


def test_workspace_enumeration_skips_symlinks(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "results"
    collection.mkdir()
    outside = tmp_path / "outside.result.json"
    outside.write_text("{}", encoding="utf-8")
    (collection / "linked.result.json").symlink_to(outside)

    result = workspace._bounded_files(collection, suffix=".result.json")

    assert result.paths == ()
    assert result.limited is False


def test_workspace_enumeration_returns_partial_result_at_entry_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = tmp_path / "results"
    collection.mkdir()
    (collection / "one.result.json").write_text("{}", encoding="utf-8")
    (collection / "two.result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        workspace,
        "MAX_INDEXED_FILES",
        MAX_TEST_INDEX_ENTRIES,
    )

    result = workspace._bounded_files(collection, suffix=".result.json")

    assert result.paths == (collection / "one.result.json",)
    assert result.limited is True


def test_overview_discloses_partial_counts_without_becoming_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_workspace(monkeypatch, tmp_path)
    reports = settings.output_dir / "results"
    for name in ("one", "two"):
        write_json_atomic(
            reports / f"{name}.result.json",
            {"scenario": name, "status": "passed"},
        )
    monkeypatch.setattr(workspace, "MAX_INDEXED_FILES", MAX_TEST_INDEX_ENTRIES)

    overview = workspace.load_workspace_overview(tmp_path)

    assert overview.run_count == MAX_TEST_INDEX_ENTRIES
    assert overview.run_count_limited is True
    assert overview.evidence_count_limited is False
    assert "counts marked + are lower bounds" in overview.notice


def test_dashboard_project_root_uses_only_explicit_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(state.PROJECT_ROOT_ENV, raising=False)
    assert state.dashboard_project_root() == tmp_path

    configured = tmp_path / "configured"
    monkeypatch.setenv(state.PROJECT_ROOT_ENV, str(configured))
    assert state.dashboard_project_root() == configured


def test_development_and_packaged_assets_are_identical() -> None:
    root = Path(__file__).parents[3]
    for name in ("plantain_favicon.png", "plantain_logo.png"):
        assert (root / "assets" / name).read_bytes() == (
            root / "src/plantain/dashboard/assets" / name
        ).read_bytes()
