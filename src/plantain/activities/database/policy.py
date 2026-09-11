"""Fail-closed SQL statement firewall for every agent-accessible connection."""

from __future__ import annotations

import re
import secrets

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlglot import exp, parse
from sqlglot.errors import ParseError


class DatabasePolicyError(RuntimeError):
    """Raised when database access violates the immutable agent policy."""


_DIALECTS = {
    "mssql": "tsql",
    "postgresql": "postgres",
}
_READ_PREFIX = re.compile(r"^\s*(?:SELECT|WITH|SHOW|DESCRIBE|DESC)\b", re.IGNORECASE)
_QUERY_PREFIX = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)
_LOCKING_READ = re.compile(
    r"\b(?:FOR\s+(?:NO\s+KEY\s+)?UPDATE|FOR\s+(?:KEY\s+)?SHARE|LOCK\s+IN\s+SHARE\s+MODE)\b",
    re.IGNORECASE,
)
MAX_DATABASE_STATEMENT_LENGTH = 1_000_000
_INTERNAL_TIMEOUT_SETUP = re.compile(
    r"^(?:set local statement_timeout|set session max_execution_time) = [1-9][0-9]*$"
)
_MUTATING_NODES = (
    exp.Alter,
    exp.Command,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Into,
    exp.Merge,
    exp.TruncateTable,
    exp.Update,
    exp.Use,
)
_SIDE_EFFECTING_FUNCTIONS = frozenset(
    {
        "benchmark",
        "dblink",
        "get_lock",
        "load_extension",
        "load_file",
        "lo_create",
        "lo_export",
        "lo_from_bytea",
        "lo_import",
        "lo_put",
        "lo_unlink",
        "nextval",
        "opendatasource",
        "openquery",
        "openrowset",
        "pg_cancel_backend",
        "pg_create_restore_point",
        "pg_export_snapshot",
        "pg_notify",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_switch_wal",
        "pg_terminate_backend",
        "release_all_locks",
        "release_lock",
        "set_config",
        "setval",
        "sleep",
        "sys_eval",
        "sys_exec",
        "writefile",
    }
)
_SIDE_EFFECTING_FUNCTION_PREFIXES = ("dblink_", "pg_advisory_")
_EXTERNAL_PACKAGE_QUALIFIERS = frozenset(
    {
        "dbms_alert",
        "dbms_aq",
        "dbms_aqadm",
        "dbms_java",
        "dbms_job",
        "dbms_lock",
        "dbms_pipe",
        "dbms_scheduler",
        "dbms_sql",
        "utl_file",
        "utl_http",
        "utl_inaddr",
        "utl_smtp",
        "utl_tcp",
    }
)


def install_agent_read_only_firewall(engine: Engine) -> None:
    """Attach an event guard before any discovery connection is opened."""

    dialect = _DIALECTS.get(engine.dialect.name, engine.dialect.name)

    @event.listens_for(engine, "before_cursor_execute")
    def guard_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        context: object,
        _executemany: bool,
    ) -> None:
        execution_options = getattr(context, "execution_options", {})
        if execution_options.get("plantain_internal_readonly_setup"):
            _assert_internal_readonly_setup(statement)
            return
        assert_agent_read_only(statement, dialect=dialect)


def assert_agent_read_only(statement: str, *, dialect: str | None = None) -> None:
    """Allow reflection reads and SELECT queries; reject all mutation and multi-SQL."""

    _read_only_expression(statement, dialect=dialect)


def assert_agent_query(statement: str, *, dialect: str | None = None) -> None:
    """Prove an agent query is one non-locking, side-effect-free SELECT."""

    _query_expression(statement, dialect=dialect)


def prepare_agent_query(
    statement: str,
    *,
    dialect: str | None,
    row_limit: int,
) -> str:
    """Validate a query and quietly add a root result limit when absent."""

    if isinstance(row_limit, bool) or row_limit < 1:
        raise DatabasePolicyError("Database row limit must be a positive integer")
    expression = _query_expression(statement, dialect=dialect)
    if expression.args.get("limit") is not None:
        return statement
    bounded = expression.copy().limit(row_limit)
    return _render_with_named_binds(bounded, dialect=dialect)


def _read_only_expression(
    statement: str,
    *,
    dialect: str | None,
) -> exp.Expr:
    if len(statement) > MAX_DATABASE_STATEMENT_LENGTH:
        raise DatabasePolicyError("Database statement exceeds the safety limit")
    if not _READ_PREFIX.match(statement):
        raise DatabasePolicyError("Agent database access permits metadata reads and SELECT only")
    try:
        expressions = parse(statement, read=dialect)
    except (ParseError, ValueError) as exc:
        raise DatabasePolicyError("Database statement could not be proven read-only") from exc
    if len(expressions) != 1:
        raise DatabasePolicyError("Multiple database statements are prohibited")
    expression = expressions[0]
    if expression is None:
        raise DatabasePolicyError("Empty database statements are prohibited")
    if any(expression.find(node_type) is not None for node_type in _MUTATING_NODES):
        # SQLGlot represents several dialect-specific SHOW/DESCRIBE forms as
        # Command, which are admitted only by their exact leading verb below.
        leading = statement.lstrip().split(None, 1)[0].casefold()
        if leading not in {"show", "describe", "desc"}:
            raise DatabasePolicyError("Database mutation is prohibited for agent access")
        forbidden = (
            exp.Alter,
            exp.Create,
            exp.Delete,
            exp.Drop,
            exp.Insert,
            exp.Into,
            exp.Merge,
            exp.TruncateTable,
            exp.Update,
        )
        if any(expression.find(node_type) is not None for node_type in forbidden):
            raise DatabasePolicyError("Database mutation is prohibited for agent access")
    return expression


def _query_expression(statement: str, *, dialect: str | None) -> exp.Query:
    if not _QUERY_PREFIX.match(statement):
        raise DatabasePolicyError("queryDatabase permits SELECT statements only")
    expression = _read_only_expression(statement, dialect=dialect)
    if not isinstance(expression, exp.Query):
        raise DatabasePolicyError("queryDatabase permits SELECT statements only")
    if _LOCKING_READ.search(statement):
        raise DatabasePolicyError("Locking SELECT statements are prohibited")
    _reject_side_effecting_features(expression, dialect=dialect)
    return expression


def _reject_side_effecting_features(
    expression: exp.Query,
    *,
    dialect: str | None,
) -> None:
    for function in expression.find_all(exp.Func):
        qualified = _qualified_function_name(function, dialect=dialect)
        parts = qualified.split(".")
        leaf = parts[-1]
        if (
            leaf in _SIDE_EFFECTING_FUNCTIONS
            or leaf.startswith(_SIDE_EFFECTING_FUNCTION_PREFIXES)
            or _EXTERNAL_PACKAGE_QUALIFIERS.intersection(parts[:-1])
        ):
            raise DatabasePolicyError(
                "Side-effecting or external database functions are prohibited"
            )
    if any(column.name.casefold() == "nextval" for column in expression.find_all(exp.Column)):
        raise DatabasePolicyError("Sequence mutation is prohibited for agent access")
    if any(type(node).__name__ == "NextValueFor" for node in expression.walk()):
        raise DatabasePolicyError("Sequence mutation is prohibited for agent access")


def _qualified_function_name(
    function: exp.Func,
    *,
    dialect: str | None,
) -> str:
    current: exp.Expr = function
    while isinstance(current.parent, exp.Dot):
        current = current.parent
    head = current.sql(dialect=dialect).split("(", 1)[0]
    return re.sub(r'["`\[\]\s]', "", head).casefold()


def _render_with_named_binds(
    expression: exp.Query,
    *,
    dialect: str | None,
) -> str:
    replacements: list[tuple[str, str]] = []
    nonce = secrets.token_hex(16)
    for index, placeholder in enumerate(expression.find_all(exp.Placeholder)):
        token = f"plantain_{nonce}_{index}"
        replacements.append((token, placeholder.name))
        placeholder.set("this", token)
    rendered = expression.sql(dialect=dialect)
    for token, name in replacements:
        for marker in (f"%({token})s", f":{token}"):
            if marker in rendered:
                rendered = rendered.replace(marker, f":{name}", 1)
                break
        else:
            raise DatabasePolicyError("Database query bind syntax could not be preserved safely")
    return rendered


def _assert_internal_readonly_setup(statement: str) -> None:
    normalized = " ".join(statement.strip().split()).casefold()
    exact_statements = {
        "pragma query_only = on",
        "set transaction read only",
    }
    if normalized not in exact_statements and _INTERNAL_TIMEOUT_SETUP.fullmatch(normalized) is None:
        raise DatabasePolicyError("Unrecognized internal database safety statement")
