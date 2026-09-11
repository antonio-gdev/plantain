"""Backend-only process profile for dashboard result configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Literal

from plantain.config import Settings
from plantain.errors import ConfigurationError
from plantain.reporting.zephyr import (
    TOKEN_ENVIRONMENT_NAME,
    validate_zephyr_configuration,
)

ReportingCredentialSource = Literal[
    "session",
    "environment",
    "not_required",
    "missing",
]


class DashboardReportingProfileError(ValueError):
    """Raised when dashboard result settings cannot be accepted safely."""


@dataclass(frozen=True, slots=True)
class DashboardReportingProfile:
    """Browser-safe status for the active result profile."""

    allure_enabled: bool
    zephyr_enabled: bool
    zephyr_base_url: str
    zephyr_attach_report: bool
    attachment_governance_approved: bool
    credential_source: ReportingCredentialSource
    session_configured: bool


@dataclass(frozen=True, slots=True)
class DashboardReportingRuntime:
    """One immutable backend snapshot used to construct a dashboard runner."""

    settings: Settings
    profile: DashboardReportingProfile
    environment: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _SessionProfile:
    allure_enabled: bool
    zephyr_enabled: bool
    zephyr_base_url: str
    zephyr_attach_report: bool
    attachment_governance_approved: bool
    credential: str | None = field(repr=False)

    def overrides(self) -> dict[str, str]:
        if self.credential is None:
            return {}
        return {TOKEN_ENVIRONMENT_NAME: self.credential}


class _ReportingEnvironment(Mapping[str, str]):
    """Read-only environment overlay with a value-free representation."""

    __slots__ = ("_fallback", "_overrides")

    def __init__(
        self,
        profile: _SessionProfile | None,
        fallback: Mapping[str, str],
    ) -> None:
        self._fallback = fallback
        self._overrides = profile.overrides() if profile is not None else {}

    def __getitem__(self, name: str) -> str:
        if name in self._overrides:
            return self._overrides[name]
        return self._fallback[name]

    def __iter__(self) -> Iterator[str]:
        yielded = set(self._overrides)
        yield from self._overrides
        yield from (name for name in self._fallback if name not in yielded)

    def __len__(self) -> int:
        return len(set(self._overrides).union(self._fallback))


class _ReportingProfileStore:
    __slots__ = ("_lock", "_profile")

    def __init__(self) -> None:
        self._lock = RLock()
        self._profile: _SessionProfile | None = None

    def runtime(
        self,
        settings: Settings,
        fallback: Mapping[str, str],
    ) -> DashboardReportingRuntime:
        with self._lock:
            return _build_runtime(settings, self._profile, fallback)

    def configure(
        self,
        settings: Settings,
        *,
        allure_enabled: bool,
        zephyr_enabled: bool,
        zephyr_base_url: str,
        zephyr_attach_report: bool,
        attachment_governance_approved: bool,
        credential: str,
        fallback: Mapping[str, str],
    ) -> DashboardReportingProfile:
        with self._lock:
            candidate = _candidate_profile(
                allure_enabled=allure_enabled,
                zephyr_enabled=zephyr_enabled,
                zephyr_base_url=zephyr_base_url,
                zephyr_attach_report=zephyr_attach_report,
                attachment_governance_approved=attachment_governance_approved,
                credential=credential,
                current=self._profile,
            )
            try:
                runtime = _build_runtime(settings, candidate, fallback)
            except ConfigurationError as exc:
                raise DashboardReportingProfileError(
                    "Review the enabled result destinations and required fields."
                ) from exc
            self._profile = candidate
            return runtime.profile

    def clear(self) -> None:
        with self._lock:
            self._profile = None


_PROFILE_STORE = _ReportingProfileStore()


def dashboard_reporting_runtime(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> DashboardReportingRuntime:
    """Return one stable backend-only result configuration snapshot."""

    return _PROFILE_STORE.runtime(
        settings,
        os.environ if environ is None else environ,
    )


def load_dashboard_reporting_profile(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> DashboardReportingProfile:
    """Return only browser-safe result configuration metadata."""

    return dashboard_reporting_runtime(settings, environ).profile


def configure_dashboard_reporting_profile(
    settings: Settings,
    *,
    allure_enabled: bool,
    zephyr_enabled: bool,
    zephyr_base_url: str = "",
    zephyr_attach_report: bool = False,
    attachment_governance_approved: bool = False,
    credential: str = "",
    environ: Mapping[str, str] | None = None,
) -> DashboardReportingProfile:
    """Install validated process-lifetime result settings without persistence."""

    return _PROFILE_STORE.configure(
        settings,
        allure_enabled=allure_enabled,
        zephyr_enabled=zephyr_enabled,
        zephyr_base_url=zephyr_base_url,
        zephyr_attach_report=zephyr_attach_report,
        attachment_governance_approved=attachment_governance_approved,
        credential=credential,
        fallback=os.environ if environ is None else environ,
    )


def clear_dashboard_reporting_profile() -> None:
    """Discard GUI-entered result settings and return to environment discovery."""

    _PROFILE_STORE.clear()


def _candidate_profile(
    *,
    allure_enabled: bool,
    zephyr_enabled: bool,
    zephyr_base_url: str,
    zephyr_attach_report: bool,
    attachment_governance_approved: bool,
    credential: str,
    current: _SessionProfile | None,
) -> _SessionProfile:
    retained = (
        current.credential
        if current is not None and current.zephyr_enabled and not credential.strip()
        else None
    )
    active_credential = credential if credential.strip() else retained
    return _SessionProfile(
        allure_enabled=allure_enabled,
        zephyr_enabled=zephyr_enabled,
        zephyr_base_url=zephyr_base_url.strip() if zephyr_enabled else "",
        zephyr_attach_report=zephyr_enabled and zephyr_attach_report,
        attachment_governance_approved=(
            zephyr_enabled and zephyr_attach_report and attachment_governance_approved
        ),
        credential=active_credential if zephyr_enabled else None,
    )


def _build_runtime(
    settings: Settings,
    profile: _SessionProfile | None,
    fallback: Mapping[str, str],
) -> DashboardReportingRuntime:
    environment = _ReportingEnvironment(profile, fallback)
    effective = _effective_settings(settings, profile)
    validate_zephyr_configuration(effective, environ=environment)
    return DashboardReportingRuntime(
        settings=effective,
        profile=_profile_status(effective, profile, environment),
        environment=environment,
    )


def _effective_settings(
    settings: Settings,
    profile: _SessionProfile | None,
) -> Settings:
    if profile is None:
        return settings
    return replace(
        settings,
        allure_results_enabled=profile.allure_enabled,
        zephyr_base_url=profile.zephyr_base_url,
        zephyr_publish_results=profile.zephyr_enabled,
        zephyr_attach_report=profile.zephyr_attach_report,
        zephyr_attachment_data_governance_approved=(profile.attachment_governance_approved),
    )


def _profile_status(
    settings: Settings,
    profile: _SessionProfile | None,
    environment: Mapping[str, str],
) -> DashboardReportingProfile:
    zephyr_enabled = settings.zephyr_publish_results
    if not zephyr_enabled:
        credential_source: ReportingCredentialSource = "not_required"
    elif profile is not None and profile.credential is not None:
        credential_source = "session"
    elif environment.get(TOKEN_ENVIRONMENT_NAME):
        credential_source = "environment"
    else:
        credential_source = "missing"
    return DashboardReportingProfile(
        allure_enabled=settings.allure_results_enabled,
        zephyr_enabled=zephyr_enabled,
        zephyr_base_url=settings.zephyr_base_url if zephyr_enabled else "",
        zephyr_attach_report=settings.zephyr_attach_report,
        attachment_governance_approved=(settings.zephyr_attachment_data_governance_approved),
        credential_source=credential_source,
        session_configured=profile is not None,
    )


__all__ = [
    "DashboardReportingProfile",
    "DashboardReportingProfileError",
    "DashboardReportingRuntime",
    "ReportingCredentialSource",
    "clear_dashboard_reporting_profile",
    "configure_dashboard_reporting_profile",
    "dashboard_reporting_runtime",
    "load_dashboard_reporting_profile",
]
