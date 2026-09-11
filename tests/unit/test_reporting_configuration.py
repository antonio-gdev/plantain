"""Configuration and CLI preflight tests for optional Zephyr reporting."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from plantain import cli
from plantain.config import Settings
from plantain.errors import ConfigurationError, PlantainError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.reporting.zephyr import TOKEN_ENVIRONMENT_NAME

EXPECTED_MAX_CONNECTIONS = 3
EXPECTED_CLEANUP_TIMEOUT_SECONDS = 7.5
EXPECTED_OUTBOX_SETTINGS = (123, 4, 2.5)
OVERLONG_REMOTE_METADATA = "x" * 129


def test_settings_load_public_controls_without_retaining_pat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.test")
    monkeypatch.setenv("PLANTAIN_ZEPHYR_PUBLISH_RESULTS", "true")
    monkeypatch.setenv("PLANTAIN_ZEPHYR_ATTACH_REPORT", "true")
    monkeypatch.setenv(
        "PLANTAIN_ZEPHYR_ATTACHMENT_DATA_GOVERNANCE_APPROVED",
        "true",
    )
    monkeypatch.setenv("PLANTAIN_ZEPHYR_MAX_CONNECTIONS", "3")
    monkeypatch.setenv("PLANTAIN_ZEPHYR_OUTBOX_MAX_ENTRIES", "123")
    monkeypatch.setenv("PLANTAIN_ZEPHYR_OUTBOX_RETRY_BATCH_SIZE", "4")
    monkeypatch.setenv("PLANTAIN_ZEPHYR_OUTBOX_LOCK_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("PLANTAIN_ALLURE_RESULTS_ENABLED", "true")
    monkeypatch.setenv("PLANTAIN_CLEANUP_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, "synthetic-not-loaded-by-settings")

    settings = Settings.from_env(tmp_path)
    serialized = asdict(settings)

    assert settings.zephyr_base_url == "https://jira.example.test"
    assert settings.zephyr_publish_results is True
    assert settings.zephyr_attach_report is True
    assert settings.zephyr_attachment_data_governance_approved is True
    assert settings.zephyr_max_connections == EXPECTED_MAX_CONNECTIONS
    assert settings.allure_results_enabled is True
    assert settings.cleanup_timeout_seconds == EXPECTED_CLEANUP_TIMEOUT_SECONDS
    assert (
        settings.zephyr_outbox_max_entries,
        settings.zephyr_outbox_retry_batch_size,
        settings.zephyr_outbox_lock_timeout_seconds,
    ) == EXPECTED_OUTBOX_SETTINGS
    assert TOKEN_ENVIRONMENT_NAME not in serialized
    assert "synthetic-not-loaded-by-settings" not in str(serialized)


def test_settings_reject_attachment_without_governance_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANTAIN_ZEPHYR_ATTACH_REPORT", "true")

    with pytest.raises(ConfigurationError, match="DATA_GOVERNANCE_APPROVED"):
        Settings.from_env(tmp_path)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLANTAIN_ZEPHYR_OUTBOX_MAX_ENTRIES", "100001"),
        ("PLANTAIN_ZEPHYR_OUTBOX_RETRY_BATCH_SIZE", "17"),
        ("PLANTAIN_ZEPHYR_OUTBOX_LOCK_TIMEOUT_SECONDS", "60.1"),
    ],
)
def test_settings_reject_outbox_controls_above_hard_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env(tmp_path)


def test_cli_validate_enforces_enabled_reporting_metadata_before_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, "synthetic-cli-pat")
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.test")
    monkeypatch.setenv("PLANTAIN_ZEPHYR_PUBLISH_RESULTS", "true")
    settings = Settings.from_env(tmp_path)
    scenario = ScenarioDefinition(
        scenario="Missing reporting metadata",
        steps=[StepDefinition(activity="unknownActivity", params={"id": "unknown"})],
    )
    monkeypatch.setattr(cli, "_selected_scenarios", lambda _args, _settings: [scenario])

    with pytest.raises(PlantainError, match="testCaseKey"):
        cli._validate(argparse.Namespace(), settings)


@pytest.mark.parametrize(
    "field",
    ["jira_ticket", "test_case_key", "test_run_key"],
)
def test_optional_reporting_metadata_is_bounded_only_when_supplied(field: str) -> None:
    values = {
        "scenario": "Optional reporting metadata",
        "steps": [StepDefinition(activity="probe", params={})],
        field: OVERLONG_REMOTE_METADATA,
    }

    with pytest.raises(ValidationError, match="128"):
        ScenarioDefinition(**values)
