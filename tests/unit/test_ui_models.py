"""Strict user-facing YAML contracts for generic UI automation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plantain.models.ui import (
    CapturePageSnapshotParams,
    CapturePageSnapshotResult,
    FrameTarget,
    LocatorSpec,
    SelectChoice,
    SnapshotArtifact,
    UiAction,
    UiActionType,
    UiAssertion,
    UiAssertionType,
)

ACTION_COUNT = 2
BEYOND_LEGACY_FRAME_DEPTH = 13
BEYOND_LEGACY_OPERATION_COUNT = 1_001
CAPTURE_TIMEOUT_MS = 1_000
POPUP_TIMEOUT_MS = 500
RESPONSE_TIMEOUT_MS = 15_000


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "checkout-frame"},
        {"url": "https://example.test/frame"},
        {"css": "iframe#checkout"},
    ],
)
def test_frame_target_requires_one_supported_strategy(payload: dict[str, str]) -> None:
    frame = FrameTarget.model_validate(payload)

    assert sum(value is not None for value in (frame.name, frame.url, frame.css)) == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": "frame", "url": "https://example.test/frame"}],
)
def test_frame_target_rejects_missing_or_mixed_strategies(
    payload: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        FrameTarget.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "button", "name": "Continue"},
        {"label": "First Name"},
        {"placeholder": "Username"},
        {"text": "Checkout"},
        {"altText": "Cart"},
        {"title": "Inventory"},
        {"testId": "checkout"},
        {"css": "#continue"},
        {"xpath": "//button[@id='continue']"},
    ],
)
def test_locator_accepts_every_semantic_and_explicit_strategy(
    payload: dict[str, str],
) -> None:
    locator = LocatorSpec.model_validate(payload)

    assert locator.exact is True
    assert locator.visible is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"css": "#first", "text": "Second"},
        {"name": "Continue"},
        {"css": "#item", "nth": -1},
    ],
)
def test_locator_rejects_ambiguous_or_invalid_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LocatorSpec.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [{"value": "available"}, {"label": "Available"}, {"index": 0}],
)
def test_select_choice_requires_one_strategy(payload: dict[str, object]) -> None:
    assert SelectChoice.model_validate(payload).model_dump(exclude_none=True)


@pytest.mark.parametrize(
    "payload",
    [{}, {"value": "available", "label": "Available"}, {"index": -1}],
)
def test_select_choice_rejects_missing_mixed_or_negative_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SelectChoice.model_validate(payload)


def test_ui_action_parses_normalized_shorthand_and_null_page_actions() -> None:
    normalized = UiAction.from_yaml(
        {
            "action": "click",
            "target": {"role": "button", "name": "Login"},
        }
    )
    shorthand = UiAction.from_yaml(
        {
            "fill": {
                "target": {"placeholder": "Username"},
                "value": "standard_user",
            }
        }
    )
    reload_action = UiAction.from_yaml({"reload": None})

    assert normalized.action is UiActionType.CLICK
    assert normalized.target == LocatorSpec(role="button", name="Login")
    assert shorthand.action is UiActionType.FILL
    assert shorthand.value == "standard_user"
    assert reload_action.action is UiActionType.RELOAD


def test_ui_action_shorthand_scalar_rules_are_explicit() -> None:
    with pytest.raises(ValidationError, match="requires a target"):
        UiAction.from_yaml({"fill": "value"})
    with pytest.raises(ValueError, match="payload must be a mapping"):
        UiAction.from_yaml({"click": "unsupported"})


@pytest.mark.parametrize("payload", [None, "click", ["click"]])
def test_ui_action_requires_a_mapping(payload: object) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        UiAction.from_yaml(payload)


def test_ui_action_rejects_multi_key_shorthand() -> None:
    with pytest.raises(ValueError, match="exactly one action key"):
        UiAction.from_yaml({"reload": {}, "goBack": {}})


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "click"},
        {"action": "fill", "target": {"css": "#field"}},
        {"action": "type", "target": {"css": "#field"}},
        {"action": "press", "target": {"css": "#field"}},
        {"action": "select", "target": {"css": "#field"}},
        {"action": "waitForUrl"},
        {"action": "reload", "expectPopup": True},
    ],
)
def test_ui_action_rejects_missing_or_incompatible_arguments(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        UiAction.model_validate(payload)


def test_ui_action_accepts_key_choice_timeout_and_popup_contracts() -> None:
    press = UiAction.model_validate(
        {"action": "press", "target": {"css": "#field"}, "key": "Enter"}
    )
    select = UiAction.model_validate(
        {
            "action": "select",
            "target": {"css": "#status"},
            "choices": [{"value": "available"}],
        }
    )
    popup = UiAction.model_validate(
        {
            "action": "click",
            "target": {"text": "Open"},
            "expectPopup": True,
            "timeoutMs": POPUP_TIMEOUT_MS,
        }
    )

    assert press.key == "Enter"
    assert select.choices == [SelectChoice(value="available")]
    assert popup.expect_popup is True
    assert popup.timeout_ms == POPUP_TIMEOUT_MS


def test_ui_action_accepts_causal_response_expectation() -> None:
    action = UiAction.from_yaml(
        {
            "click": {
                "target": {"role": "button", "name": "Submit"},
                "expectResponse": {
                    "url": "**/api/orders",
                    "method": "POST",
                    "status": [200, 201],
                    "timeoutMs": RESPONSE_TIMEOUT_MS,
                },
            }
        }
    )

    expectation = action.expect_response
    assert expectation is not None
    assert expectation.url == "**/api/orders"
    assert expectation.method.value == "POST"
    assert expectation.statuses == (200, 201)
    assert expectation.timeout_ms == RESPONSE_TIMEOUT_MS


@pytest.mark.parametrize(
    "expectation",
    [
        {"url": "**/api/orders", "method": "POST"},
        {"url": "**/api/orders", "method": "TRACE", "status": 200},
        {"url": "**/api/orders", "method": "POST", "status": []},
        {"url": "**/api/orders", "method": "POST", "status": 99},
        {"url": "**/api/orders", "method": "POST", "status": 600},
    ],
)
def test_ui_action_rejects_invalid_response_expectation(
    expectation: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        UiAction.model_validate(
            {
                "action": "click",
                "target": {"css": "#submit"},
                "expectResponse": expectation,
            }
        )


def test_ui_assertion_parses_normalized_and_shorthand_forms() -> None:
    visible = UiAssertion.from_yaml({"assertion": "visible", "target": {"css": "#result"}})
    url = UiAssertion.from_yaml({"url": "https://example.test/complete"})
    text = UiAssertion.from_yaml({"text": {"target": {"css": "#result"}, "contains": "Complete"}})

    assert visible.assertion is UiAssertionType.VISIBLE
    assert url.equals == "https://example.test/complete"
    assert text.contains == "Complete"


@pytest.mark.parametrize("payload", [None, "visible", ["visible"]])
def test_ui_assertion_requires_a_mapping(payload: object) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        UiAssertion.from_yaml(payload)


def test_ui_assertion_rejects_multi_key_shorthand() -> None:
    with pytest.raises(ValueError, match="exactly one assertion key"):
        UiAssertion.from_yaml({"url": "one", "title": "two"})


@pytest.mark.parametrize(
    "payload",
    [
        {"assertion": "visible"},
        {"assertion": "text", "target": {"css": "#result"}},
        {"assertion": "value", "target": {"css": "#field"}},
        {"assertion": "count", "target": {"css": ".row"}, "equals": "2"},
        {"assertion": "attribute", "target": {"css": "#result"}},
        {"assertion": "url"},
        {"assertion": "title"},
    ],
)
def test_ui_assertion_rejects_missing_or_invalid_expectations(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        UiAssertion.model_validate(payload)


def test_capture_contract_parses_real_yaml_aliases_and_shorthand() -> None:
    params = CapturePageSnapshotParams.model_validate(
        {
            "id": "discover_inventory",
            "url": "env:SAUCE_DEMO_URL",
            "actions": [
                {
                    "fill": {
                        "target": {"placeholder": "Username"},
                        "value": "env:SAUCE_DEMO_USERNAME",
                    }
                },
                {"waitForUrl": {"value": "**/inventory.html"}},
            ],
            "verify": [{"url": {"equals": "https://www.saucedemo.com/inventory.html"}}],
            "snapshot": {"enabled": True, "captureAfterEachAction": False},
            "timeoutMs": CAPTURE_TIMEOUT_MS,
        }
    )

    assert [action.action for action in params.actions] == [
        UiActionType.FILL,
        UiActionType.WAIT_FOR_URL,
    ]
    assert params.verify[0].assertion is UiAssertionType.URL
    assert params.snapshot.capture_after_each_action is False
    assert params.timeout_ms == CAPTURE_TIMEOUT_MS


def test_capture_contract_relies_on_resource_budgets_not_legacy_list_caps() -> None:
    params = CapturePageSnapshotParams.model_validate(
        {
            "actions": [
                {"waitForUrl": {"value": "**/ready"}} for _ in range(BEYOND_LEGACY_OPERATION_COUNT)
            ],
            "verify": [
                {"url": {"equals": "https://example.test/ready"}}
                for _ in range(BEYOND_LEGACY_OPERATION_COUNT)
            ],
        }
    )
    locator = LocatorSpec.model_validate(
        {
            "css": "#target",
            "frames": [{"name": f"frame-{index}"} for index in range(BEYOND_LEGACY_FRAME_DEPTH)],
        }
    )

    assert len(params.actions) == BEYOND_LEGACY_OPERATION_COUNT
    assert len(params.verify) == BEYOND_LEGACY_OPERATION_COUNT
    assert len(locator.frames) == BEYOND_LEGACY_FRAME_DEPTH


def test_capture_contract_normalizes_null_lists_and_requires_work() -> None:
    params = CapturePageSnapshotParams.model_validate(
        {"url": "https://example.test", "actions": None, "verify": None}
    )

    assert params.actions == []
    assert params.verify == []

    with pytest.raises(ValidationError, match="requires url, actions, or verify"):
        CapturePageSnapshotParams.model_validate({})
    with pytest.raises(TypeError, match="actions must be a list"):
        CapturePageSnapshotParams.model_validate({"url": "https://example.test", "actions": {}})
    with pytest.raises(TypeError, match="verify must be a list"):
        CapturePageSnapshotParams.model_validate({"url": "https://example.test", "verify": {}})


def test_capture_result_serializes_yaml_facing_aliases() -> None:
    result = CapturePageSnapshotResult(
        url="https://example.test/inventory",
        page_title="Inventory",
        actions_executed=ACTION_COUNT,
        verifications_passed=1,
        snapshots=[
            SnapshotArtifact(
                activity="inventory",
                canonical_file="snapshots/inventory.semantic.json",
                status="created",
            )
        ],
    )

    rendered = result.model_dump(mode="python", by_alias=True)

    assert rendered["pageTitle"] == "Inventory"
    assert rendered["actionsExecuted"] == ACTION_COUNT
    assert rendered["snapshots"][0]["canonicalFile"] == ("snapshots/inventory.semantic.json")
    assert rendered["snapshots"][0]["evidenceState"] == "verified"
    assert rendered["snapshots"][0]["failureStage"] is None


def test_snapshot_artifact_requires_consistent_diagnostic_state() -> None:
    base = {
        "activity": "inventory",
        "canonicalFile": "snapshots/inventory.semantic.json",
        "status": "created",
    }

    with pytest.raises(ValidationError, match="exactly one failure stage"):
        SnapshotArtifact.model_validate({**base, "evidenceState": "diagnostic"})
    with pytest.raises(ValidationError, match="exactly one failure stage"):
        SnapshotArtifact.model_validate({**base, "failureStage": "action"})

    artifact = SnapshotArtifact.model_validate(
        {**base, "evidenceState": "diagnostic", "failureStage": "verification"}
    )
    assert artifact.evidence_state == "diagnostic"
    assert artifact.failure_stage == "verification"
