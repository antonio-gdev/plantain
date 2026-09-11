"""Canonical in-memory scenario parsing for safe dashboard authoring."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.engine.loader import parse_scenario_text
from plantain.errors import ScenarioLoadError

VALID_SCENARIO = """\
scenario: Generated API check
tags:
  - generated
steps:
  - sendRequest:
      id: health
      endpoint: https://api.example.test/health
      method: GET
      expectedStatus: 200
outputs:
  status: ${health.statusCode}
"""


def _settings(*, max_bytes: int = 2_000_000) -> Any:
    return cast(
        "Any",
        SimpleNamespace(
            yaml_max_bytes=max_bytes,
            yaml_max_nodes=50_000,
            yaml_max_depth=100,
        ),
    )


def test_parse_scenario_text_normalizes_without_binding_a_source() -> None:
    scenario = parse_scenario_text(VALID_SCENARIO, _settings())

    assert scenario.scenario == "Generated API check"
    assert scenario.source_path is None
    assert scenario.steps[0].activity == "sendRequest"
    assert scenario.steps[0].params["id"] == "health"
    assert scenario.outputs == {"status": "${health.statusCode}"}


def test_parse_scenario_text_applies_canonical_yaml_limits() -> None:
    encoded_size = len(VALID_SCENARIO.encode())
    with pytest.raises(ScenarioLoadError, match="byte safety limit"):
        parse_scenario_text(
            VALID_SCENARIO,
            _settings(max_bytes=encoded_size - 1),
        )

    duplicate = """\
scenario: First
scenario: Second
steps:
  - sendRequest: {}
"""
    with pytest.raises(ScenarioLoadError, match="Duplicate YAML key"):
        parse_scenario_text(duplicate, _settings())


def test_parse_scenario_text_rejects_invalid_contract_shape() -> None:
    invalid = """\
scenario: Invalid
steps:
  - sendRequest: {}
  - sendRequest: {}
outputs: []
"""
    with pytest.raises(ScenarioLoadError, match="Scenario validation failed"):
        parse_scenario_text(invalid, _settings())
