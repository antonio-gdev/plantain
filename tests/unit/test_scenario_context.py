"""Scenario context navigation, isolation, and sanitization behavior."""

from __future__ import annotations

import pytest

from plantain.engine.context import (
    MAX_CONTEXT_PATH_LENGTH,
    MAX_CONTEXT_PATH_TOKENS,
    ScenarioContext,
)
from plantain.errors import ExpressionResolutionError
from plantain.security.redaction import REDACTED


def test_context_navigates_dot_slash_and_list_paths_without_export_aliasing() -> None:
    context = ScenarioContext()
    context.set_result(
        "lookup",
        {
            "rows": [{"item": "Jacket", "prices": ["19.99", "24.99"]}],
            "empty": None,
        },
    )

    assert context.get("lookup.rows[0].item") == "Jacket"
    assert context.get("/lookup/rows/0/prices/1") == "24.99"
    assert context.get("lookup.empty") is None
    assert context.contains("lookup") is True
    assert context.last_step == "lookup"

    exported = context.export()
    exported["lookup"]["rows"][0]["item"] = "Changed"
    assert context.get("lookup.rows[0].item") == "Jacket"


def test_context_replaces_existing_results_and_clears_all_state() -> None:
    context = ScenarioContext()
    context.set_result("step", {"status": "created"})
    context.replace_result("step", {"status": "updated"})

    assert context.get("step.status") == "updated"

    context.clear()

    assert context.export() == {}
    assert context.contains("step") is False
    assert context.last_step is None


def test_context_rejects_duplicate_and_missing_replacements() -> None:
    context = ScenarioContext()
    context.set_result("step", {"ok": True})

    with pytest.raises(ExpressionResolutionError, match="already exists"):
        context.set_result("step", {"ok": False})
    with pytest.raises(ExpressionResolutionError, match="No result exists"):
        context.replace_result("missing", {"ok": False})


@pytest.mark.parametrize(
    "path",
    ["", ".", "step..value", "step[", "step[-1]"],
)
def test_context_rejects_malformed_dot_paths(path: str) -> None:
    with pytest.raises(ExpressionResolutionError):
        ScenarioContext().get(path)


def test_context_bounds_path_length_and_segment_count() -> None:
    with pytest.raises(ExpressionResolutionError, match="maximum length"):
        ScenarioContext().get("x" * (MAX_CONTEXT_PATH_LENGTH + 1))
    with pytest.raises(ExpressionResolutionError, match="too many segments"):
        ScenarioContext().get(".".join("x" for _ in range(MAX_CONTEXT_PATH_TOKENS + 1)))


def test_context_reports_structural_navigation_failures_without_values() -> None:
    context = ScenarioContext()
    context.set_result("step", {"rows": [{"item": "Jacket"}], "scalar": "text"})

    with pytest.raises(ExpressionResolutionError, match="does not contain key 'missing'"):
        context.get("step.missing")
    with pytest.raises(ExpressionResolutionError, match="out of range"):
        context.get("step.rows[2]")
    with pytest.raises(ExpressionResolutionError, match="Expected an object"):
        context.get("step.rows.item")
    with pytest.raises(ExpressionResolutionError, match="Expected a list"):
        context.get("step.scalar[0]")
    with pytest.raises(ExpressionResolutionError, match="Invalid context path"):
        context.get("///")


def test_context_sanitized_export_masks_nested_sensitive_keys() -> None:
    context = ScenarioContext()
    observed_value = "synthetic-sensitive-value"
    context.set_result("login", {"password": observed_value, "username": "tester"})

    assert context.export(sanitized=True) == {"login": {"password": REDACTED, "username": "tester"}}
    assert context.get("login.password") == observed_value
