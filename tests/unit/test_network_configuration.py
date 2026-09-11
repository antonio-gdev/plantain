"""Fail-closed outbound network configuration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from plantain.config import Settings
from plantain.errors import ConfigurationError

NETWORK_SETTING_NAMES = (
    "PLANTAIN_API_ALLOWED_METHODS",
    "PLANTAIN_ALLOWED_HOSTS",
    "PLANTAIN_BLOCKED_HOSTS",
    "PLANTAIN_NETWORK_MODE",
    "PLANTAIN_ALLOW_PRIVATE_NETWORKS",
    "PLANTAIN_ALLOW_INSECURE_LOCAL_HTTP",
    "PLANTAIN_EGRESS_CONTROL_ENFORCED",
    "PLANTAIN_DB_ALLOWED_TARGETS",
    "PLANTAIN_DB_ALLOW_PRIVATE_NETWORKS",
)


def _clear_network_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NETWORK_SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_network_configuration_defaults_use_public_safe_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_network_settings(monkeypatch)

    settings = Settings.from_env(tmp_path)

    assert settings.allowed_hosts == ()
    assert settings.blocked_hosts == ()
    assert settings.api_allowed_methods == ()
    assert settings.network_mode == "standard"
    assert settings.allow_private_networks is False
    assert settings.allow_insecure_local_http is False
    assert settings.egress_control_enforced is False
    assert settings.db_allowed_targets == ()
    assert settings.db_allow_private_networks is False


def test_network_configuration_normalizes_explicit_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_network_settings(monkeypatch)
    monkeypatch.setenv(
        "PLANTAIN_ALLOWED_HOSTS",
        "API.Example.Test., *.services.example.test,api.example.test",
    )
    monkeypatch.setenv("PLANTAIN_BLOCKED_HOSTS", "*.ads.example.test,Tracker.Example.Test.")
    monkeypatch.setenv("PLANTAIN_API_ALLOWED_METHODS", "post, GET,POST")
    monkeypatch.setenv("PLANTAIN_NETWORK_MODE", "restricted")
    monkeypatch.setenv("PLANTAIN_ALLOW_PRIVATE_NETWORKS", "true")
    monkeypatch.setenv("PLANTAIN_ALLOW_INSECURE_LOCAL_HTTP", "true")
    monkeypatch.setenv("PLANTAIN_EGRESS_CONTROL_ENFORCED", "true")
    monkeypatch.setenv(
        "PLANTAIN_DB_ALLOWED_TARGETS",
        "DB.Example.Test.:5432, [2001:db8::1]:1521",
    )
    monkeypatch.setenv("PLANTAIN_DB_ALLOW_PRIVATE_NETWORKS", "true")

    settings = Settings.from_env(tmp_path)

    assert settings.allowed_hosts == ("*.services.example.test", "api.example.test")
    assert settings.blocked_hosts == ("*.ads.example.test", "tracker.example.test")
    assert settings.api_allowed_methods == ("GET", "POST")
    assert settings.network_mode == "restricted"
    assert settings.allow_private_networks is True
    assert settings.allow_insecure_local_http is True
    assert settings.egress_control_enforced is True
    assert settings.db_allowed_targets == (
        "[2001:db8::1]:1521",
        "db.example.test:5432",
    )
    assert settings.db_allow_private_networks is True


def test_network_configuration_rejects_unknown_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_network_settings(monkeypatch)
    monkeypatch.setenv("PLANTAIN_NETWORK_MODE", "permissive")

    with pytest.raises(ConfigurationError, match=r"standard.*restricted"):
        Settings.from_env(tmp_path)


def test_network_configuration_rejects_unknown_api_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_network_settings(monkeypatch)
    monkeypatch.setenv("PLANTAIN_API_ALLOWED_METHODS", "GET,TRACE")

    with pytest.raises(ConfigurationError, match="unsupported HTTP method"):
        Settings.from_env(tmp_path)


@pytest.mark.parametrize(
    "value",
    (
        "*",
        "example.*",
        "*.127.0.0.1",
        "https://example.test",
        "example..test",
    ),
)
def test_network_configuration_rejects_malformed_host_rules(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_network_settings(monkeypatch)
    monkeypatch.setenv("PLANTAIN_ALLOWED_HOSTS", value)

    with pytest.raises(ConfigurationError):
        Settings.from_env(tmp_path)


@pytest.mark.parametrize(
    "value",
    (
        "db.example.test",
        "*.example.test:5432",
        "db.example.test:0",
        "2001:db8::1:5432",
    ),
)
def test_network_configuration_rejects_malformed_database_targets(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_network_settings(monkeypatch)
    monkeypatch.setenv("PLANTAIN_DB_ALLOWED_TARGETS", value)

    with pytest.raises(ConfigurationError):
        Settings.from_env(tmp_path)
