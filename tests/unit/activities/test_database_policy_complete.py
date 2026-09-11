"""Complete fail-closed database firewall coverage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlglot import exp
from sqlglot.errors import ParseError

from plantain.activities.database import policy
from plantain.activities.database.policy import (
    DatabasePolicyError,
    assert_agent_query,
    assert_agent_read_only,
    install_agent_read_only_firewall,
)


def test_read_policy_enforces_length_prefix_parse_and_cardinality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "MAX_DATABASE_STATEMENT_LENGTH", 8)
    with pytest.raises(DatabasePolicyError, match="safety limit"):
        assert_agent_read_only("SELECT 12345")

    monkeypatch.setattr(policy, "MAX_DATABASE_STATEMENT_LENGTH", 1_000)
    with pytest.raises(DatabasePolicyError, match="metadata reads and SELECT only"):
        assert_agent_read_only("PRAGMA query_only")
    with pytest.raises(DatabasePolicyError, match="could not be proven read-only"):
        assert_agent_read_only("SELECT (")

    with pytest.raises(DatabasePolicyError, match="Multiple database statements"):
        assert_agent_read_only("SELECT 1; SELECT 2")


def test_read_policy_rejects_parser_empty_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def empty_parse(_statement: str, *, read: str | None = None) -> list[None]:
        assert read is None
        return [None]

    monkeypatch.setattr(policy, "parse", empty_parse)

    with pytest.raises(DatabasePolicyError, match="Empty database statements"):
        assert_agent_read_only("SELECT 1")


def test_query_policy_wraps_parse_failure_without_parser_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def failing_parse(_statement: str, *, read: str | None = None) -> list[Any]:
        nonlocal calls
        assert read is None
        calls += 1
        raise ParseError("synthetic parser detail")

    monkeypatch.setattr(policy, "parse", failing_parse)
    with pytest.raises(DatabasePolicyError, match="could not be proven") as captured:
        assert_agent_query("SELECT 1")
    assert calls == 1
    assert "synthetic parser detail" not in str(captured.value)


def test_query_policy_requires_query_expression_on_single_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def non_query_parse(_statement: str, *, read: str | None = None) -> list[Any]:
        nonlocal calls
        assert read is None
        calls += 1
        return [exp.Literal.string("not-a-query")]

    monkeypatch.setattr(policy, "parse", non_query_parse)
    with pytest.raises(DatabasePolicyError, match="SELECT statements only"):
        assert_agent_query("SELECT 1")
    assert calls == 1


def test_firewall_routes_internal_setup_and_maps_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[str, Any] = {}

    def listens_for(_engine: Any, event_name: str) -> Any:
        def register(handler: Any) -> Any:
            handlers[event_name] = handler
            return handler

        return register

    monkeypatch.setattr(policy.event, "listens_for", listens_for)
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mssql"))
    install_agent_read_only_firewall(engine)
    guard = handlers["before_cursor_execute"]
    internal_context = SimpleNamespace(execution_options={"plantain_internal_readonly_setup": True})
    normal_context = SimpleNamespace(execution_options={})

    guard(None, None, "SET TRANSACTION READ ONLY", None, internal_context, False)
    guard(None, None, "SELECT 1", None, normal_context, False)
    with pytest.raises(DatabasePolicyError, match="Unrecognized internal"):
        guard(None, None, "SET ROLE elevated", None, internal_context, False)
    with pytest.raises(DatabasePolicyError, match="SELECT only"):
        guard(None, None, "UPDATE uno SET price = 1", None, normal_context, False)


def test_firewall_handles_context_without_execution_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[Any] = []

    def listens_for(_engine: Any, _event_name: str) -> Any:
        def register(handler: Any) -> Any:
            handlers.append(handler)
            return handler

        return register

    monkeypatch.setattr(policy.event, "listens_for", listens_for)
    install_agent_read_only_firewall(SimpleNamespace(dialect=SimpleNamespace(name="mysql")))

    handlers[0](None, None, "SELECT 1", None, object(), False)


(SimpleNamespace(dialect=SimpleNamespace(name="mysql")))
