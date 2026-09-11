"""Provider transport is bounded, typed, and credential-safe."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from secrets import token_hex
from typing import Any, cast

import pytest

from plantain.activities.api.client import BoundedResponse
from plantain.dashboard.agent.connection import (
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    AgentProvider,
    load_agent_connection,
)
from plantain.dashboard.agent.profile import (
    clear_dashboard_agent_profile,
    configure_dashboard_agent_profile,
)
from plantain.dashboard.agent.transport import AgentTransport, AgentTransportError
from plantain.dashboard.agent_usage import AgentUsageOperation
from plantain.models.api import HttpMethod
from plantain.models.common import StrictModel
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
OPENAI_INPUT_TOKENS = 5
OPENAI_OUTPUT_TOKENS = 7
OPENAI_TOTAL_TOKENS = 12
ANTHROPIC_INPUT_TOKENS = 10
ANTHROPIC_CACHE_CREATE_TOKENS = 2
ANTHROPIC_CACHE_READ_TOKENS = 3
ANTHROPIC_OUTPUT_TOKENS = 4
ANTHROPIC_TOTAL_TOKENS = 19


class ExampleResult(StrictModel):
    """Small structured result used by the transport boundary."""

    answer: str


class FakeApi:
    """Capture one request and return a bounded synthetic response."""

    def __init__(self, response: BoundedResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> BoundedResponse:
        self.calls.append(kwargs)
        return self.response


def _response(payload: Any, *, status_code: int = HTTP_OK) -> BoundedResponse:
    return BoundedResponse(
        request_url="https://provider.example.test/v1/chat/completions",
        method=HttpMethod.POST,
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
        elapsed_ms=1,
    )


def _openai_payload(content: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": OPENAI_INPUT_TOKENS,
            "completion_tokens": OPENAI_OUTPUT_TOKENS,
            "total_tokens": OPENAI_TOTAL_TOKENS,
        },
    }


def _complete(
    api: FakeApi,
    connection: Any,
    secrets: SecretRegistry,
    environ: dict[str, str],
    usage_observer: Callable[[AgentUsageOperation, Any], None] | None = None,
) -> tuple[ExampleResult, Any]:
    transport = AgentTransport(cast("Any", api), secrets, usage_observer)
    return asyncio.run(
        transport.complete_json(
            connection,
            operation=AgentUsageOperation.INTENT_ROUTING,
            system_prompt="Route the request to one internal testing capability.",
            user_prompt="Check the checkout flow.",
            response_model=ExampleResult,
            environ=environ,
        )
    )


def test_openai_request_is_typed_bounded_and_secret_tainted() -> None:
    credential = token_hex(24)
    environ = {
        AGENT_PROVIDER_ENV: "openai",
        AGENT_MODEL_ENV: "gpt-test",
        "OPENAI_API_KEY": credential,
    }
    connection = load_agent_connection(environ)
    api = FakeApi(_response(_openai_payload('{"answer":"ready"}')))
    secrets = SecretRegistry()
    observed: list[tuple[AgentUsageOperation, Any]] = []

    result, metadata = _complete(
        api,
        connection,
        secrets,
        environ,
        lambda operation, item: observed.append((operation, item)),
    )

    assert result.answer == "ready"
    assert metadata.provider is AgentProvider.OPENAI
    assert metadata.usage is not None
    assert metadata.usage.input_tokens == OPENAI_INPUT_TOKENS
    assert metadata.usage.output_tokens == OPENAI_OUTPUT_TOKENS
    assert metadata.usage.total_tokens == OPENAI_TOTAL_TOKENS
    assert observed == [(AgentUsageOperation.INTENT_ROUTING, metadata)]
    assert secrets.redact_text(credential) == REDACTED
    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["method"] is HttpMethod.POST
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {credential}"
    assert call["max_response_bytes"] > 0
    body = call["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in body["messages"][0]["content"]
    assert "private reasoning" in body["messages"][0]["content"]


def test_default_transport_uses_backend_session_profile() -> None:
    credential = token_hex(24)
    profile = configure_dashboard_agent_profile(
        provider="openai",
        model="gpt-test",
        credential=credential,
        environ={},
    )
    api = FakeApi(_response(_openai_payload('{"answer":"ready"}')))
    secrets = SecretRegistry()
    transport = AgentTransport(cast("Any", api), secrets)

    try:
        result, _metadata = asyncio.run(
            transport.complete_json(
                profile.connection,
                operation=AgentUsageOperation.CONNECTION_CHECK,
                system_prompt="Route the request to one internal testing capability.",
                user_prompt="Check the checkout flow.",
                response_model=ExampleResult,
            )
        )
    finally:
        clear_dashboard_agent_profile()

    assert result.answer == "ready"
    assert api.calls[0]["headers"]["Authorization"] == f"Bearer {credential}"
    assert secrets.redact_text(credential) == REDACTED


def test_anthropic_request_uses_native_contract_and_exact_usage() -> None:
    credential = token_hex(24)
    environ = {
        AGENT_PROVIDER_ENV: "anthropic",
        AGENT_MODEL_ENV: "claude-test",
        "ANTHROPIC_API_KEY": credential,
    }
    connection = load_agent_connection(environ)
    payload = {
        "content": [
            {"type": "thinking", "thinking": "not retained"},
            {"type": "text", "text": '{"answer":"verified"}'},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": ANTHROPIC_INPUT_TOKENS,
            "cache_creation_input_tokens": ANTHROPIC_CACHE_CREATE_TOKENS,
            "cache_read_input_tokens": ANTHROPIC_CACHE_READ_TOKENS,
            "output_tokens": ANTHROPIC_OUTPUT_TOKENS,
        },
    }
    api = FakeApi(_response(payload))
    secrets = SecretRegistry()

    result, metadata = _complete(api, connection, secrets, environ)

    assert result.answer == "verified"
    assert metadata.provider is AgentProvider.ANTHROPIC
    assert metadata.usage is not None
    assert metadata.usage.input_tokens == (
        ANTHROPIC_INPUT_TOKENS + ANTHROPIC_CACHE_CREATE_TOKENS + ANTHROPIC_CACHE_READ_TOKENS
    )
    assert metadata.usage.total_tokens == ANTHROPIC_TOTAL_TOKENS
    assert "not retained" not in repr((result, metadata))
    call = api.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == credential
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["body"]["output_config"]["format"]["type"] == "json_schema"


def test_local_provider_requires_neither_key_nor_optional_json_mode() -> None:
    environ = {
        AGENT_PROVIDER_ENV: "ollama",
        AGENT_MODEL_ENV: "local-model",
    }
    connection = load_agent_connection(environ)
    api = FakeApi(_response(_openai_payload('{"answer":"local"}')))

    result, metadata = _complete(api, connection, SecretRegistry(), environ)

    assert result.answer == "local"
    assert metadata.provider is AgentProvider.OLLAMA
    call = api.calls[0]
    assert call["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert call["headers"] == {"Accept": "application/json"}
    assert "response_format" not in call["body"]


def test_http_failure_never_exposes_provider_body_or_credential() -> None:
    credential = token_hex(24)
    environ = {
        AGENT_PROVIDER_ENV: "openai",
        AGENT_MODEL_ENV: "gpt-test",
        "OPENAI_API_KEY": credential,
    }
    connection = load_agent_connection(environ)
    api = FakeApi(
        _response(
            {"error": {"message": credential}},
            status_code=HTTP_UNAUTHORIZED,
        )
    )

    with pytest.raises(AgentTransportError) as captured:
        _complete(api, connection, SecretRegistry(), environ)

    assert credential not in str(captured.value)
    assert str(HTTP_UNAUTHORIZED) in str(captured.value)


def test_invalid_structured_output_is_value_free_and_has_no_cause() -> None:
    rejected_value = token_hex(24)
    environ = {
        AGENT_PROVIDER_ENV: "ollama",
        AGENT_MODEL_ENV: "local-model",
    }
    connection = load_agent_connection(environ)
    content = json.dumps({"answer": rejected_value, "unexpected": rejected_value})
    api = FakeApi(_response(_openai_payload(content)))

    with pytest.raises(AgentTransportError) as captured:
        _complete(api, connection, SecretRegistry(), environ)

    assert rejected_value not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "finish_reason",
    ["length", "max_tokens", "content_filter", "refusal"],
)
def test_incomplete_or_declined_results_are_not_accepted(finish_reason: str) -> None:
    environ = {
        AGENT_PROVIDER_ENV: "ollama",
        AGENT_MODEL_ENV: "local-model",
    }
    connection = load_agent_connection(environ)
    payload = _openai_payload('{"answer":"partial"}')
    payload["choices"][0]["finish_reason"] = finish_reason
    api = FakeApi(_response(payload))

    with pytest.raises(AgentTransportError, match="did not complete"):
        _complete(api, connection, SecretRegistry(), environ)


def test_usage_must_be_non_negative_and_internally_consistent() -> None:
    environ = {
        AGENT_PROVIDER_ENV: "ollama",
        AGENT_MODEL_ENV: "local-model",
    }
    connection = load_agent_connection(environ)
    payload = _openai_payload('{"answer":"invalid usage"}')
    payload["usage"]["total_tokens"] = 1
    api = FakeApi(_response(payload))

    with pytest.raises(AgentTransportError, match="invalid token usage"):
        _complete(api, connection, SecretRegistry(), environ)
