"""Safe agent-provider discovery without retaining credential values."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

AGENT_PROVIDER_ENV = "PLANTAIN_AGENT_PROVIDER"
AGENT_MODEL_ENV = "PLANTAIN_AGENT_MODEL"
AGENT_BASE_URL_ENV = "PLANTAIN_AGENT_BASE_URL"
CUSTOM_AGENT_KEY_ENV = "PLANTAIN_AGENT_API_KEY"
MAX_MODEL_NAME_LENGTH = 160
MAX_AGENT_BASE_URL_LENGTH = 2_048
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class AgentConfigurationError(ValueError):
    """Raised when non-secret agent configuration is invalid."""


class AgentProvider(StrEnum):
    """Supported hosted and local model-provider families."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    LM_STUDIO = "lm_studio"
    CUSTOM = "custom"


class AgentProtocol(StrEnum):
    """Small wire-protocol set implemented by the dashboard transport."""

    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class AgentConnectionState(StrEnum):
    """Safe connection readiness exposed to the browser."""

    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    CHOICE_REQUIRED = "choice_required"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Static provider metadata that never contains credentials."""

    provider: AgentProvider
    display_name: str
    protocol: AgentProtocol
    base_url: str
    credential_environments: tuple[str, ...]
    credential_required: bool
    auto_detect: bool


@dataclass(frozen=True, slots=True)
class AgentConnection:
    """Resolved non-secret connection metadata."""

    state: AgentConnectionState
    provider: AgentProvider | None
    provider_name: str
    protocol: AgentProtocol | None
    model: str
    base_url: str
    credential_environment: str | None
    available_providers: tuple[str, ...]
    message: str

    @property
    def ready(self) -> bool:
        """Return whether the backend has enough configuration to connect."""

        return self.state is AgentConnectionState.READY


PROVIDER_SPECS = {
    AgentProvider.OPENAI: ProviderSpec(
        provider=AgentProvider.OPENAI,
        display_name="OpenAI",
        protocol=AgentProtocol.OPENAI_COMPATIBLE,
        base_url="https://api.openai.com/v1",
        credential_environments=("OPENAI_API_KEY",),
        credential_required=True,
        auto_detect=True,
    ),
    AgentProvider.ANTHROPIC: ProviderSpec(
        provider=AgentProvider.ANTHROPIC,
        display_name="Anthropic",
        protocol=AgentProtocol.ANTHROPIC_MESSAGES,
        base_url="https://api.anthropic.com",
        credential_environments=("ANTHROPIC_API_KEY",),
        credential_required=True,
        auto_detect=True,
    ),
    AgentProvider.DEEPSEEK: ProviderSpec(
        provider=AgentProvider.DEEPSEEK,
        display_name="DeepSeek",
        protocol=AgentProtocol.OPENAI_COMPATIBLE,
        base_url="https://api.deepseek.com",
        credential_environments=("DEEPSEEK_API_KEY",),
        credential_required=True,
        auto_detect=True,
    ),
    AgentProvider.GEMINI: ProviderSpec(
        provider=AgentProvider.GEMINI,
        display_name="Gemini",
        protocol=AgentProtocol.OPENAI_COMPATIBLE,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        credential_environments=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        credential_required=True,
        auto_detect=True,
    ),
    AgentProvider.OLLAMA: ProviderSpec(
        provider=AgentProvider.OLLAMA,
        display_name="Ollama",
        protocol=AgentProtocol.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:11434/v1",
        credential_environments=(),
        credential_required=False,
        auto_detect=False,
    ),
    AgentProvider.LM_STUDIO: ProviderSpec(
        provider=AgentProvider.LM_STUDIO,
        display_name="LM Studio",
        protocol=AgentProtocol.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:1234/v1",
        credential_environments=(),
        credential_required=False,
        auto_detect=False,
    ),
    AgentProvider.CUSTOM: ProviderSpec(
        provider=AgentProvider.CUSTOM,
        display_name="Custom compatible endpoint",
        protocol=AgentProtocol.OPENAI_COMPATIBLE,
        base_url="",
        credential_environments=(CUSTOM_AGENT_KEY_ENV,),
        credential_required=False,
        auto_detect=False,
    ),
}

_PROVIDER_ALIASES = {
    "lm-studio": AgentProvider.LM_STUDIO,
    "lmstudio": AgentProvider.LM_STUDIO,
}


def load_agent_connection(
    environ: Mapping[str, str] | None = None,
) -> AgentConnection:
    """Resolve safe provider status while never retaining a credential value."""

    values = os.environ if environ is None else environ
    explicit = _setting(values, AGENT_PROVIDER_ENV)
    if explicit:
        provider = _provider(explicit)
        available: tuple[str, ...] = ()
    else:
        detected = _detected_providers(values)
        if len(detected) > 1:
            return AgentConnection(
                state=AgentConnectionState.CHOICE_REQUIRED,
                provider=None,
                provider_name="",
                protocol=None,
                model="",
                base_url="",
                credential_environment=None,
                available_providers=tuple(PROVIDER_SPECS[item].display_name for item in detected),
                message="Choose which configured provider Plantain should use.",
            )
        if not detected:
            return AgentConnection(
                state=AgentConnectionState.SETUP_REQUIRED,
                provider=None,
                provider_name="",
                protocol=None,
                model="",
                base_url="",
                credential_environment=None,
                available_providers=(),
                message="Add a provider key or select a local provider in Settings.",
            )
        provider = detected[0]
        available = (PROVIDER_SPECS[provider].display_name,)

    spec = PROVIDER_SPECS[provider]
    model = _model_name(_setting(values, AGENT_MODEL_ENV))
    base_url = (
        _custom_base_url(_setting(values, AGENT_BASE_URL_ENV))
        if provider is AgentProvider.CUSTOM
        else spec.base_url
    )
    credential_environment = _available_credential(spec, values)
    missing: list[str] = []
    if not model:
        missing.append("Choose a model")
    if not base_url:
        missing.append("Add a compatible endpoint")
    if spec.credential_required and credential_environment is None:
        names = " or ".join(spec.credential_environments)
        missing.append(f"Set {names}")
    if missing:
        return AgentConnection(
            state=AgentConnectionState.SETUP_REQUIRED,
            provider=provider,
            provider_name=spec.display_name,
            protocol=spec.protocol,
            model=model,
            base_url=base_url,
            credential_environment=credential_environment,
            available_providers=available,
            message=f"{'; '.join(missing)} to connect {spec.display_name}.",
        )
    return AgentConnection(
        state=AgentConnectionState.READY,
        provider=provider,
        provider_name=spec.display_name,
        protocol=spec.protocol,
        model=model,
        base_url=base_url,
        credential_environment=credential_environment,
        available_providers=available,
        message=f"{spec.display_name} is ready.",
    )


def _provider(value: str) -> AgentProvider:
    normalized = value.casefold().replace(" ", "_")
    if normalized in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[normalized]
    try:
        return AgentProvider(normalized)
    except ValueError as exc:
        raise AgentConfigurationError(
            f"{AGENT_PROVIDER_ENV} selects an unsupported provider"
        ) from exc


def _detected_providers(values: Mapping[str, str]) -> tuple[AgentProvider, ...]:
    return tuple(
        provider
        for provider, spec in PROVIDER_SPECS.items()
        if spec.auto_detect and _available_credential(spec, values) is not None
    )


def _available_credential(
    spec: ProviderSpec,
    values: Mapping[str, str],
) -> str | None:
    return next(
        (
            name
            for name in spec.credential_environments
            if name in values and bool(values[name].strip())
        ),
        None,
    )


def _setting(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    return value.strip() if value is not None else ""


def _model_name(value: str) -> str:
    if not value:
        return ""
    if len(value) > MAX_MODEL_NAME_LENGTH or _MODEL_NAME.fullmatch(value) is None:
        raise AgentConfigurationError(f"{AGENT_MODEL_ENV} must be a valid model identifier")
    return value


def _custom_base_url(value: str) -> str:
    if not value:
        return ""
    if len(value) > MAX_AGENT_BASE_URL_LENGTH:
        raise AgentConfigurationError(f"{AGENT_BASE_URL_ENV} exceeds its length limit")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AgentConfigurationError(
            f"{AGENT_BASE_URL_ENV} must be a valid HTTP or HTTPS origin"
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is None and ":" in parsed.netloc.rsplit("]", maxsplit=1)[-1])
    ):
        raise AgentConfigurationError(
            f"{AGENT_BASE_URL_ENV} must be an HTTP or HTTPS URL without credentials, "
            "a query, or a fragment"
        )
    return value.rstrip("/")


__all__ = [
    "AGENT_BASE_URL_ENV",
    "AGENT_MODEL_ENV",
    "AGENT_PROVIDER_ENV",
    "CUSTOM_AGENT_KEY_ENV",
    "PROVIDER_SPECS",
    "AgentConfigurationError",
    "AgentConnection",
    "AgentConnectionState",
    "AgentProtocol",
    "AgentProvider",
    "load_agent_connection",
]
