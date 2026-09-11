"""Bounded provider transport for dashboard agent decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any, NoReturn, TypeVar

from pydantic import BaseModel, ValidationError

from plantain.activities.api.client import ApiActivityError, ApiSession, BoundedResponse
from plantain.dashboard.agent.connection import (
    AgentConnection,
    AgentProtocol,
    AgentProvider,
)
from plantain.dashboard.agent.models import (
    MAX_AGENT_FINISH_REASON_LENGTH,
    AgentResponseMetadata,
    AgentTokenUsage,
)
from plantain.dashboard.agent.profile import dashboard_agent_environment
from plantain.dashboard.agent_usage_types import AgentUsageObserver, AgentUsageOperation
from plantain.errors import ConfigurationError, PlantainError
from plantain.models.api import HttpMethod
from plantain.security.secrets import SecretRegistry

AGENT_REQUEST_TIMEOUT_SECONDS = 120.0
MAX_AGENT_RESPONSE_BYTES = 4_194_304
MAX_AGENT_SYSTEM_BYTES = 131_072
MAX_AGENT_PROMPT_BYTES = 1_048_576
MAX_AGENT_OUTPUT_BYTES = 2_097_152
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
MAX_AGENT_OUTPUT_TOKENS = 65_536
HTTP_SUCCESS_MINIMUM = 200
HTTP_SUCCESS_MAXIMUM_EXCLUSIVE = 300
ANTHROPIC_API_VERSION = "2023-06-01"


def _distribution_version() -> str:
    try:
        return version("plantain-automation")
    except PackageNotFoundError:
        return "development"


GEMINI_CLIENT_IDENTIFIER = f"plantain-automation/{_distribution_version()}"
_JSON_MODE_PROVIDERS = frozenset(
    {
        AgentProvider.OPENAI,
        AgentProvider.DEEPSEEK,
        AgentProvider.GEMINI,
    }
)
ModelT = TypeVar("ModelT", bound=BaseModel)


class AgentTransportError(PlantainError):
    """Raised when a provider cannot produce a safe typed response."""


class AgentTransport:
    """Call one provider through an externally owned, policy-bound API session."""

    def __init__(
        self,
        api: ApiSession,
        secrets: SecretRegistry,
        usage_observer: AgentUsageObserver | None = None,
    ) -> None:
        self._api = api
        self._secrets = secrets
        self._usage_observer = usage_observer

    async def complete_json(
        self,
        connection: AgentConnection,
        *,
        operation: AgentUsageOperation,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[ModelT, AgentResponseMetadata]:
        """Request one JSON object and validate it before returning browser-safe data."""

        provider, protocol = _ready_protocol(connection)
        _validate_text(system_prompt, MAX_AGENT_SYSTEM_BYTES, "Agent system prompt")
        _validate_text(user_prompt, MAX_AGENT_PROMPT_BYTES, "Agent user prompt")
        _validate_output_tokens(max_output_tokens)
        schema = response_model.model_json_schema(by_alias=True)
        instructed_system = _schema_instruction(system_prompt, schema)
        _validate_text(instructed_system, MAX_AGENT_PROMPT_BYTES, "Agent schema prompt")
        credential = _resolve_credential(
            connection,
            dashboard_agent_environment() if environ is None else environ,
            self._secrets,
        )
        endpoint = _endpoint(connection.base_url, protocol)
        headers = _request_headers(provider, credential)
        body = _request_body(
            connection,
            protocol=protocol,
            system_prompt=instructed_system,
            user_prompt=user_prompt,
            schema=schema,
            max_output_tokens=max_output_tokens,
        )
        try:
            response = await self._api.request(
                method=HttpMethod.POST,
                url=endpoint,
                headers=headers,
                body=body,
                body_format="json",
                timeout_seconds=AGENT_REQUEST_TIMEOUT_SECONDS,
                max_response_bytes=MAX_AGENT_RESPONSE_BYTES,
            )
        except (ApiActivityError, ConfigurationError) as exc:
            raise AgentTransportError("Agent provider request failed safely") from exc
        content, finish_reason, usage = _completion(response, provider, protocol)
        value = _validated_result(content, response_model)
        metadata = AgentResponseMetadata(
            provider=provider,
            model=connection.model,
            finish_reason=finish_reason,
            usage=usage,
        )
        if self._usage_observer is not None:
            self._usage_observer(operation, metadata)
        return value, metadata


def _ready_protocol(
    connection: AgentConnection,
) -> tuple[AgentProvider, AgentProtocol]:
    provider = connection.provider
    protocol = connection.protocol
    if not connection.ready or provider is None or protocol is None:
        raise AgentTransportError("Agent provider setup is incomplete")
    return provider, protocol


def _validate_text(value: str, maximum: int, label: str) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise AgentTransportError(f"{label} must be valid UTF-8 text") from None
    if not value.strip():
        raise AgentTransportError(f"{label} cannot be empty")
    if size > maximum:
        raise AgentTransportError(f"{label} exceeds its byte limit")


def _validate_output_tokens(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_AGENT_OUTPUT_TOKENS
    ):
        raise AgentTransportError(
            f"Agent output tokens must be between 1 and {MAX_AGENT_OUTPUT_TOKENS}"
        )


def _schema_instruction(system_prompt: str, schema: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AgentTransportError("Agent response schema could not be encoded safely") from None
    return (
        f"{system_prompt.rstrip()}\n\n"
        "Return exactly one JSON object matching this JSON Schema. "
        "Do not include markdown fences or private reasoning.\n"
        f"{encoded}"
    )


def _resolve_credential(
    connection: AgentConnection,
    environ: Mapping[str, str],
    secrets: SecretRegistry,
) -> str | None:
    name = connection.credential_environment
    if name is None:
        return None
    value = environ.get(name)
    if value is None or not value.strip():
        raise AgentTransportError("The configured agent credential is unavailable")
    secrets.observe_environment(name, value)
    return value


def _endpoint(base_url: str, protocol: AgentProtocol) -> str:
    suffix = "chat/completions" if protocol is AgentProtocol.OPENAI_COMPATIBLE else "v1/messages"
    return f"{base_url.rstrip('/')}/{suffix}"


def _request_headers(
    provider: AgentProvider,
    credential: str | None,
) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if provider is AgentProvider.ANTHROPIC:
        if credential is None:
            raise AgentTransportError("The configured agent credential is unavailable")
        headers["anthropic-version"] = ANTHROPIC_API_VERSION
        headers["x-api-key"] = credential
        return headers
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    if provider is AgentProvider.GEMINI:
        headers["x-goog-api-client"] = GEMINI_CLIENT_IDENTIFIER
    return headers


def _request_body(
    connection: AgentConnection,
    *,
    protocol: AgentProtocol,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    if protocol is AgentProtocol.ANTHROPIC_MESSAGES:
        return {
            "model": connection.model,
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
            "stream": False,
        }
    body: dict[str, Any] = {
        "model": connection.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    if connection.provider in _JSON_MODE_PROVIDERS:
        body["response_format"] = {"type": "json_object"}
    return body


def _completion(
    response: BoundedResponse,
    provider: AgentProvider,
    protocol: AgentProtocol,
) -> tuple[str, str, AgentTokenUsage | None]:
    if (
        response.status_code < HTTP_SUCCESS_MINIMUM
        or response.status_code >= HTTP_SUCCESS_MAXIMUM_EXCLUSIVE
    ):
        raise AgentTransportError(
            f"{provider.value} agent provider returned HTTP {response.status_code}"
        )
    try:
        payload = response.decoded_body()
    except ApiActivityError as exc:
        raise AgentTransportError("Agent provider returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise AgentTransportError("Agent provider returned an invalid response")
    if protocol is AgentProtocol.ANTHROPIC_MESSAGES:
        return _anthropic_completion(payload)
    return _openai_completion(payload)


def _openai_completion(
    payload: dict[str, Any],
) -> tuple[str, str, AgentTokenUsage | None]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentTransportError("Agent provider returned no completion choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise AgentTransportError("Agent provider returned an invalid completion choice")
    finish_reason = _finish_reason(choice.get("finish_reason"))
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AgentTransportError("Agent provider returned an invalid completion message")
    if message.get("refusal"):
        raise AgentTransportError("Agent provider declined the request")
    content = message.get("content")
    if not isinstance(content, str):
        raise AgentTransportError("Agent provider returned no text completion")
    _validate_text(content, MAX_AGENT_OUTPUT_BYTES, "Agent provider output")
    return content, finish_reason, _openai_usage(payload.get("usage"))


def _anthropic_completion(
    payload: dict[str, Any],
) -> tuple[str, str, AgentTokenUsage | None]:
    finish_reason = _finish_reason(payload.get("stop_reason"))
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise AgentTransportError("Agent provider returned invalid content blocks")
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise AgentTransportError("Agent provider returned invalid content blocks")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise AgentTransportError("Agent provider returned an invalid text block")
            parts.append(text)
    content = "".join(parts)
    _validate_text(content, MAX_AGENT_OUTPUT_BYTES, "Agent provider output")
    return content, finish_reason, _anthropic_usage(payload.get("usage"))


def _finish_reason(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_AGENT_FINISH_REASON_LENGTH:
        raise AgentTransportError("Agent provider returned an invalid finish reason")
    if value in {"length", "max_tokens", "content_filter", "refusal"}:
        raise AgentTransportError("Agent provider did not complete the requested result")
    return value


def _openai_usage(value: Any) -> AgentTokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentTransportError("Agent provider returned invalid token usage")
    input_tokens = _usage_integer(value, "prompt_tokens")
    output_tokens = _usage_integer(value, "completion_tokens")
    total_tokens = _optional_usage_integer(value, "total_tokens")
    return _token_usage(
        input_tokens,
        output_tokens,
        input_tokens + output_tokens if total_tokens is None else total_tokens,
    )


def _anthropic_usage(value: Any) -> AgentTokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentTransportError("Agent provider returned invalid token usage")
    input_tokens = (
        _usage_integer(value, "input_tokens")
        + (_optional_usage_integer(value, "cache_creation_input_tokens", default=0) or 0)
        + (_optional_usage_integer(value, "cache_read_input_tokens", default=0) or 0)
    )
    output_tokens = _usage_integer(value, "output_tokens")
    return _token_usage(input_tokens, output_tokens, input_tokens + output_tokens)


def _token_usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> AgentTokenUsage:
    try:
        return AgentTokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    except ValidationError:
        raise AgentTransportError("Agent provider returned invalid token usage") from None


def _usage_integer(value: Mapping[str, Any], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise AgentTransportError("Agent provider returned invalid token usage")
    return item


def _optional_usage_integer(
    value: Mapping[str, Any],
    name: str,
    *,
    default: int | None = None,
) -> int | None:
    if name not in value:
        return default
    return _usage_integer(value, name)


def _validated_result(content: str, response_model: type[ModelT]) -> ModelT:
    try:
        value = json.loads(content, parse_constant=_reject_json_constant)
        return response_model.model_validate(value)
    except (ValueError, UnicodeError, RecursionError):
        raise AgentTransportError(
            "Agent provider output did not match the required result contract"
        ) from None


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("Non-finite JSON values are unsupported")


__all__ = ["AgentTransport", "AgentTransportError"]
