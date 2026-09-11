"""Dashboard agent profiles remain backend-only, bounded, and environment-compatible."""

from __future__ import annotations

from collections.abc import Iterator
from secrets import token_hex

import pytest

from plantain.dashboard.agent.connection import (
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    AgentProvider,
)
from plantain.dashboard.agent.profile import (
    MAX_AGENT_CREDENTIAL_BYTES,
    DashboardAgentProfileError,
    clear_dashboard_agent_profile,
    configure_dashboard_agent_profile,
    dashboard_agent_environment,
    load_dashboard_agent_profile,
)


@pytest.fixture(autouse=True)
def _clear_process_profile() -> Iterator[None]:
    clear_dashboard_agent_profile()
    yield
    clear_dashboard_agent_profile()


def test_environment_configuration_remains_the_fallback() -> None:
    credential = token_hex(24)
    profile = load_dashboard_agent_profile(
        {
            AGENT_MODEL_ENV: "gpt-test",
            "OPENAI_API_KEY": credential,
        }
    )

    assert profile.connection.ready is True
    assert profile.connection.provider is AgentProvider.OPENAI
    assert profile.credential_source == "environment"
    assert profile.session_configured is False
    assert credential not in repr(profile)


def test_session_profile_overlays_without_mutating_environment() -> None:
    credential = token_hex(24)
    fallback: dict[str, str] = {}

    profile = configure_dashboard_agent_profile(
        provider="openai",
        model="gpt-test",
        credential=credential,
        environ=fallback,
    )
    environment = dashboard_agent_environment(fallback)

    assert profile.connection.ready is True
    assert profile.credential_source == "session"
    assert profile.session_configured is True
    assert environment[AGENT_PROVIDER_ENV] == "openai"
    assert environment[AGENT_MODEL_ENV] == "gpt-test"
    assert environment["OPENAI_API_KEY"] == credential
    assert fallback == {}
    assert credential not in repr(profile)
    assert credential not in repr(environment)


def test_blank_key_retains_same_provider_session_credential() -> None:
    credential = token_hex(24)
    configure_dashboard_agent_profile(
        provider="anthropic",
        model="first-model",
        credential=credential,
        environ={},
    )

    profile = configure_dashboard_agent_profile(
        provider="anthropic",
        model="second-model",
        credential="",
        environ={},
    )

    assert profile.connection.model == "second-model"
    assert dashboard_agent_environment({})["ANTHROPIC_API_KEY"] == credential


def test_switching_to_local_provider_drops_session_credential() -> None:
    configure_dashboard_agent_profile(
        provider="openai",
        model="gpt-test",
        credential=token_hex(24),
        environ={},
    )

    profile = configure_dashboard_agent_profile(
        provider="ollama",
        model="local/model",
        environ={},
    )
    environment = dashboard_agent_environment({})

    assert profile.connection.provider is AgentProvider.OLLAMA
    assert profile.credential_source == "not_required"
    assert "OPENAI_API_KEY" not in environment


def test_custom_provider_accepts_optional_bearer_key() -> None:
    profile = configure_dashboard_agent_profile(
        provider="custom",
        model="private-model",
        base_url="https://agent.example.test/v1",
        environ={},
    )

    assert profile.connection.ready is True
    assert profile.connection.base_url == "https://agent.example.test/v1"
    assert profile.credential_source == "not_required"


def test_invalid_inputs_never_echo_their_values() -> None:
    invalid_provider = token_hex(16)
    with pytest.raises(DashboardAgentProfileError) as provider_error:
        configure_dashboard_agent_profile(
            provider=invalid_provider,
            model="model",
            environ={},
        )
    assert invalid_provider not in str(provider_error.value)

    with pytest.raises(DashboardAgentProfileError, match="size limit"):
        configure_dashboard_agent_profile(
            provider="openai",
            model="model",
            credential="x" * (MAX_AGENT_CREDENTIAL_BYTES + 1),
            environ={},
        )
    with pytest.raises(DashboardAgentProfileError, match="control characters"):
        configure_dashboard_agent_profile(
            provider="openai",
            model="model",
            credential="first\nsecond",
            environ={},
        )


def test_environment_snapshots_are_stable_across_reconfiguration() -> None:
    first_credential = token_hex(24)
    second_credential = token_hex(24)
    configure_dashboard_agent_profile(
        provider="openai",
        model="first-model",
        credential=first_credential,
        environ={},
    )
    first_environment = dashboard_agent_environment({})

    configure_dashboard_agent_profile(
        provider="openai",
        model="second-model",
        credential=second_credential,
        environ={},
    )
    second_environment = dashboard_agent_environment({})

    assert first_environment["OPENAI_API_KEY"] == first_credential
    assert first_environment[AGENT_MODEL_ENV] == "first-model"
    assert second_environment["OPENAI_API_KEY"] == second_credential
    assert second_environment[AGENT_MODEL_ENV] == "second-model"


def test_clear_returns_control_to_environment_configuration() -> None:
    fallback = {
        AGENT_MODEL_ENV: "environment-model",
        "OPENAI_API_KEY": token_hex(24),
    }
    configure_dashboard_agent_profile(
        provider="ollama",
        model="local-model",
        environ=fallback,
    )

    clear_dashboard_agent_profile()
    profile = load_dashboard_agent_profile(fallback)

    assert profile.connection.provider is AgentProvider.OPENAI
    assert profile.connection.model == "environment-model"
    assert profile.credential_source == "environment"
