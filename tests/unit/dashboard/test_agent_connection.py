"""Agent connection discovery never exposes provider credentials."""

from __future__ import annotations

from dataclasses import asdict
from secrets import token_hex

import pytest

from plantain.dashboard.agent.connection import (
    AGENT_BASE_URL_ENV,
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    AgentConfigurationError,
    AgentConnectionState,
    AgentProvider,
    load_agent_connection,
)


def test_no_provider_requires_setup_without_guessing() -> None:
    connection = load_agent_connection({})

    assert connection.state is AgentConnectionState.SETUP_REQUIRED
    assert connection.provider is None
    assert connection.available_providers == ()


def test_one_hosted_provider_is_detected_without_retaining_key() -> None:
    credential = token_hex(24)
    connection = load_agent_connection(
        {
            "OPENAI_API_KEY": credential,
            AGENT_MODEL_ENV: "gpt-test",
        }
    )

    assert connection.ready is True
    assert connection.provider is AgentProvider.OPENAI
    assert connection.credential_environment == "OPENAI_API_KEY"
    assert credential not in repr(connection)
    assert credential not in repr(asdict(connection))


def test_multiple_detected_providers_require_a_choice() -> None:
    connection = load_agent_connection(
        {
            "OPENAI_API_KEY": token_hex(24),
            "ANTHROPIC_API_KEY": token_hex(24),
            AGENT_MODEL_ENV: "model-name",
        }
    )

    assert connection.state is AgentConnectionState.CHOICE_REQUIRED
    assert connection.provider is None
    assert connection.available_providers == ("OpenAI", "Anthropic")


def test_explicit_local_provider_needs_no_credential() -> None:
    connection = load_agent_connection(
        {
            AGENT_PROVIDER_ENV: "ollama",
            AGENT_MODEL_ENV: "local/model",
        }
    )

    assert connection.ready is True
    assert connection.provider is AgentProvider.OLLAMA
    assert connection.base_url == "http://127.0.0.1:11434/v1"
    assert connection.credential_environment is None


def test_whitespace_credential_is_not_treated_as_configured() -> None:
    connection = load_agent_connection(
        {
            "OPENAI_API_KEY": " \t ",
            AGENT_MODEL_ENV: "gpt-test",
        }
    )

    assert connection.state is AgentConnectionState.SETUP_REQUIRED
    assert connection.provider is None


def test_deepseek_uses_current_official_base_url() -> None:
    connection = load_agent_connection(
        {
            AGENT_PROVIDER_ENV: "deepseek",
            AGENT_MODEL_ENV: "deepseek-test",
            "DEEPSEEK_API_KEY": token_hex(24),
        }
    )

    assert connection.ready is True
    assert connection.base_url == "https://api.deepseek.com"


def test_custom_provider_requires_endpoint_and_validates_its_shape() -> None:
    incomplete = load_agent_connection(
        {
            AGENT_PROVIDER_ENV: "custom",
            AGENT_MODEL_ENV: "private-model",
        }
    )
    assert incomplete.state is AgentConnectionState.SETUP_REQUIRED

    with pytest.raises(AgentConfigurationError, match="without credentials"):
        load_agent_connection(
            {
                AGENT_PROVIDER_ENV: "custom",
                AGENT_MODEL_ENV: "private-model",
                AGENT_BASE_URL_ENV: "https://name:credential@example.test/v1",
            }
        )


def test_invalid_provider_and_model_errors_never_echo_values() -> None:
    invalid_provider = token_hex(16)
    with pytest.raises(AgentConfigurationError) as provider_error:
        load_agent_connection({AGENT_PROVIDER_ENV: invalid_provider})
    assert invalid_provider not in str(provider_error.value)

    invalid_model = f"model {token_hex(16)}"
    with pytest.raises(AgentConfigurationError) as model_error:
        load_agent_connection(
            {
                AGENT_PROVIDER_ENV: "ollama",
                AGENT_MODEL_ENV: invalid_model,
            }
        )
    assert invalid_model not in str(model_error.value)
