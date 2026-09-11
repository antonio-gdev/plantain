"""Dashboard UI continuation consumes only exact verified snapshot evidence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from plantain.activities.snapshot_registry_complete import (
    DiagnosticSnapshotExcerpt,
    VerifiedSnapshotExcerpt,
    VerifiedSnapshotSummary,
)
from plantain.dashboard import ui_evidence
from plantain.dashboard.agent.ui_models import (
    MAX_UI_DOM_EXCERPT_BYTES,
    UI_DISCOVERY_OUTPUT,
    UiRunEvidence,
)
from plantain.dashboard.ui_evidence import UiEvidenceError, load_ui_run_evidence
from plantain.models.ui import CapturePageSnapshotParams
from plantain.persistence import write_json_atomic
from plantain.security.redaction import REDACTED

RUN_ID = "a" * 32
STARTED_AT_MS = 1_756_742_400_000
SOURCE_PATH = "generated/ui/discover-login.yaml"
STEP_ID = "discover_login"
CANONICAL_FILE = "login.semantic.json"
YAML_MAX_BYTES = 100_000
YAML_MAX_DEPTH = 100
YAML_MAX_NODES = 10_000


class _Registry:
    def __init__(
        self,
        *,
        verified: VerifiedSnapshotExcerpt | None = None,
        diagnostic: DiagnosticSnapshotExcerpt | None = None,
    ) -> None:
        self.verified = verified
        self.diagnostic = diagnostic
        self.reads: list[tuple[Any, ...]] = []

    def read_verified_excerpt(
        self,
        canonical_file: str,
        *,
        max_bytes: int,
    ) -> VerifiedSnapshotExcerpt:
        self.reads.append(("verified", canonical_file, max_bytes))
        if self.verified is None:
            raise AssertionError("verified evidence was not configured")
        return self.verified

    def read_diagnostic_excerpt(
        self,
        canonical_file: str,
        activity: str,
        failure_stage: str,
        *,
        max_bytes: int,
    ) -> DiagnosticSnapshotExcerpt:
        self.reads.append(("diagnostic", canonical_file, activity, failure_stage, max_bytes))
        if self.diagnostic is None:
            raise AssertionError("diagnostic evidence was not configured")
        return self.diagnostic


def _settings(
    root: Path,
    *,
    sensitive_keys: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=root,
        scenarios_dir=root / "scenarios",
        snapshots_dir=root / "snapshots",
        output_dir=root / "output",
        allure_results_enabled=False,
        zephyr_publish_results=False,
        zephyr_base_url="",
        zephyr_attach_report=False,
        zephyr_attachment_data_governance_approved=False,
        yaml_max_bytes=YAML_MAX_BYTES,
        yaml_max_depth=YAML_MAX_DEPTH,
        yaml_max_nodes=YAML_MAX_NODES,
        sensitive_key_names=sensitive_keys,
    )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    registry: _Registry,
    *,
    sensitive_keys: tuple[str, ...] = (),
) -> SimpleNamespace:
    settings = _settings(root, sensitive_keys=sensitive_keys)
    settings.scenarios_dir.mkdir(parents=True)
    monkeypatch.setattr(ui_evidence.Settings, "from_env", lambda _root: settings)

    def registry_factory(_path: Path) -> _Registry:
        return registry

    monkeypatch.setattr(
        ui_evidence,
        "CompleteSnapshotRegistry",
        registry_factory,
    )
    return settings


def _write_scenario(
    settings: SimpleNamespace,
    *,
    activity: str | None = None,
    output: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "id": STEP_ID,
        "url": "https://example.test/login",
    }
    if activity is not None:
        params["activity"] = activity
    target = settings.scenarios_dir / SOURCE_PATH
    write_json_atomic(
        target,
        {
            "scenario": "Discover login",
            "steps": [{"capturePageSnapshot": params}],
            "outputs": {
                UI_DISCOVERY_OUTPUT: output or f"${{{STEP_ID}.snapshots[0].canonicalFile}}"
            },
        },
    )


def _report(
    *,
    status: str = "passed",
    output: Any = CANONICAL_FILE,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "scenario": "Discover login",
        "status": status,
        "duration_ms": 20,
        "started_at_ms": STARTED_AT_MS,
        "correlation_id": RUN_ID,
        "source_path": SOURCE_PATH,
        "jira_ticket": None,
        "test_case_key": None,
        "test_run_key": None,
        "tags": ["ui", "discovery"],
        "steps": [
            {
                "activity": "capturePageSnapshot",
                "step_id": STEP_ID,
                "status": status,
                "duration_ms": 10,
            }
        ],
        "failure": failure,
        "artifacts": [],
        "operations": [],
        "outputs": {UI_DISCOVERY_OUTPUT: output} if status == "passed" else {},
        "integrations": {},
    }


def _write_report(settings: SimpleNamespace, report: dict[str, Any]) -> None:
    write_json_atomic(
        settings.output_dir / "results/generated/ui/discover-login" / f"{RUN_ID}.result.json",
        report,
    )


def _verified(
    *,
    activity: str = STEP_ID,
    content: str = '- textbox "Username"\n- button "Login"\n',
) -> VerifiedSnapshotExcerpt:
    summary = VerifiedSnapshotSummary(
        canonical_file=CANONICAL_FILE,
        url="https://example.test/login?token=private-query",
        page_title="Sign in",
        activities=(activity,),
        normalized_key="login",
        structural_digest="b" * 64,
        element_counts=(("button", 1), ("textbox", 1)),
        key_ids=("login",),
        last_updated="2026-09-09T00:00:00Z",
    )
    return VerifiedSnapshotExcerpt(summary=summary, content=content)


def _diagnostic(
    *,
    activity: str = STEP_ID,
    failure_stage: str = "action",
) -> DiagnosticSnapshotExcerpt:
    return DiagnosticSnapshotExcerpt(
        canonical_file=CANONICAL_FILE,
        activity=activity,
        failure_stage=failure_stage,  # type: ignore[arg-type]
        url="https://example.test/login",
        page_title="Sign in",
        content='- textbox "Username"\n- button "Continue"\n',
    )


def _failure(
    *,
    report_stage: str = "ui_action",
    snapshot: str = f"snapshots/{CANONICAL_FILE}",
) -> dict[str, Any]:
    return {
        "activity": "capturePageSnapshot",
        "step_id": STEP_ID,
        "message": "UI action failed",
        "exception_type": "UiActionError",
        "details": {
            "failure_stage": report_stage,
            "operation_index": 1,
            "operation_total": 1,
            "operation_type": "click",
            "operation_target": "role:button[name=Login]",
            "operation_error_type": "TimeoutError",
            "diagnostic_snapshot": snapshot,
            "evidence_state": "diagnostic",
        },
    }


def test_passed_run_loads_declared_verified_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _Registry(verified=_verified())
    settings = _configure(monkeypatch, tmp_path, registry)
    _write_scenario(settings)
    _write_report(settings, _report())

    evidence = load_ui_run_evidence(tmp_path, RUN_ID)

    assert evidence.evidence_state == "verified"
    assert evidence.failure is None
    assert evidence.step_id == STEP_ID
    assert evidence.canonical_file == CANONICAL_FILE
    assert "Username" in evidence.content
    assert "private-query" not in evidence.url
    assert registry.reads == [("verified", CANONICAL_FILE, MAX_UI_DOM_EXCERPT_BYTES)]


@pytest.mark.parametrize(
    ("report_stage", "registry_stage"),
    [("ui_action", "action"), ("ui_verification", "verification")],
)
def test_failed_run_loads_only_exact_repairable_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_stage: str,
    registry_stage: str,
) -> None:
    registry = _Registry(
        diagnostic=_diagnostic(failure_stage=registry_stage),
    )
    settings = _configure(monkeypatch, tmp_path, registry)
    _write_scenario(settings)
    _write_report(
        settings,
        _report(status="failed", failure=_failure(report_stage=report_stage)),
    )

    evidence = load_ui_run_evidence(tmp_path, RUN_ID)

    assert evidence.evidence_state == "diagnostic"
    assert evidence.failure_stage == registry_stage
    assert evidence.failure is not None
    assert evidence.failure.operation_target == "role:button[name=Login]"
    assert registry.reads == [
        (
            "diagnostic",
            CANONICAL_FILE,
            STEP_ID,
            registry_stage,
            MAX_UI_DOM_EXCERPT_BYTES,
        )
    ]


def test_ui_evidence_is_redacted_before_agent_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = "classified-marker"
    registry = _Registry(
        verified=_verified(content=f'- text "{protected}"\n'),
    )
    settings = _configure(
        monkeypatch,
        tmp_path,
        registry,
        sensitive_keys=("classified",),
    )
    _write_scenario(settings)
    _write_report(settings, _report())

    evidence = load_ui_run_evidence(tmp_path, RUN_ID)

    assert protected not in evidence.content
    assert evidence.content == REDACTED
    assert protected not in repr(evidence)


@pytest.mark.parametrize(
    ("report", "message"),
    [
        (_report(status="cancelled"), "unsupported run status"),
        (_report(output="../outside.semantic.json"), "reference is invalid"),
        (_report(failure=_failure()), "contains a failure"),
        (
            _report(
                status="failed",
                failure=_failure(report_stage="ui_navigation"),
            ),
            "stage is not repairable",
        ),
        (
            _report(
                status="failed",
                failure=_failure(snapshot="snapshots/nested/evidence.semantic.json"),
            ),
            "reference is invalid",
        ),
    ],
)
def test_untrusted_report_evidence_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: dict[str, Any],
    message: str,
) -> None:
    registry = _Registry(
        verified=_verified(),
        diagnostic=_diagnostic(),
    )
    settings = _configure(monkeypatch, tmp_path, registry)
    _write_scenario(settings)
    _write_report(settings, report)

    with pytest.raises(UiEvidenceError, match=message):
        load_ui_run_evidence(tmp_path, RUN_ID)


def test_wrong_activity_and_invalid_output_contract_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _Registry(verified=_verified(activity="different"))
    settings = _configure(monkeypatch, tmp_path, registry)
    _write_scenario(settings, activity="login_page")
    _write_report(settings, _report())

    with pytest.raises(UiEvidenceError, match="does not match its activity"):
        load_ui_run_evidence(tmp_path, RUN_ID)

    _write_scenario(settings, output="${discover_login.url}")
    with pytest.raises(UiEvidenceError, match="snapshot output is invalid"):
        load_ui_run_evidence(tmp_path, RUN_ID)


def test_ui_evidence_model_rejects_inconsistent_diagnostic_state() -> None:
    with pytest.raises(ValidationError, match="failure stage"):
        UiRunEvidence(
            run_id=RUN_ID,
            scenario="Discover login",
            source_path=SOURCE_PATH,
            step_id=STEP_ID,
            activity=STEP_ID,
            canonical_file=CANONICAL_FILE,
            evidence_state="verified",
            failure_stage="action",
            parameters=CapturePageSnapshotParams(
                id=STEP_ID,
                url="https://example.test/login",
            ),
            url="https://example.test/login",
            page_title="Sign in",
            content='- button "Login"\n',
        )
