"""Agent usage telemetry is private, bounded, exact, and content-free."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plantain.dashboard import agent_usage
from plantain.dashboard.agent.connection import AgentProvider
from plantain.dashboard.agent.models import AgentResponseMetadata, AgentTokenUsage
from plantain.dashboard.agent_usage import (
    AgentUsageOperation,
    AgentUsageRecorder,
    DashboardAgentUsageError,
    load_agent_usage_overview,
    record_agent_usage,
)
from plantain.errors import AtomicPersistenceError
from plantain.persistence import write_bytes_atomic
from plantain.security.redaction import REDACTED

FIRST_EVENT_ID = "a" * 32
SECOND_EVENT_ID = "b" * 32
THIRD_EVENT_ID = "c" * 32
NOW_MS = int(datetime(2026, 9, 9, 12, tzinfo=UTC).timestamp() * 1_000)
DAY_MS = 86_400_000
HOSTED_TOTAL_TOKENS = 15
LOCAL_TOTAL_TOKENS = 5
EXPECTED_TOTAL_CALLS = 3
EXPECTED_METERED_CALLS = 2
EXPECTED_HOSTED_CALLS = 2
EXPECTED_INPUT_TOKENS = 13
EXPECTED_OUTPUT_TOKENS = 7
EXPECTED_TOTAL_TOKENS = 20
PRIVATE_MODEL_FRAGMENT = "private-model-fragment"


def _metadata(
    provider: AgentProvider = AgentProvider.OPENAI,
    *,
    model: str = "test-model",
    usage: AgentTokenUsage | None = None,
) -> AgentResponseMetadata:
    return AgentResponseMetadata(
        provider=provider,
        model=model,
        finish_reason="stop",
        usage=usage,
    )


def _token(value: str) -> Callable[[int], str]:
    return lambda _byte_count: value


def _tokens(*values: str) -> Callable[[int], str]:
    iterator: Iterator[str] = iter(values)
    return lambda _byte_count: next(iterator)


def test_record_is_atomic_content_free_and_collision_safe(tmp_path: Path) -> None:
    usage = AgentTokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    metadata = _metadata(usage=usage)
    first = record_agent_usage(
        tmp_path,
        AgentUsageOperation.INTENT_ROUTING,
        metadata,
        now_ms=NOW_MS,
        token_factory=_token(FIRST_EVENT_ID),
    )
    second = record_agent_usage(
        tmp_path,
        AgentUsageOperation.SCENARIO_AUTHORING,
        metadata,
        now_ms=NOW_MS,
        token_factory=_tokens(FIRST_EVENT_ID, SECOND_EVENT_ID),
    )

    document = json.loads(first.read_text(encoding="utf-8"))

    assert first != second
    assert first.name.endswith(f"{FIRST_EVENT_ID}.usage.json")
    assert second.name.endswith(f"{SECOND_EVENT_ID}.usage.json")
    assert set(document) == {
        "schemaVersion",
        "eventId",
        "recordedAtMs",
        "operation",
        "provider",
        "model",
        "usage",
    }
    assert document["usage"]["totalTokens"] == HOSTED_TOTAL_TOKENS
    assert "prompt" not in first.read_text(encoding="utf-8").casefold()
    assert "response" not in first.read_text(encoding="utf-8").casefold()


def test_overview_aggregates_exact_reported_usage_and_missing_metering(
    tmp_path: Path,
) -> None:
    record_agent_usage(
        tmp_path,
        AgentUsageOperation.INTENT_ROUTING,
        _metadata(
            usage=AgentTokenUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=HOSTED_TOTAL_TOKENS,
            )
        ),
        now_ms=NOW_MS - DAY_MS,
        token_factory=_token(FIRST_EVENT_ID),
    )
    record_agent_usage(
        tmp_path,
        AgentUsageOperation.SCENARIO_AUTHORING,
        _metadata(),
        now_ms=NOW_MS,
        token_factory=_token(SECOND_EVENT_ID),
    )
    record_agent_usage(
        tmp_path,
        AgentUsageOperation.DECISION_PLANNING,
        _metadata(
            AgentProvider.OLLAMA,
            model="local-model",
            usage=AgentTokenUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=LOCAL_TOTAL_TOKENS,
            ),
        ),
        now_ms=NOW_MS,
        token_factory=_token(THIRD_EVENT_ID),
    )

    overview = load_agent_usage_overview(tmp_path, now_ms=NOW_MS)

    assert overview.total_calls == EXPECTED_TOTAL_CALLS
    assert overview.metered_calls == EXPECTED_METERED_CALLS
    assert overview.input_tokens == EXPECTED_INPUT_TOKENS
    assert overview.output_tokens == EXPECTED_OUTPUT_TOKENS
    assert overview.total_tokens == EXPECTED_TOTAL_TOKENS
    assert overview.cost_boundary == "Provider-managed and local"
    assert overview.breakdowns[0].provider == "OpenAI"
    assert overview.breakdowns[0].call_count == EXPECTED_HOSTED_CALLS
    assert overview.breakdowns[0].metered_call_count == 1
    assert overview.trend[-2].call_count == 1
    assert overview.trend[-1].call_count == EXPECTED_HOSTED_CALLS
    assert overview.notice == ""


def test_overview_redacts_models_and_excludes_untrusted_records(tmp_path: Path) -> None:
    target = record_agent_usage(
        tmp_path,
        AgentUsageOperation.CONNECTION_CHECK,
        _metadata(model=f"token={PRIVATE_MODEL_FRAGMENT}"),
        now_ms=NOW_MS,
        token_factory=_token(FIRST_EVENT_ID),
    )
    directory = target.parent
    write_bytes_atomic(directory / "invalid.usage.json", b"{}")
    write_bytes_atomic(directory / "renamed.usage.json", target.read_bytes())
    outside = tmp_path / "outside.usage.json"
    outside.write_bytes(b"{}")
    (directory / "linked.usage.json").symlink_to(outside)

    overview = load_agent_usage_overview(tmp_path, now_ms=NOW_MS)

    assert overview.total_calls == 1
    assert PRIVATE_MODEL_FRAGMENT not in overview.breakdowns[0].model
    assert REDACTED in overview.breakdowns[0].model
    assert "3 invalid or incompatible usage records were excluded" in overview.notice


def test_overview_discloses_bounded_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for event_id in (FIRST_EVENT_ID, SECOND_EVENT_ID):
        record_agent_usage(
            tmp_path,
            AgentUsageOperation.CONNECTION_CHECK,
            _metadata(),
            now_ms=NOW_MS,
            token_factory=_token(event_id),
        )
    monkeypatch.setattr(agent_usage, "MAX_AGENT_USAGE_FILES", 1)

    overview = load_agent_usage_overview(tmp_path, now_ms=NOW_MS)

    assert overview.total_calls == 1
    assert overview.limited is True
    assert "totals are lower bounds" in overview.notice


def test_symlinked_usage_collection_fails_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "agent-usage").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DashboardAgentUsageError, match="invalid location"):
        load_agent_usage_overview(tmp_path, now_ms=NOW_MS)


def test_runtime_recorder_degrades_without_exposing_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_detail = "synthetic-private-persistence-detail"

    def reject(*_args: object, **_kwargs: object) -> Path:
        raise AtomicPersistenceError(private_detail)

    monkeypatch.setattr(agent_usage, "record_agent_usage", reject)

    AgentUsageRecorder(tmp_path)(
        AgentUsageOperation.INTENT_ROUTING,
        _metadata(),
    )

    assert "could not be persisted safely" in caplog.text
    assert private_detail not in caplog.text
