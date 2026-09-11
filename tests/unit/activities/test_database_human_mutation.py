"""Human-only database mutation boundary tests with no live database access."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.pool import NullPool

import plantain.activities.database.human_mutation as mutation_module
from plantain.activities.database.human_mutation import (
    HUMAN_MUTATION_ACKNOWLEDGEMENT,
    MAX_HUMAN_SQL_FILE_BYTES,
    HumanMutationError,
    _execute_sync,
    _read_human_sql,
    execute_human_mutation_file,
)
from plantain.models.database import DatabaseSource
from plantain.security import url_policy


def _settings(project_root: Path, *, allowed: bool = True) -> Any:
    return SimpleNamespace(
        project_root=project_root,
        allow_db_mutations=allowed,
        environment="test",
        allow_insecure_local_db_tls=False,
        db_allowed_targets=("db.example.test:5432",),
        db_allow_private_networks=True,
        egress_control_enforced=True,
    )


@pytest.fixture(autouse=True)
def _resolve_database_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_policy,
        "_resolve_isolated_sync",
        lambda _admission, _host, _port, _cancelled: {"10.0.0.8"},
    )


def test_environment_gate_and_exact_acknowledgement_precede_file_access(
    tmp_path: Path,
) -> None:
    missing = Path("reviewed/missing.sql")

    with pytest.raises(HumanMutationError, match="ALLOW_DB_MUTATIONS"):
        asyncio.run(
            execute_human_mutation_file(
                cast("Any", _settings(tmp_path, allowed=False)),
                sql_file=missing,
                parameters={},
                acknowledgement=HUMAN_MUTATION_ACKNOWLEDGEMENT,
            )
        )
    with pytest.raises(HumanMutationError, match="exact human mutation acknowledgement"):
        asyncio.run(
            execute_human_mutation_file(
                cast("Any", _settings(tmp_path)),
                sql_file=missing,
                parameters={},
                acknowledgement="not authorized",
            )
        )


def test_relative_sql_path_is_anchored_to_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    reviewed = root / "reviewed"
    reviewed.mkdir(parents=True)
    (reviewed / "change.sql").write_text(
        "INSERT INTO audit_log (message) VALUES (:message)",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    statement = _read_human_sql(
        cast("Any", _settings(root)),
        Path("reviewed/change.sql"),
    )

    assert statement == "INSERT INTO audit_log (message) VALUES (:message)"


def test_sql_file_must_be_contained_nonempty_utf8_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.sql"
    outside.write_text("DELETE FROM audit_log", encoding="utf-8")
    empty = root / "empty.sql"
    empty.write_text("  ", encoding="utf-8")
    invalid = root / "invalid.sql"
    invalid.write_bytes(b"\xff")
    oversized = root / "oversized.sql"
    oversized.write_bytes(b"x" * (MAX_HUMAN_SQL_FILE_BYTES + 1))
    settings = cast("Any", _settings(root))

    with pytest.raises(HumanMutationError, match="stay inside"):
        _read_human_sql(settings, outside)
    with pytest.raises(HumanMutationError, match="contain one DML"):
        _read_human_sql(settings, empty)
    with pytest.raises(HumanMutationError, match="readable UTF-8"):
        _read_human_sql(settings, invalid)
    with pytest.raises(HumanMutationError, match="exceeds 1 MB"):
        _read_human_sql(settings, oversized)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM audit_log",
        "CREATE TABLE unsafe_table (id INTEGER)",
        "INSERT INTO audit_log (message) VALUES ('one'); DELETE FROM audit_log",
    ],
)
def test_non_dml_and_multiple_statements_fail_before_source_lookup(
    statement: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_source_lookup() -> DatabaseSource:
        raise AssertionError("database environment must not be read")

    monkeypatch.setattr(
        mutation_module,
        "database_source_from_environment",
        forbidden_source_lookup,
    )

    with pytest.raises(HumanMutationError):
        _execute_sync(cast("Any", _settings(tmp_path)), statement, {})


def test_authorized_dml_uses_null_pool_commits_and_disposes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = DatabaseSource.model_validate(
        {
            "username": "human-operator",
            "password": "unit-test-credential",
            "dbUrl": "jdbc:postgresql://db.example.test/clothing",
        }
    )
    executed: list[tuple[str, dict[str, object]]] = []
    engine_options: list[dict[str, object]] = []

    class FakeConnection:
        def execute(
            self,
            statement: object,
            parameters: dict[str, object],
        ) -> object:
            executed.append((str(statement), parameters))
            return SimpleNamespace(rowcount=1)

    class FakeTransaction:
        def __enter__(self) -> FakeConnection:
            return FakeConnection()

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeEngine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        def begin(self) -> FakeTransaction:
            return FakeTransaction()

        def dispose(self, *, close: bool) -> None:
            assert close is True
            self.dispose_calls += 1

    engine = FakeEngine()

    def resolve_source(
        resolved_source: DatabaseSource,
        *,
        tls_policy: object,
        read_only_intent: bool,
    ) -> object:
        assert resolved_source is source
        assert tls_policy is not None
        assert read_only_intent is False
        return SimpleNamespace(
            sqlalchemy_url="postgresql+psycopg://unit.test/clothing",
            connect_args={"sslmode": "require"},
            host="db.example.test",
            port=5432,
        )

    def create_fake_engine(_url: object, **options: object) -> FakeEngine:
        engine_options.append(options)
        return engine

    monkeypatch.setattr(
        mutation_module,
        "database_source_from_environment",
        lambda: source,
    )
    monkeypatch.setattr(mutation_module, "resolve_database_source", resolve_source)
    monkeypatch.setattr(mutation_module, "create_engine", create_fake_engine)

    result = _execute_sync(
        cast("Any", _settings(tmp_path)),
        "INSERT INTO audit_log (message) VALUES (:message)",
        {"message": "reviewed value"},
    )

    assert result.statement_type == "INSERT"
    assert result.affected_rows == 1
    assert executed == [
        (
            "INSERT INTO audit_log (message) VALUES (:message)",
            {"message": "reviewed value"},
        )
    ]
    assert engine_options[0]["poolclass"] is NullPool
    assert engine_options[0]["pool_pre_ping"] is True
    assert engine.dispose_calls == 1
