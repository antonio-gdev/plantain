"""Scenario-detail state retains only a compact string projection."""

from plantain.dashboard.scenario_detail import ScenarioStepDetail
from plantain.dashboard.scenario_state import _scenario_step_row


def test_scenario_step_row_is_browser_safe() -> None:
    detail = ScenarioStepDetail(
        position=2,
        activity="sendRequest",
        step_id="request",
        yaml_text="- sendRequest:\n    id: request",
        content_limited=True,
    )

    assert _scenario_step_row(detail) == {
        "position": "2",
        "activity": "sendRequest",
        "step_id": "request",
        "yaml": "- sendRequest:\n    id: request",
        "limited": "true",
    }
