"""Agent database firewall tests."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from plantain.activities.database.policy import (
    DatabasePolicyError,
    _assert_internal_readonly_setup,
    assert_agent_query,
    assert_agent_read_only,
    install_agent_read_only_firewall,
    prepare_agent_query,
)

SERVER_ROW_CAP = 11


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT item, price FROM uno",
        "WITH latest AS (SELECT item FROM uno) SELECT item FROM latest",
        "SHOW TABLES",
        "DESCRIBE uno",
    ],
)
def test_read_policy_accepts_only_provable_reads(statement: str) -> None:
    assert_agent_read_only(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO uno(item) VALUES ('coat')",
        "UPDATE uno SET price = 20",
        "DELETE FROM uno",
        "DROP TABLE uno",
        "ALTER TABLE uno ADD COLUMN hidden INT",
        "TRUNCATE TABLE uno",
        "SELECT * INTO copied_uno FROM uno",
        "SELECT * FROM uno; DELETE FROM uno",
        "PRAGMA foreign_keys = OFF",
    ],
)
def test_read_policy_rejects_mutation_and_multi_statement_sql(statement: str) -> None:
    with pytest.raises(DatabasePolicyError):
        assert_agent_read_only(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM uno FOR UPDATE",
        "SELECT * FROM uno FOR SHARE",
        "SHOW TABLES",
        "DESCRIBE uno",
    ],
)
def test_query_policy_rejects_locking_and_metadata_statements(statement: str) -> None:
    with pytest.raises(DatabasePolicyError):
        assert_agent_query(statement)


@pytest.mark.parametrize(
    ("statement", "dialect"),
    [
        ("SELECT pg_catalog.pg_advisory_lock(1)", "postgres"),
        ("SELECT nextval('sequence_name')", "postgres"),
        ("SELECT dblink('remote', 'SELECT 1')", "postgres"),
        ("SELECT GET_LOCK('resource', 1)", "mysql"),
        ("SELECT LOAD_FILE('/tmp/example')", "mysql"),
        ("SELECT UTL_HTTP.REQUEST('https://example.test') FROM dual", "oracle"),
        ("SELECT DBMS_LOCK.SLEEP(1) FROM dual", "oracle"),
    ],
)
def test_query_policy_rejects_side_effecting_and_external_functions(
    statement: str,
    dialect: str,
) -> None:
    with pytest.raises(DatabasePolicyError):
        assert_agent_query(statement, dialect=dialect)


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgres", "LIMIT 11"),
        ("mysql", "LIMIT 11"),
        ("tsql", "TOP 11"),
        ("oracle", "FETCH FIRST 11 ROWS ONLY"),
    ],
)
def test_query_policy_quietly_applies_vendor_row_bound(
    dialect: str,
    expected: str,
) -> None:
    bounded = prepare_agent_query(
        "SELECT :value AS value",
        dialect=dialect,
        row_limit=SERVER_ROW_CAP,
    )

    assert expected in bounded
    assert ":value" in bounded
    assert_agent_query("SELECT COUNT(*) FROM items", dialect=dialect)


def test_query_policy_preserves_an_explicit_root_limit() -> None:
    statement = "SELECT * FROM items LIMIT 5"

    assert (
        prepare_agent_query(
            statement,
            dialect="postgres",
            row_limit=SERVER_ROW_CAP,
        )
        == statement
    )


def test_query_policy_preserves_postgres_bind_without_rewriting_literal() -> None:
    bounded = prepare_agent_query(
        "SELECT '%(value)s' AS literal, :value AS bound",
        dialect="postgres",
        row_limit=SERVER_ROW_CAP,
    )

    assert "'%(value)s' AS literal" in bounded
    assert ":value AS bound" in bounded


def test_internal_setup_requires_an_exact_safe_statement() -> None:
    for statement in (
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = 15000",
        "SET SESSION MAX_EXECUTION_TIME = 15000",
        "PRAGMA query_only = ON",
    ):
        _assert_internal_readonly_setup(statement)

    with pytest.raises(DatabasePolicyError):
        _assert_internal_readonly_setup("SET TRANSACTION READ ONLY; DELETE FROM uno")
    with pytest.raises(DatabasePolicyError):
        _assert_internal_readonly_setup("SET LOCAL statement_timeout = 0")


def test_engine_firewall_guards_every_cursor_execution() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    install_agent_read_only_firewall(engine)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
            with pytest.raises(DatabasePolicyError):
                connection.exec_driver_sql("CREATE TABLE unsafe(value INTEGER)")
    finally:
        engine.dispose(close=True)
