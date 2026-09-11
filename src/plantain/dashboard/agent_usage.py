"""Private, bounded usage telemetry for dashboard-owned agent calls."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from plantain.dashboard.agent.connection import AgentProvider
from plantain.dashboard.agent.models import AgentResponseMetadata, AgentTokenUsage
from plantain.dashboard.agent_usage_types import AgentUsageOperation
from plantain.errors import AtomicPersistenceError, AtomicTargetExistsError
from plantain.models.common import StrictModel
from plantain.persistence import open_binary_read_no_follow, write_bytes_atomic_new
from plantain.security.redaction import RedactionPolicy

AGENT_USAGE_SCHEMA_VERSION = "1.0"
AGENT_USAGE_DIRECTORY = ("dashboard", "agent-usage")
AGENT_USAGE_FILE_SUFFIX = ".usage.json"
AGENT_USAGE_ID_BYTES = 16
AGENT_USAGE_ID_LENGTH = AGENT_USAGE_ID_BYTES * 2
AGENT_USAGE_WRITE_ATTEMPTS = 8
MAX_AGENT_USAGE_FILES = 10_000
MAX_AGENT_USAGE_RECORD_BYTES = 8_192
MAX_REPORTED_TOKEN_COUNT = 1_000_000_000
MAX_USAGE_BREAKDOWNS = 5
USAGE_TREND_DAYS = 7
MAX_TIMESTAMP_MS = 253_402_300_799_000
MILLISECONDS_PER_SECOND = 1_000

logger = logging.getLogger("plantain.dashboard.agent_usage")


class DashboardAgentUsageError(RuntimeError):
    """Raised when local agent usage cannot be handled safely."""


class _AgentUsageDocument(StrictModel):
    """One private value-free provider usage record."""

    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(
        min_length=AGENT_USAGE_ID_LENGTH,
        max_length=AGENT_USAGE_ID_LENGTH,
        pattern=r"^[0-9a-f]+$",
    )
    recorded_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    operation: AgentUsageOperation
    provider: AgentProvider
    model: str = Field(min_length=1, max_length=160)
    usage: AgentTokenUsage | None = None

    @model_validator(mode="after")
    def bounded_usage(self) -> Self:
        """Reject implausibly large externally reported counters."""

        if (
            self.usage is not None
            and max(
                self.usage.input_tokens,
                self.usage.output_tokens,
                self.usage.total_tokens,
            )
            > MAX_REPORTED_TOKEN_COUNT
        ):
            raise ValueError("Agent usage exceeds its safe counter limit")
        return self


@dataclass(frozen=True, slots=True)
class AgentUsageRecorder:
    """Best-effort atomic recorder that never blocks a completed agent task."""

    output_dir: Path

    def __call__(
        self,
        operation: AgentUsageOperation,
        metadata: AgentResponseMetadata,
    ) -> None:
        try:
            record_agent_usage(self.output_dir, operation, metadata)
        except (AtomicPersistenceError, DashboardAgentUsageError, OSError):
            logger.warning("Agent usage metadata could not be persisted safely")


def record_agent_usage(
    output_dir: Path,
    operation: AgentUsageOperation,
    metadata: AgentResponseMetadata,
    *,
    now_ms: int | None = None,
    token_factory: Callable[[int], str] = token_hex,
) -> Path:
    """Atomically create one private record without retaining request content."""

    timestamp = _recorded_at_ms(now_ms)
    directory = output_dir.joinpath(*AGENT_USAGE_DIRECTORY)
    for _attempt in range(AGENT_USAGE_WRITE_ATTEMPTS):
        event_id = _event_id(token_factory)
        document = _AgentUsageDocument(
            event_id=event_id,
            recorded_at_ms=timestamp,
            operation=operation,
            provider=metadata.provider,
            model=metadata.model,
            usage=metadata.usage,
        )
        payload = f"{document.model_dump_json(by_alias=True)}\n".encode()
        if len(payload) > MAX_AGENT_USAGE_RECORD_BYTES:
            raise DashboardAgentUsageError("Agent usage record exceeds its byte limit")
        target = directory / (f"{timestamp:015d}-{event_id}{AGENT_USAGE_FILE_SUFFIX}")
        try:
            write_bytes_atomic_new(target, payload)
        except AtomicTargetExistsError:
            continue
        return target
    raise DashboardAgentUsageError("A unique agent usage record could not be allocated")


def _recorded_at_ms(value: int | None) -> int:
    timestamp = time.time_ns() // 1_000_000 if value is None else value
    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or timestamp < 0
        or timestamp > MAX_TIMESTAMP_MS
    ):
        raise DashboardAgentUsageError("Agent usage timestamp is invalid")
    return timestamp


def _event_id(token_factory: Callable[[int], str]) -> str:
    value = token_factory(AGENT_USAGE_ID_BYTES)
    if (
        not isinstance(value, str)
        or len(value) != AGENT_USAGE_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DashboardAgentUsageError("Agent usage identifier is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AgentUsageBreakdown:
    """Browser-safe aggregate for one provider and model."""

    provider: str
    model: str
    call_count: int
    metered_call_count: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class AgentUsageTrendPoint:
    """One UTC day in the bounded usage trend."""

    label: str
    call_count: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class AgentUsageOverview:
    """Bounded browser-safe usage summary."""

    total_calls: int
    metered_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_boundary: str
    breakdowns: tuple[AgentUsageBreakdown, ...]
    trend: tuple[AgentUsageTrendPoint, ...]
    limited: bool
    notice: str


@dataclass(slots=True)
class _UsageAccumulator:
    call_count: int = 0
    metered_call_count: int = 0
    total_tokens: int = 0


_PROVIDER_LABELS = {
    AgentProvider.OPENAI: "OpenAI",
    AgentProvider.ANTHROPIC: "Anthropic",
    AgentProvider.DEEPSEEK: "DeepSeek",
    AgentProvider.GEMINI: "Gemini",
    AgentProvider.OLLAMA: "Ollama",
    AgentProvider.LM_STUDIO: "LM Studio",
    AgentProvider.CUSTOM: "Custom endpoint",
}
_LOCAL_PROVIDERS = frozenset(
    {
        AgentProvider.OLLAMA,
        AgentProvider.LM_STUDIO,
    }
)


def load_agent_usage_overview(
    output_dir: Path,
    *,
    sensitive_keys: tuple[str, ...] = (),
    now_ms: int | None = None,
) -> AgentUsageOverview:
    """Load exact reported usage from bounded private local records."""

    directory = output_dir.joinpath(*AGENT_USAGE_DIRECTORY)
    documents, limited, invalid_count = _read_usage_documents(directory)
    timestamp = _recorded_at_ms(now_ms)
    redaction = RedactionPolicy(sensitive_keys)
    return _usage_overview(
        documents,
        timestamp,
        redaction,
        limited=limited,
        invalid_count=invalid_count,
    )


def unavailable_agent_usage_overview(
    *,
    now_ms: int | None = None,
) -> AgentUsageOverview:
    """Return an explicit empty projection after a fail-closed usage read."""

    trend = _empty_trend(_recorded_at_ms(now_ms))
    return AgentUsageOverview(
        total_calls=0,
        metered_calls=0,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_boundary="Unavailable",
        breakdowns=(),
        trend=tuple(
            AgentUsageTrendPoint(label=day.strftime("%b %d"), call_count=0, total_tokens=0)
            for day, _unused in trend
        ),
        limited=False,
        notice="Agent usage history is unavailable.",
    )


def _read_usage_documents(
    directory: Path,
) -> tuple[tuple[_AgentUsageDocument, ...], bool, int]:
    if not directory.exists():
        return (), False, 0
    if directory.is_symlink() or not directory.is_dir():
        raise DashboardAgentUsageError("The agent usage collection has an invalid location")
    try:
        with os.scandir(directory) as scanner:
            entries = sorted(scanner, key=lambda entry: entry.name)
    except OSError as exc:
        raise DashboardAgentUsageError(
            "The agent usage collection could not be enumerated safely"
        ) from exc

    documents: list[_AgentUsageDocument] = []
    invalid_count = 0
    limited = False
    for index, entry in enumerate(entries):
        if index >= MAX_AGENT_USAGE_FILES:
            limited = True
            break
        if not entry.name.endswith(AGENT_USAGE_FILE_SUFFIX):
            continue
        try:
            regular_file = not entry.is_symlink() and entry.is_file(follow_symlinks=False)
        except OSError:
            regular_file = False
        if not regular_file:
            invalid_count += 1
            continue
        document = _read_usage_document(Path(entry.path))
        if document is None:
            invalid_count += 1
        else:
            documents.append(document)
    return tuple(documents), limited, invalid_count


def _read_usage_document(path: Path) -> _AgentUsageDocument | None:
    try:
        with open_binary_read_no_follow(path, private=True) as stream:
            raw = stream.read(MAX_AGENT_USAGE_RECORD_BYTES + 1)
        if len(raw) > MAX_AGENT_USAGE_RECORD_BYTES:
            return None
        document = _AgentUsageDocument.model_validate_json(raw)
    except (
        AtomicPersistenceError,
        OSError,
        UnicodeError,
        ValidationError,
        ValueError,
    ):
        return None
    expected_name = f"{document.recorded_at_ms:015d}-{document.event_id}{AGENT_USAGE_FILE_SUFFIX}"
    return document if path.name == expected_name else None


def _usage_overview(
    documents: tuple[_AgentUsageDocument, ...],
    now_ms: int,
    redaction: RedactionPolicy,
    *,
    limited: bool,
    invalid_count: int,
) -> AgentUsageOverview:
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    metered_calls = 0
    providers: set[AgentProvider] = set()
    breakdowns: dict[tuple[AgentProvider, str], _UsageAccumulator] = {}
    trend = _empty_trend(now_ms)
    trend_values = {point[0]: _UsageAccumulator() for point in trend}

    for document in documents:
        providers.add(document.provider)
        key = (document.provider, document.model)
        aggregate = breakdowns.setdefault(key, _UsageAccumulator())
        aggregate.call_count += 1
        usage = document.usage
        if usage is not None:
            metered_calls += 1
            aggregate.metered_call_count += 1
            aggregate.total_tokens += usage.total_tokens
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            total_tokens += usage.total_tokens
        day = _usage_day(document.recorded_at_ms)
        daily = trend_values.get(day)
        if daily is not None:
            daily.call_count += 1
            if usage is not None:
                daily.total_tokens += usage.total_tokens

    return AgentUsageOverview(
        total_calls=len(documents),
        metered_calls=metered_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_boundary=_cost_boundary(providers),
        breakdowns=_top_breakdowns(breakdowns, redaction),
        trend=tuple(
            AgentUsageTrendPoint(
                label=day.strftime("%b %d"),
                call_count=trend_values[day].call_count,
                total_tokens=trend_values[day].total_tokens,
            )
            for day, _unused in trend
        ),
        limited=limited,
        notice=_usage_notice(invalid_count, limited),
    )


def _empty_trend(now_ms: int) -> tuple[tuple[date, None], ...]:
    today = _usage_day(now_ms)
    return tuple(
        (today - timedelta(days=offset), None) for offset in reversed(range(USAGE_TREND_DAYS))
    )


def _usage_day(recorded_at_ms: int) -> date:
    return datetime.fromtimestamp(
        recorded_at_ms / MILLISECONDS_PER_SECOND,
        tz=UTC,
    ).date()


def _top_breakdowns(
    values: dict[tuple[AgentProvider, str], _UsageAccumulator],
    redaction: RedactionPolicy,
) -> tuple[AgentUsageBreakdown, ...]:
    ordered = sorted(
        values.items(),
        key=lambda item: (
            -item[1].total_tokens,
            -item[1].call_count,
            item[0][0].value,
            item[0][1].casefold(),
        ),
    )
    return tuple(
        AgentUsageBreakdown(
            provider=_PROVIDER_LABELS[provider],
            model=_safe_model(model, redaction),
            call_count=value.call_count,
            metered_call_count=value.metered_call_count,
            total_tokens=value.total_tokens,
        )
        for (provider, model), value in ordered[:MAX_USAGE_BREAKDOWNS]
    )


def _safe_model(value: str, redaction: RedactionPolicy) -> str:
    rendered = " ".join(redaction.redact_text(value).split())
    return rendered or "Unnamed model"


def _cost_boundary(providers: set[AgentProvider]) -> str:
    if not providers:
        return "No recorded usage"
    if providers <= _LOCAL_PROVIDERS:
        return "Local runtime"
    if providers == {AgentProvider.CUSTOM}:
        return "Configured endpoint"
    if providers & _LOCAL_PROVIDERS:
        return "Provider-managed and local"
    return "Provider-managed"


def _usage_notice(invalid_count: int, limited: bool) -> str:
    notices: list[str] = []
    if limited:
        notices.append("Usage scanning reached its local display budget; totals are lower bounds.")
    if invalid_count:
        notices.append(
            f"{invalid_count} invalid or incompatible usage "
            f"{'record was' if invalid_count == 1 else 'records were'} excluded."
        )
    return " ".join(notices)


__all__ = [
    "AgentUsageBreakdown",
    "AgentUsageOperation",
    "AgentUsageOverview",
    "AgentUsageRecorder",
    "AgentUsageTrendPoint",
    "DashboardAgentUsageError",
    "load_agent_usage_overview",
    "record_agent_usage",
    "unavailable_agent_usage_overview",
]
