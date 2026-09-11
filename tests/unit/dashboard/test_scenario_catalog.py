"""Bounded and browser-safe dashboard scenario catalog coverage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plantain.dashboard import scenario_catalog
from plantain.dashboard.scenario_catalog import (
    MAX_SCENARIO_PAGE_SIZE,
    MAX_SCENARIO_SEARCH_MATCHES,
    ScenarioCatalogError,
    ScenarioCatalogFilter,
    is_valid_scenario_id,
    load_scenario_catalog,
    load_scenario_selection,
    resolve_scenario_path,
    search_scenario_catalog,
)
from plantain.engine.registry import ActivityRegistry
from plantain.errors import ActivityRegistrationError, ScenarioLoadError
from plantain.models.common import StrictModel
from plantain.models.scenario import ScenarioDefinition

EXPECTED_PAGE_SIZE = 2
EXPECTED_SEARCH_MATCHES = 2
EXPECTED_TOTAL_TESTS = 3
EXPECTED_VISIBLE_TAGS = 3
LIMITED_SEARCH_TOTAL = MAX_SCENARIO_SEARCH_MATCHES + 1
TEST_YAML_MAX_BYTES = 100_000
TEST_YAML_MAX_NODES = 10_000
TEST_YAML_MAX_DEPTH = 100


class _RequestParams(StrictModel):
    id: str
    value: int


async def _request_handler(_context: Any, _params: _RequestParams) -> None:
    return None


class _ScenarioValidator:
    def __init__(
        self,
        _settings: object,
        registry: ActivityRegistry,
        *,
        reporting_environ: object,
    ) -> None:
        assert reporting_environ == {}
        self.registry = registry

    def validate_scenario(self, scenario: ScenarioDefinition) -> None:
        for step in scenario.steps:
            self.registry.validate(step.activity, step.params)


def _register_test_activity(registry: ActivityRegistry) -> None:
    registry.register(
        "sendRequest",
        _RequestParams,
        _request_handler,
        description="Test-only API activity",
    )


def _configure_catalog(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> SimpleNamespace:
    scenarios_dir = root / "scenarios"
    scenarios_dir.mkdir()
    settings = SimpleNamespace(
        project_root=root,
        scenarios_dir=scenarios_dir,
        yaml_max_bytes=TEST_YAML_MAX_BYTES,
        yaml_max_nodes=TEST_YAML_MAX_NODES,
        yaml_max_depth=TEST_YAML_MAX_DEPTH,
        sensitive_key_names=(),
    )
    monkeypatch.setattr(
        scenario_catalog.Settings,
        "from_env",
        lambda _root: settings,
    )
    monkeypatch.setattr(
        scenario_catalog,
        "dashboard_reporting_runtime",
        lambda configured: SimpleNamespace(settings=configured, environment={}),
    )
    monkeypatch.setattr(
        scenario_catalog,
        "register_framework_activities",
        _register_test_activity,
    )
    monkeypatch.setattr(scenario_catalog, "ScenarioRunner", _ScenarioValidator)
    return settings


def _write_scenario(
    path: Path,
    name: str,
    *,
    value: object = 1,
    tags: tuple[str, ...] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "scenario": name,
        "tags": list(tags),
        "steps": [
            {
                "sendRequest": {
                    "id": "request",
                    "value": value,
                }
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_catalog_pages_valid_and_invalid_tests_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    _write_scenario(
        settings.scenarios_dir / "a.yaml",
        "Checkout",
        tags=("smoke", "api", "checkout", "regression"),
    )
    _write_scenario(settings.scenarios_dir / "b.yaml", "Inventory")
    _write_scenario(
        settings.scenarios_dir / "c.yaml",
        "Broken",
        value="not-an-integer",
    )

    first = load_scenario_catalog(tmp_path, page_size=EXPECTED_PAGE_SIZE)
    last = load_scenario_catalog(
        tmp_path,
        page=99,
        page_size=EXPECTED_PAGE_SIZE,
    )

    assert first.total_count == EXPECTED_TOTAL_TESTS
    assert first.page == 1
    assert first.page_count == EXPECTED_PAGE_SIZE
    assert first.has_previous is False
    assert first.has_next is True
    assert [item.name for item in first.items] == ["Checkout", "Inventory"]
    assert first.items[0].domains == ("API",)
    assert len(first.items[0].tags) == EXPECTED_VISIBLE_TAGS
    assert first.items[0].additional_tag_count == 1
    assert first.notice == "Showing 1-2 of 3 tests."

    assert last.page == EXPECTED_PAGE_SIZE
    assert last.has_previous is True
    assert last.has_next is False
    assert len(last.items) == 1
    assert last.items[0].status == "needs_review"
    assert last.items[0].source == "c.yaml"
    assert last.items[0].issue == "Scenario YAML or activity parameters need review."
    assert "not-an-integer" not in repr(last)
    assert last.notice == ("Showing 3-3 of 3 tests. 1 test needs review on this page.")


def test_catalog_filters_and_selects_the_complete_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    _write_scenario(
        settings.scenarios_dir / "api/checkout.yaml",
        "Checkout API",
        tags=("smoke", "api"),
    )
    _write_scenario(
        settings.scenarios_dir / "api/broken.yaml",
        "Broken API",
        value="invalid",
        tags=("smoke", "api"),
    )
    catalog_filter = ScenarioCatalogFilter.create(
        query="checkout",
        required_all=("SMOKE",),
    )

    page = load_scenario_catalog(tmp_path, catalog_filter=catalog_filter)
    selection = load_scenario_selection(
        tmp_path,
        catalog_filter=ScenarioCatalogFilter.create(query="api"),
    )

    assert [item.name for item in page.items] == ["Checkout API"]
    assert [item.status for item in selection.items] == ["needs_review", "ready"]
    assert selection.ready_count == 1
    assert selection.review_count == 1


def test_empty_workspace_returns_an_empty_first_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(
        scenario_catalog,
        "register_framework_activities",
        lambda _registry: pytest.fail("empty catalogs must not initialize activities"),
    )

    page = load_scenario_catalog(tmp_path)

    assert page.items == ()
    assert page.total_count == 0
    assert page.page == 1
    assert page.page_count == 0
    assert page.notice == ""


def test_opaque_identifier_resolves_only_through_current_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    target = settings.scenarios_dir / "team/checkout.yaml"
    _write_scenario(target, "Checkout")
    item = load_scenario_catalog(tmp_path).items[0]

    assert item.source == "team/checkout.yaml"
    assert not hasattr(item, "path")
    assert is_valid_scenario_id(item.scenario_id)
    assert not is_valid_scenario_id("../checkout.yaml")
    assert not is_valid_scenario_id(None)
    assert resolve_scenario_path(tmp_path, item.scenario_id) == target
    with pytest.raises(ScenarioCatalogError, match="identifier is invalid"):
        resolve_scenario_path(tmp_path, "../checkout.yaml")
    with pytest.raises(ScenarioCatalogError, match="no longer available"):
        resolve_scenario_path(tmp_path, "f" * 64)


def test_catalog_redacts_displayed_names_and_relative_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    observed_value = "synthetic-sensitive-value"
    target = settings.scenarios_dir / f"token={observed_value}.yaml"
    _write_scenario(target, f"password={observed_value}")

    item = load_scenario_catalog(tmp_path).items[0]

    assert observed_value not in item.name
    assert observed_value not in item.source
    assert observed_value not in repr(item)


def test_search_ranks_safe_relative_identity_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    _write_scenario(
        settings.scenarios_dir / "api/petstore/find_available_pets.yaml",
        "Find available pets",
    )
    _write_scenario(
        settings.scenarios_dir / "ui/available_pets.yaml",
        "Browse available pets",
    )
    _write_scenario(
        settings.scenarios_dir / "api/inventory.yaml",
        "Inventory",
    )

    result = search_scenario_catalog(tmp_path, "petstore available pets")

    assert result.total_matches == EXPECTED_SEARCH_MATCHES
    assert result.matches_limited is False
    assert [item.name for item in result.items] == [
        "Find available pets",
        "Browse available pets",
    ]
    assert all(not hasattr(item, "path") for item in result.items)


def test_search_bounds_explicit_all_scenario_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    for index in range(LIMITED_SEARCH_TOTAL):
        _write_scenario(
            settings.scenarios_dir / f"case-{index:02d}.yaml",
            f"Case {index}",
        )

    result = search_scenario_catalog(tmp_path, "all tests")

    assert result.total_matches == LIMITED_SEARCH_TOTAL
    assert len(result.items) == MAX_SCENARIO_SEARCH_MATCHES
    assert result.matches_limited is True


@pytest.mark.parametrize("query", ["", "---", "x" * 241, None])
def test_search_rejects_invalid_queries(
    tmp_path: Path,
    *,
    query: Any,
) -> None:
    with pytest.raises(ScenarioCatalogError, match="search query"):
        search_scenario_catalog(tmp_path, query)


@pytest.mark.parametrize(
    ("page", "page_size"),
    [
        (0, 1),
        (True, 1),
        (1, 0),
        (1, False),
        (1, MAX_SCENARIO_PAGE_SIZE + 1),
    ],
)
def test_catalog_rejects_invalid_page_requests(
    tmp_path: Path,
    *,
    page: Any,
    page_size: Any,
) -> None:
    with pytest.raises(ScenarioCatalogError, match="catalog page"):
        load_scenario_catalog(tmp_path, page=page, page_size=page_size)


def test_catalog_translates_discovery_and_registration_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure_catalog(monkeypatch, tmp_path)
    _write_scenario(settings.scenarios_dir / "test.yaml", "Test")

    def reject_discovery(*_args: Any, **_kwargs: Any) -> list[Path]:
        raise ScenarioLoadError("private discovery detail")

    monkeypatch.setattr(
        scenario_catalog,
        "discover_scenario_paths",
        reject_discovery,
    )
    with pytest.raises(ScenarioCatalogError, match="loaded safely") as discovery:
        load_scenario_catalog(tmp_path)
    assert "private discovery detail" not in str(discovery.value)

    monkeypatch.setattr(
        scenario_catalog,
        "discover_scenario_paths",
        lambda *_args, **_kwargs: [settings.scenarios_dir / "test.yaml"],
    )

    def reject_registration(_registry: ActivityRegistry) -> None:
        raise ActivityRegistrationError("private registration detail")

    monkeypatch.setattr(
        scenario_catalog,
        "register_framework_activities",
        reject_registration,
    )
    with pytest.raises(ScenarioCatalogError, match="activity catalog") as registration:
        load_scenario_catalog(tmp_path)
    assert "private registration detail" not in str(registration.value)
