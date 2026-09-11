"""Browser provisioning environment configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from plantain.config import Settings

SETTING_NAMES = (
    "PLANTAIN_BROWSER_AUTO_INSTALL",
    "PLANTAIN_BROWSER_INSTALL_TIMEOUT_SECONDS",
    "PLANTAIN_ALLOW_CUSTOM_PLAYWRIGHT_DOWNLOAD_HOSTS",
)
DEFAULT_INSTALL_TIMEOUT_SECONDS = 300.0
CUSTOM_INSTALL_TIMEOUT_SECONDS = 45.0


def test_browser_provisioning_defaults_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env(tmp_path)

    assert settings.browser_auto_install is True
    assert settings.browser_install_timeout_seconds == DEFAULT_INSTALL_TIMEOUT_SECONDS
    assert settings.allow_custom_playwright_download_hosts is False


def test_browser_provisioning_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANTAIN_BROWSER_AUTO_INSTALL", "false")
    monkeypatch.setenv("PLANTAIN_BROWSER_INSTALL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("PLANTAIN_ALLOW_CUSTOM_PLAYWRIGHT_DOWNLOAD_HOSTS", "true")

    settings = Settings.from_env(tmp_path)

    assert settings.browser_auto_install is False
    assert settings.browser_install_timeout_seconds == CUSTOM_INSTALL_TIMEOUT_SECONDS
    assert settings.allow_custom_playwright_download_hosts is True
