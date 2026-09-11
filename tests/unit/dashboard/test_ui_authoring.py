"""Progressive dashboard UI authoring remains evidence-grounded."""

from __future__ import annotations

import pytest

from plantain.dashboard.agent.ui_authoring import (
    UiDraftValidationError,
    ui_authoring_context,
    validate_ui_draft,
)
from plantain.dashboard.agent.ui_models import (
    UI_DISCOVERY_OUTPUT,
    UiFailureEvidence,
    UiFailureStage,
    UiRunEvidence,
)
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.models.ui import CapturePageSnapshotParams, UiAction, UiAssertion

RUN_ID = "a" * 32
SOURCE_PATH = "generated/ui/discover-login.yaml"
STEP_ID = "discover_login"
CANONICAL_FILE = "login.semantic.json"


def _parameters(
    *,
    actions: list[UiAction] | None = None,
    verify: list[UiAssertion] | None = None,
    url: str = "https://example.test/login",
) -> CapturePageSnapshotParams:
    return CapturePageSnapshotParams(
        id=STEP_ID,
        url=url,
        actions=[action.model_dump(mode="json", by_alias=True) for action in actions or []],
        verify=[assertion.model_dump(mode="json", by_alias=True) for assertion in verify or []],
    )


def _scenario(
    parameters: CapturePageSnapshotParams,
    *,
    output: str | None = None,
) -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario="Discover login",
        steps=[
            StepDefinition(
                activity="capturePageSnapshot",
                params=parameters.model_dump(mode="json", by_alias=True),
            )
        ],
        outputs={UI_DISCOVERY_OUTPUT: output or f"${{{STEP_ID}.snapshots[0].canonicalFile}}"},
    )


def _evidence(
    parameters: CapturePageSnapshotParams,
    *,
    content: str = '- textbox "Username"\n- button "Login"\n',
    failure_stage: UiFailureStage | None = None,
    operation_type: str = "click",
    operation_index: int = 1,
    operation_total: int = 1,
) -> UiRunEvidence:
    failure = None
    state = "verified"
    if failure_stage is not None:
        state = "diagnostic"
        failure = UiFailureEvidence(
            operation_index=operation_index,
            operation_total=operation_total,
            operation_type=operation_type,
            operation_target="css:#old-target",
            operation_error_type="TimeoutError",
        )
    return UiRunEvidence(
        run_id=RUN_ID,
        scenario="Discover login",
        source_path=SOURCE_PATH,
        step_id=STEP_ID,
        activity=STEP_ID,
        canonical_file=CANONICAL_FILE,
        evidence_state=state,
        failure_stage=failure_stage,
        failure=failure,
        parameters=parameters,
        url="https://example.test/login",
        page_title="Sign in",
        content=content,
    )


def _click(locator: dict[str, str], *, force: bool = False) -> UiAction:
    return UiAction.model_validate(
        {
            "action": "click",
            "target": locator,
            "force": force,
        }
    )


def _visible(locator: dict[str, str]) -> UiAssertion:
    return UiAssertion.model_validate(
        {
            "assertion": "visible",
            "target": locator,
        }
    )


def test_initial_discovery_is_url_only_and_exposes_snapshot() -> None:
    initial = _scenario(_parameters())

    validate_ui_draft(initial, None)

    invented = _scenario(_parameters(actions=[_click({"role": "button", "name": "Login"})]))
    with pytest.raises(UiDraftValidationError, match="cannot invent actions"):
        validate_ui_draft(invented, None)

    missing_output = _scenario(_parameters(), output="${discover_login.url}")
    with pytest.raises(UiDraftValidationError, match="canonical snapshot evidence"):
        validate_ui_draft(missing_output, None)


def test_verified_continuation_appends_grounded_behavior_only() -> None:
    prior = _parameters()
    evidence = _evidence(prior)
    continued = _scenario(_parameters(actions=[_click({"role": "button", "name": "Login"})]))

    validate_ui_draft(continued, evidence)

    invented = _scenario(_parameters(actions=[_click({"role": "button", "name": "Invented"})]))
    with pytest.raises(UiDraftValidationError, match="not present"):
        validate_ui_draft(invented, evidence)

    changed_url = _scenario(
        _parameters(
            actions=[_click({"role": "button", "name": "Login"})],
            url="https://other.example.test/login",
        )
    )
    changed_reviewed_match = "changed reviewed navigation"
    with pytest.raises(UiDraftValidationError, match=changed_reviewed_match):
        validate_ui_draft(changed_url, evidence)


def test_verified_continuation_preserves_existing_action_prefix() -> None:
    first = _click({"role": "button", "name": "Login"})
    prior = _parameters(actions=[first])
    evidence = _evidence(prior, content='- button "Continue"\n')
    continued = _scenario(
        _parameters(
            actions=[
                first,
                _click({"role": "button", "name": "Continue"}),
            ]
        )
    )

    validate_ui_draft(continued, evidence)

    rewritten = _scenario(
        _parameters(
            actions=[
                _click({"role": "button", "name": "Continue"}),
                first,
            ]
        )
    )
    with pytest.raises(UiDraftValidationError, match="changed reviewed actions"):
        validate_ui_draft(rewritten, evidence)


def test_action_diagnostic_repairs_only_failed_locator() -> None:
    prior_action = _click({"css": "#old-target"})
    prior = _parameters(actions=[prior_action])
    evidence = _evidence(
        prior,
        content='- button "Continue"\n',
        failure_stage="action",
    )
    repaired_action = _click({"role": "button", "name": "Continue"})
    repaired = _scenario(_parameters(actions=[repaired_action]))

    validate_ui_draft(repaired, evidence)

    changed_behavior = _scenario(
        _parameters(
            actions=[
                _click(
                    {"role": "button", "name": "Continue"},
                    force=True,
                )
            ]
        )
    )
    with pytest.raises(UiDraftValidationError, match="only its locator"):
        validate_ui_draft(changed_behavior, evidence)


def test_verification_diagnostic_repairs_only_failed_locator() -> None:
    prior_assertion = _visible({"css": "#old-target"})
    prior = _parameters(verify=[prior_assertion])
    evidence = _evidence(
        prior,
        content='- button "Continue"\n',
        failure_stage="verification",
        operation_type="visible",
    )
    repaired = _scenario(_parameters(verify=[_visible({"role": "button", "name": "Continue"})]))

    validate_ui_draft(repaired, evidence)

    changed_assertion = _scenario(
        _parameters(
            verify=[
                UiAssertion.model_validate(
                    {
                        "assertion": "hidden",
                        "target": {"role": "button", "name": "Continue"},
                    }
                )
            ]
        )
    )
    with pytest.raises(UiDraftValidationError, match="only its locator"):
        validate_ui_draft(changed_assertion, evidence)


def test_ui_context_excludes_backend_identity_and_exposes_exact_dom() -> None:
    evidence = _evidence(_parameters())

    initial = ui_authoring_context(None)
    continued = ui_authoring_context(evidence)

    assert "first pass only observes" in initial
    assert RUN_ID not in continued
    assert SOURCE_PATH not in continued
    assert CANONICAL_FILE not in continued
    assert 'button \\"Login\\"' in continued
    assert "priorStep" in continued
