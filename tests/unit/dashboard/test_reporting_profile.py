"""Dashboard result profiles remain backend-only and environment-compatible."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from secrets import token_hex

import pytest

from plantain.config import Settings
from plantain.dashboard.reporting_profile import (
    DashboardReportingProfileError,
    clear_dashboard_reporting_profile,
    configure_dashboard_reporting_profile,
    dashboard_reporting_runtime,
    load_dashboard_reporting_profile,
)
from plantain.reporting.zephyr import TOKEN_ENVIRONMENT_NAME

_REPORTING_ENVIRONMENT_NAMES = (
    "JIRA_BASE_URL",
    "PLANTAIN_ALLURE_RESULTS_ENABLED",
    "PLANTAIN_ZEPHYR_ATTACHMENT_DATA_GOVERNANCE_APPROVED",
    "PLANTAIN_ZEPHYR_ATTACH_REPORT",
    "PLANTAIN_ZEPHYR_PUBLISH_RESULTS",
    TOKEN_ENVIRONMENT_NAME,
)


@pytest.fixture(autouse=True)
def _clear_process_profile(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    clear_dashboard_reporting_profile()
    for name in _REPORTING_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    clear_dashboard_reporting_profile()


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env(tmp_path)


def _enabled_settings(tmp_path: Path) -> Settings:
    return replace(
        _settings(tmp_path),
        allure_results_enabled=True,
        zephyr_base_url="https://jira.example.test/context",
        zephyr_publish_results=True,
    )


def test_environment_configuration_remains_the_fallback(tmp_path: Path) -> None:
    credential = token_hex(24)
    profile = load_dashboard_reporting_profile(
        _enabled_settings(tmp_path),
        {TOKEN_ENVIRONMENT_NAME: credential},
    )

    assert profile.allure_enabled is True
    assert profile.zephyr_enabled is True
    assert profile.credential_source == "environment"
    assert profile.session_configured is False
    assert credential not in repr(profile)


def test_session_profile_overlays_without_mutating_inputs(tmp_path: Path) -> None:
    credential = token_hex(24)
    settings = _settings(tmp_path)
    fallback: dict[str, str] = {}

    profile = configure_dashboard_reporting_profile(
        settings,
        allure_enabled=True,
        zephyr_enabled=True,
        zephyr_base_url="https://jira.example.test",
        credential=credential,
        environ=fallback,
    )
    runtime = dashboard_reporting_runtime(settings, fallback)

    assert profile.credential_source == "session"
    assert runtime.settings.allure_results_enabled is True
    assert runtime.settings.zephyr_publish_results is True
    assert runtime.environment[TOKEN_ENVIRONMENT_NAME] == credential
    assert settings.allure_results_enabled is False
    assert fallback == {}
    assert credential not in repr(profile)
    assert credential not in repr(runtime)
    assert credential not in repr(runtime.environment)


def test_blank_token_retains_session_value_and_disable_drops_it(
    tmp_path: Path,
) -> None:
    credential = token_hex(24)
    settings = _settings(tmp_path)
    configure_dashboard_reporting_profile(
        settings,
        allure_enabled=False,
        zephyr_enabled=True,
        zephyr_base_url="https://jira.example.test",
        credential=credential,
        environ={},
    )

    retained = configure_dashboard_reporting_profile(
        settings,
        allure_enabled=True,
        zephyr_enabled=True,
        zephyr_base_url="https://jira.example.test",
        credential="",
        environ={},
    )
    assert retained.credential_source == "session"

    disabled = configure_dashboard_reporting_profile(
        settings,
        allure_enabled=True,
        zephyr_enabled=False,
        environ={},
    )
    runtime = dashboard_reporting_runtime(settings, {})

    assert disabled.credential_source == "not_required"
    assert TOKEN_ENVIRONMENT_NAME not in runtime.environment


def test_attachment_requires_explicit_governance_approval(tmp_path: Path) -> None:
    with pytest.raises(DashboardReportingProfileError, match="Review"):
        configure_dashboard_reporting_profile(
            _settings(tmp_path),
            allure_enabled=False,
            zephyr_enabled=True,
            zephyr_base_url="https://jira.example.test",
            zephyr_attach_report=True,
            attachment_governance_approved=False,
            credential=token_hex(24),
            environ={},
        )


def test_invalid_values_are_not_echoed(tmp_path: Path) -> None:
    private_value = token_hex(24)
    with pytest.raises(DashboardReportingProfileError) as captured:
        configure_dashboard_reporting_profile(
            _settings(tmp_path),
            allure_enabled=False,
            zephyr_enabled=True,
            zephyr_base_url=f"https://user:{private_value}@jira.example.test",
            credential=token_hex(24),
            environ={},
        )

    assert private_value not in str(captured.value)


def test_runtime_snapshots_survive_reconfiguration(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_credential = token_hex(24)
    second_credential = token_hex(24)
    configure_dashboard_reporting_profile(
        settings,
        allure_enabled=False,
        zephyr_enabled=True,
        zephyr_base_url="https://first.example.test",
        credential=first_credential,
        environ={},
    )
    first = dashboard_reporting_runtime(settings, {})

    configure_dashboard_reporting_profile(
        settings,
        allure_enabled=True,
        zephyr_enabled=True,
        zephyr_base_url="https://second.example.test",
        credential=second_credential,
        environ={},
    )
    second = dashboard_reporting_runtime(settings, {})

    assert first.settings.zephyr_base_url == "https://first.example.test"
    assert first.environment[TOKEN_ENVIRONMENT_NAME] == first_credential
    assert second.settings.zephyr_base_url == "https://second.example.test"
    assert second.environment[TOKEN_ENVIRONMENT_NAME] == second_credential


def test_clear_returns_control_to_environment_settings(tmp_path: Path) -> None:
    environment_credential = token_hex(24)
    settings = _enabled_settings(tmp_path)
    configure_dashboard_reporting_profile(
        settings,
        allure_enabled=False,
        zephyr_enabled=False,
        environ={TOKEN_ENVIRONMENT_NAME: environment_credential},
    )

    clear_dashboard_reporting_profile()
    profile = load_dashboard_reporting_profile(
        settings,
        {TOKEN_ENVIRONMENT_NAME: environment_credential},
    )

    assert profile.allure_enabled is True
    assert profile.zephyr_enabled is True
    assert profile.credential_source == "environment"
    assert profile.session_configured is False
