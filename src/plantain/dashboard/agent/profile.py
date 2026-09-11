"""Backend-only process profile for dashboard agent configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Literal

from plantain.dashboard.agent.connection import (
    AGENT_BASE_URL_ENV,
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    PROVIDER_SPECS,
    AgentConfigurationError,
    AgentConnection,
    AgentProvider,
    load_agent_connection,
)

MAX_AGENT_CREDENTIAL_BYTES = 16_384
_ASCII_CONTROL_END = 32
_ASCII_DELETE = 127
AgentCredentialSource = Literal["session", "environment", "not_required", "missing"]


class DashboardAgentProfileError(ValueError):
    """Raised when a local dashboard agent profile cannot be accepted safely."""


@dataclass(frozen=True, slots=True)
class DashboardAgentProfile:
    """Browser-safe status for the active backend profile."""

    connection: AgentConnection
    credential_source: AgentCredentialSource
    session_configured: bool


@dataclass(frozen=True, slots=True)
class _SessionProfile:
    provider: AgentProvider
    model: str
    base_url: str
    credential_environment: str | None
    credential: str | None = field(repr=False)

    def overrides(self) -> dict[str, str]:
        values = {
            AGENT_PROVIDER_ENV: self.provider.value,
            AGENT_MODEL_ENV: self.model,
        }
        if self.provider is AgentProvider.CUSTOM:
            values[AGENT_BASE_URL_ENV] = self.base_url
        if self.credential_environment is not None and self.credential is not None:
            values[self.credential_environment] = self.credential
        return values


class _AgentEnvironment(Mapping[str, str]):
    """Read-only overlay that does not reveal values through its representation."""

    __slots__ = ("_fallback", "_overrides")

    def __init__(
        self,
        profile: _SessionProfile | None,
        fallback: Mapping[str, str],
    ) -> None:
        self._overrides = profile.overrides() if profile is not None else {}
        self._fallback = fallback

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


class _AgentProfileStore:
    __slots__ = ("_lock", "_profile")

    def __init__(self) -> None:
        self._lock = RLock()
        self._profile: _SessionProfile | None = None

    def environment(self, fallback: Mapping[str, str]) -> Mapping[str, str]:
        with self._lock:
            return _AgentEnvironment(self._profile, fallback)

    def status(self, fallback: Mapping[str, str]) -> DashboardAgentProfile:
        with self._lock:
            profile = self._profile
            connection = load_agent_connection(_AgentEnvironment(profile, fallback))
            return _profile_status(connection, profile)

    def configure(
        self,
        provider: str,
        model: str,
        base_url: str,
        credential: str,
        fallback: Mapping[str, str],
    ) -> DashboardAgentProfile:
        with self._lock:
            candidate = _candidate_profile(
                provider,
                model,
                base_url,
                credential,
                self._profile,
            )
            environment = _AgentEnvironment(candidate, fallback)
            try:
                connection = load_agent_connection(environment)
            except AgentConfigurationError as exc:
                raise DashboardAgentProfileError(
                    "Review the provider, model, endpoint, and API key."
                ) from exc
            if not connection.ready:
                raise DashboardAgentProfileError(
                    "Complete the required provider, model, endpoint, and API key fields."
                )
            self._profile = candidate
            return _profile_status(connection, candidate)

    def clear(self) -> None:
        with self._lock:
            self._profile = None


_PROFILE_STORE = _AgentProfileStore()


def dashboard_agent_environment(
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, str]:
    """Return a backend-only configuration overlay for one provider operation."""

    return _PROFILE_STORE.environment(os.environ if environ is None else environ)


def load_dashboard_agent_profile(
    environ: Mapping[str, str] | None = None,
) -> DashboardAgentProfile:
    """Return only browser-safe readiness metadata."""

    return _PROFILE_STORE.status(os.environ if environ is None else environ)


def configure_dashboard_agent_profile(
    *,
    provider: str,
    model: str,
    base_url: str = "",
    credential: str = "",
    environ: Mapping[str, str] | None = None,
) -> DashboardAgentProfile:
    """Install a validated process-lifetime profile without persistent secret storage."""

    return _PROFILE_STORE.configure(
        provider,
        model,
        base_url,
        credential,
        os.environ if environ is None else environ,
    )


def clear_dashboard_agent_profile() -> None:
    """Discard the process-lifetime profile and return to environment discovery."""

    _PROFILE_STORE.clear()


def _candidate_profile(
    provider_value: str,
    model: str,
    base_url: str,
    credential: str,
    current: _SessionProfile | None,
) -> _SessionProfile:
    provider = _provider(provider_value)
    spec = PROVIDER_SPECS[provider]
    credential_environment = (
        spec.credential_environments[0] if spec.credential_environments else None
    )
    retained = (
        current.credential
        if current is not None and current.provider is provider and not credential.strip()
        else None
    )
    normalized_credential = _credential(credential) if credential.strip() else retained
    return _SessionProfile(
        provider=provider,
        model=model.strip(),
        base_url=base_url.strip() if provider is AgentProvider.CUSTOM else "",
        credential_environment=credential_environment,
        credential=normalized_credential,
    )


def _provider(value: str) -> AgentProvider:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    try:
        return AgentProvider(normalized)
    except ValueError as exc:
        raise DashboardAgentProfileError("Choose a supported agent provider.") from exc


def _credential(value: str) -> str:
    normalized = value.strip()
    try:
        encoded = normalized.encode()
    except UnicodeEncodeError as exc:
        raise DashboardAgentProfileError("The API key must be valid UTF-8 text.") from exc
    if len(encoded) > MAX_AGENT_CREDENTIAL_BYTES:
        raise DashboardAgentProfileError("The API key exceeds its safe size limit.")
    if any(
        ord(character) < _ASCII_CONTROL_END or ord(character) == _ASCII_DELETE
        for character in normalized
    ):
        raise DashboardAgentProfileError("The API key contains unsupported control characters.")
    return normalized


def _profile_status(
    connection: AgentConnection,
    profile: _SessionProfile | None,
) -> DashboardAgentProfile:
    source: AgentCredentialSource
    if connection.credential_environment is None:
        source = (
            "missing"
            if connection.provider is not None
            and PROVIDER_SPECS[connection.provider].credential_required
            else "not_required"
        )
    elif (
        profile is not None
        and profile.credential is not None
        and profile.credential_environment == connection.credential_environment
    ):
        source = "session"
    else:
        source = "environment"
    return DashboardAgentProfile(
        connection=connection,
        credential_source=source,
        session_configured=profile is not None,
    )


__all__ = [
    "MAX_AGENT_CREDENTIAL_BYTES",
    "AgentCredentialSource",
    "DashboardAgentProfile",
    "DashboardAgentProfileError",
    "clear_dashboard_agent_profile",
    "configure_dashboard_agent_profile",
    "dashboard_agent_environment",
    "load_dashboard_agent_profile",
]
