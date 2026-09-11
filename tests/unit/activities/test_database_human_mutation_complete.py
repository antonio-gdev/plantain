"""Complete isolated human-only mutation boundary coverage using fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError

from plantain.activities.database import human_mutation
from plantain.activities.database.human_mutation import (
    HUMAN_MUTATION_ACKNOWLEDGEMENT,
    HumanMutationError,
    HumanMutationResult,
    _execute_sync,
    _read_human_sql,
    execute_human_mutation_file,
)
from plantain.activities.database.source import DatabaseSourceError
from plantain.security import url_policy


def _settings(root: Path) -> Any:
    return SimpleNamespace(
        project_root=root,
        allow_db_mutations=True,
        environment="production",
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


def test_async_human_boundary_delegates_reviewed_file_and_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def read_sql(_settings_value: Any, sql_file: Path) -> str:
        calls.append(("read", sql_file))
        return "UPDATE audit_log SET reviewed = :reviewed"

    def execute_sql(
        _settings_value: Any,
        statement: str,
        parameters: dict[str, Any],
        **_options: Any,
    ) -> Any:
        calls.append(("execute", statement, parameters))
        return HumanMutationResult(statement_type="UPDATE", affected_rows=1)

    monkeypatch.setattr(human_mutation, "_read_human_sql", read_sql)
    monkeypatch.setattr(human_mutation, "_execute_sync", execute_sql)
    result = asyncio.run(
        execute_human_mutation_file(
            cast("Any", _settings(tmp_path)),
            sql_file=Path("reviewed/change.sql"),
            parameters={"reviewed": True},
            acknowledgement=HUMAN_MUTATION_ACKNOWLEDGEMENT,
        )
    )

    assert result == HumanMutationResult(statement_type="UPDATE", affected_rows=1)
    assert calls == [
        ("read", Path("reviewed/change.sql")),
        ("execute", "UPDATE audit_log SET reviewed = :reviewed", {"reviewed": True}),
    ]


def test_human_sql_file_must_exist_and_be_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = cast("Any", _settings(tmp_path))
    with pytest.raises(HumanMutationError, match="is missing"):
        _read_human_sql(settings, Path("missing.sql"))

    target = tmp_path / "reviewed.sql"
    target.write_text("DELETE FROM audit_log", encoding="utf-8")
    original_open = Path.open

    def reject_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == target:
            raise OSError("synthetic filesystem detail")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_open)
    with pytest.raises(HumanMutationError, match="readable UTF-8") as captured:
        _read_human_sql(settings, target)
    assert "synthetic filesystem detail" not in str(captured.value)


def test_human_statement_parse_failure_precedes_environment_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        human_mutation,
        "database_source_from_environment",
        lambda: pytest.fail("environment lookup must not occur"),
    )

    def reject_parse(_statement: str) -> Any:
        raise human_mutation.ParseError("synthetic parser detail")

    monkeypatch.setattr(human_mutation, "parse_one", reject_parse)

    with pytest.raises(HumanMutationError, match="could not be parsed") as captured:
        _execute_sync(cast("Any", _settings(tmp_path)), "UPDATE audit_log SET reviewed = 1", {})
    assert "synthetic parser detail" not in str(captured.value)


def test_human_source_errors_are_translated_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_source() -> Any:
        raise DatabaseSourceError("safe source configuration failure")

    monkeypatch.setattr(human_mutation, "database_source_from_environment", reject_source)
    with pytest.raises(HumanMutationError, match="safe source configuration failure"):
        _execute_sync(
            cast("Any", _settings(tmp_path)),
            "DELETE FROM audit_log WHERE reviewed = :reviewed",
            {"reviewed": True},
        )


def _install_resolved_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def source_from_environment() -> object:
        return object()

    monkeypatch.setattr(human_mutation, "database_source_from_environment", source_from_environment)
    monkeypatch.setattr(
        human_mutation,
        "resolve_database_source",
        lambda *_args, **_kwargs: SimpleNamespace(
            sqlalchemy_url="postgresql+psycopg://db.example.test/app",
            connect_args={},
            host="db.example.test",
            port=5432,
        ),
    )


def test_human_target_policy_blocks_before_engine_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolved_source(monkeypatch)
    settings = _settings(tmp_path)
    settings.db_allowed_targets = ()
    monkeypatch.setattr(
        human_mutation,
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail("engine must not be created"),
    )

    with pytest.raises(HumanMutationError, match="blocked by outbound network policy"):
        _execute_sync(
            cast("Any", settings),
            "DELETE FROM audit_log WHERE reviewed = :reviewed",
            {"reviewed": True},
        )


def test_human_driver_failure_is_classified_without_driver_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolved_source(monkeypatch)

    def reject_engine(*_args: Any, **_kwargs: Any) -> Any:
        raise SQLAlchemyError("synthetic driver detail")

    monkeypatch.setattr(human_mutation, "create_engine", reject_engine)
    with pytest.raises(HumanMutationError, match=r"failed \(SQLAlchemyError\)") as captured:
        _execute_sync(
            cast("Any", _settings(tmp_path)),
            "UPDATE audit_log SET reviewed = :reviewed",
            {"reviewed": True},
        )
    assert "synthetic driver detail" not in str(captured.value)


def test_negative_driver_row_count_becomes_unknown_and_engine_is_disposed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolved_source(monkeypatch)

    class Transaction:
        def __enter__(self) -> Any:
            return SimpleNamespace(
                execute=lambda _statement, _parameters: SimpleNamespace(rowcount=-1)
            )

        def __exit__(self, *_args: Any) -> None:
            return None

    class Engine:
        def __init__(self) -> None:
            self.disposed = False

        def begin(self) -> Transaction:
            return Transaction()

        def dispose(self, *, close: bool) -> None:
            assert close is True
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(human_mutation, "create_engine", lambda *_args, **_kwargs: engine)

    result = _execute_sync(
        cast("Any", _settings(tmp_path)),
        "DELETE FROM audit_log WHERE reviewed = :reviewed",
        {"reviewed": True},
    )

    assert result.affected_rows is None
    assert engine.disposed is True
