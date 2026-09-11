"""Strict JDBC parsing into credential-safe SQLAlchemy connection arguments."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import SplitResult, parse_qsl, unquote, urlsplit

from pydantic import ValidationError
from sqlalchemy import URL

from plantain.models.database import DatabaseSource

DEFAULT_POSTGRESQL_PORT = 5432
DEFAULT_SQL_SERVER_PORT = 1433
DEFAULT_MYSQL_PORT = 3306
DEFAULT_ORACLE_PORT = 1521
MAX_JDBC_PROPERTIES = 32
MAX_TCP_PORT = 65_535
MAX_HOST_LENGTH = 253
IPV6_VERSION = 6

_ORACLE_EASY_CONNECT = re.compile(
    r"^(?:(?P<protocol>tcps?)://)?(?P<host>\[[^]]+\]|[^:/]+):"
    r"(?P<port>[0-9]{1,5})/(?P<service>[^/?#;]+)$",
    re.IGNORECASE,
)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class DatabaseSourceError(ValueError):
    """Raised with a safe message when a database source is invalid."""


class DatabaseVendor(StrEnum):
    """First-class relational database implementations."""

    POSTGRESQL = "postgresql"
    SQL_SERVER = "sqlserver"
    MYSQL = "mysql"
    ORACLE = "oracle"


@dataclass(frozen=True, slots=True)
class DatabaseTlsPolicy:
    """Security policy used while translating JDBC transport settings."""

    environment: str
    allow_insecure_local_tls: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedDatabaseSource:
    """Native driver arguments with secrets excluded from repr and identity."""

    vendor: DatabaseVendor
    sqlalchemy_url: URL = field(repr=False)
    port: int = field(repr=False)
    connect_args: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )
    target_identity: str = field(repr=False, default="")
    host: str = field(repr=False, default="")
    database: str = field(repr=False, default="")


def resolve_database_source(
    source: DatabaseSource,
    *,
    tls_policy: DatabaseTlsPolicy,
    read_only_intent: bool = True,
) -> ResolvedDatabaseSource:
    """Translate one supported JDBC URL without rendering its credentials."""

    raw = source.db_url.strip()
    if raw != source.db_url or _CONTROL_CHARACTER.search(raw):
        raise DatabaseSourceError("Database dbUrl contains prohibited whitespace or controls")
    password = source.password.get_secret_value()
    if not password:
        raise DatabaseSourceError("Database password cannot be empty")
    if raw.startswith("jdbc:postgresql://"):
        return _postgresql(source.username, password, raw, tls_policy)
    if raw.startswith("jdbc:sqlserver://"):
        return _sql_server(
            source.username,
            password,
            raw,
            tls_policy,
            read_only_intent=read_only_intent,
        )
    if raw.startswith("jdbc:mysql://"):
        return _mysql(source.username, password, raw, tls_policy)
    if raw.startswith("jdbc:oracle:thin:@"):
        return _oracle(source.username, password, raw, tls_policy)
    raise DatabaseSourceError(
        "Unsupported database protocol; expected PostgreSQL, SQL Server, MySQL, or Oracle JDBC"
    )


def database_source_from_environment(
    environment: Mapping[str, str] | None = None,
) -> DatabaseSource:
    """Load the human CLI source from fixed environment variable names."""

    values = os.environ if environment is None else environment
    names = ("APP_DB_USERNAME", "APP_DB_PASSWORD", "APP_DB_URL")
    missing = [name for name in names if not values.get(name, "").strip()]
    if missing:
        raise DatabaseSourceError(
            "Human database command requires environment variables: " + ", ".join(missing)
        )
    try:
        return DatabaseSource.model_validate(
            {
                "username": values["APP_DB_USERNAME"],
                "password": values["APP_DB_PASSWORD"],
                "dbUrl": values["APP_DB_URL"],
            }
        )
    except ValidationError as exc:
        raise DatabaseSourceError("Human database environment source is invalid") from exc


def _postgresql(
    username: str,
    password: str,
    raw: str,
    tls_policy: DatabaseTlsPolicy,
) -> ResolvedDatabaseSource:
    parsed = _split_network_url(raw.removeprefix("jdbc:"), "PostgreSQL")
    host = _hostname(parsed, "PostgreSQL")
    properties = _query_properties(parsed.query, "PostgreSQL")
    allowed = {
        "application_name",
        "connect_timeout",
        "sslcert",
        "sslkey",
        "sslmode",
        "sslrootcert",
    }
    _reject_unknown(properties, allowed, "PostgreSQL")
    secure_mode = properties.get("sslmode")
    if secure_mode is None:
        properties["sslmode"] = "verify-full"
    elif secure_mode.casefold() != "verify-full":
        if not _is_loopback(host):
            raise DatabaseSourceError("Remote PostgreSQL requires sslmode=verify-full")
        _require_local_insecure_opt_in(host, tls_policy)
    properties.setdefault("application_name", "plantain")
    database = _database_path(parsed.path, "PostgreSQL")
    port = _port(parsed, DEFAULT_POSTGRESQL_PORT, "PostgreSQL")
    url = URL.create(
        "postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
        query=properties,
    )
    return _resolved(
        DatabaseVendor.POSTGRESQL,
        url,
        host=host,
        port=port,
        database=database,
    )


def _sql_server(
    username: str,
    password: str,
    raw: str,
    tls_policy: DatabaseTlsPolicy,
    *,
    read_only_intent: bool,
) -> ResolvedDatabaseSource:
    payload = raw.removeprefix("jdbc:sqlserver://")
    authority, separator, property_text = payload.partition(";")
    parsed = _split_network_url(f"mssql://{authority}", "SQL Server")
    host = _hostname(parsed, "SQL Server")
    properties = _semicolon_properties(property_text, "SQL Server") if separator else {}
    allowed = {
        "applicationintent",
        "databasename",
        "encrypt",
        "hostnameincertificate",
        "logintimeout",
        "trustservercertificate",
    }
    _reject_unknown(properties, allowed, "SQL Server")
    database = properties.get("databasename", "").strip()
    if not database:
        raise DatabaseSourceError("SQL Server JDBC dbUrl requires databaseName")
    encrypt = properties.get("encrypt", "true").casefold()
    if encrypt not in {"true", "yes", "mandatory", "strict"}:
        if not _is_loopback(host):
            raise DatabaseSourceError("Remote SQL Server requires encrypted transport")
        _require_local_insecure_opt_in(host, tls_policy)
    trust = properties.get("trustservercertificate", "false").casefold()
    if trust not in {"true", "false", "yes", "no"}:
        raise DatabaseSourceError("SQL Server trustServerCertificate must be true or false")
    if trust in {"true", "yes"}:
        _require_local_insecure_opt_in(host, tls_policy)
    query: dict[str, str] = {
        "driver": "ODBC Driver 18 for SQL Server",
        "Encrypt": _sql_server_encrypt_value(encrypt),
        "TrustServerCertificate": "yes" if trust in {"true", "yes"} else "no",
    }
    if read_only_intent:
        query["ApplicationIntent"] = "ReadOnly"
    if host_certificate := properties.get("hostnameincertificate"):
        query["HostNameInCertificate"] = host_certificate
    if login_timeout := properties.get("logintimeout"):
        query["LoginTimeout"] = _positive_integer_string(login_timeout, "loginTimeout")
    port = _port(parsed, DEFAULT_SQL_SERVER_PORT, "SQL Server")
    url = URL.create(
        "mssql+pyodbc",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
        query=query,
    )
    return _resolved(
        DatabaseVendor.SQL_SERVER,
        url,
        host=host,
        port=port,
        database=database,
    )


def _mysql(
    username: str,
    password: str,
    raw: str,
    tls_policy: DatabaseTlsPolicy,
) -> ResolvedDatabaseSource:
    parsed = _split_network_url(raw.removeprefix("jdbc:"), "MySQL")
    host = _hostname(parsed, "MySQL")
    properties = _query_properties(parsed.query, "MySQL")
    allowed = {
        "connecttimeout",
        "requiressl",
        "sslca",
        "sslcert",
        "sslkey",
        "sslmode",
        "usessl",
    }
    _reject_unknown(properties, allowed, "MySQL")
    mode = properties.get("sslmode", "").replace("-", "_").upper()
    legacy_disabled = any(
        properties.get(name, "").casefold() == "false" for name in ("usessl", "requiressl")
    )
    loopback = _is_loopback(host)
    insecure = legacy_disabled or mode in {"DISABLED", "PREFERRED"}
    if insecure:
        if not loopback:
            raise DatabaseSourceError("Remote MySQL requires verified TLS")
        _require_local_insecure_opt_in(host, tls_policy)
    connect_args: dict[str, object] = {}
    if not loopback or not insecure:
        if mode and mode != "VERIFY_IDENTITY":
            raise DatabaseSourceError("MySQL sslMode must be VERIFY_IDENTITY")
        connect_args.update(
            {
                "ssl_disabled": False,
                "ssl_verify_cert": True,
                "ssl_verify_identity": True,
            }
        )
    for jdbc_name, native_name in (
        ("sslca", "ssl_ca"),
        ("sslcert", "ssl_cert"),
        ("sslkey", "ssl_key"),
    ):
        if value := properties.get(jdbc_name):
            connect_args[native_name] = value
    if timeout := properties.get("connecttimeout"):
        milliseconds = int(_positive_integer_string(timeout, "connectTimeout"))
        connect_args["connection_timeout"] = max(1, (milliseconds + 999) // 1_000)
    database = _database_path(parsed.path, "MySQL")
    port = _port(parsed, DEFAULT_MYSQL_PORT, "MySQL")
    url = URL.create(
        "mysql+mysqlconnector",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
        query={"charset": "utf8mb4"},
    )
    return _resolved(
        DatabaseVendor.MYSQL,
        url,
        host=host,
        port=port,
        database=database,
        connect_args=connect_args,
    )


def _oracle(
    username: str,
    password: str,
    raw: str,
    tls_policy: DatabaseTlsPolicy,
) -> ResolvedDatabaseSource:
    payload = raw.removeprefix("jdbc:oracle:thin:@")
    match = _ORACLE_EASY_CONNECT.fullmatch(payload)
    if match is None:
        raise DatabaseSourceError(
            "Oracle JDBC dbUrl must use host:port/service or tcps://host:port/service"
        )
    host = match.group("host").removeprefix("[").removesuffix("]")
    _validate_host(host, "Oracle")
    port = _validated_port(int(match.group("port")), "Oracle")
    service = unquote(match.group("service"))
    if not service or _CONTROL_CHARACTER.search(service):
        raise DatabaseSourceError("Oracle service name is invalid")
    protocol = (match.group("protocol") or "tcp").casefold()
    if protocol != "tcps":
        _require_local_insecure_opt_in(host, tls_policy)
    dsn = f"{protocol}://{_bracket_ipv6(host)}:{port}/{service}"
    url = URL.create(
        "oracle+oracledb",
        username=username,
        password=password,
    )
    return _resolved(
        DatabaseVendor.ORACLE,
        url,
        host=host,
        port=port,
        database=service,
        connect_args={"dsn": dsn},
    )


def _resolved(
    vendor: DatabaseVendor,
    url: URL,
    *,
    host: str,
    port: int,
    database: str,
    connect_args: Mapping[str, object] | None = None,
) -> ResolvedDatabaseSource:
    identity = f"{vendor.value}\0{host.casefold()}\0{port}\0{database}"
    return ResolvedDatabaseSource(
        vendor=vendor,
        sqlalchemy_url=url,
        connect_args=MappingProxyType(dict(connect_args or {})),
        target_identity=identity,
        host=host,
        port=port,
        database=database,
    )


def _split_network_url(value: str, label: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DatabaseSourceError(f"{label} JDBC dbUrl has an invalid network address") from exc
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise DatabaseSourceError(
            f"{label} JDBC dbUrl requires a host and must not embed credentials"
        )
    if parsed.fragment:
        raise DatabaseSourceError(f"{label} JDBC dbUrl cannot contain a fragment")
    _validate_host(parsed.hostname, label)
    if port is not None:
        _validated_port(port, label)
    return parsed


def _hostname(parsed: SplitResult, label: str) -> str:
    host = parsed.hostname
    if host is None:
        raise DatabaseSourceError(f"{label} JDBC dbUrl requires a host")
    return host


def _port(parsed: object, default: int, label: str) -> int:
    value = getattr(parsed, "port", None)
    return _validated_port(default if value is None else int(value), label)


def _validated_port(port: int, label: str) -> int:
    if port < 1 or port > MAX_TCP_PORT:
        raise DatabaseSourceError(f"{label} JDBC dbUrl has an invalid port")
    return port


def _validate_host(host: str, label: str) -> None:
    if not host or len(host) > MAX_HOST_LENGTH or _CONTROL_CHARACTER.search(host):
        raise DatabaseSourceError(f"{label} JDBC dbUrl has an invalid host")


def _database_path(path: str, label: str) -> str:
    database = unquote(path.removeprefix("/"))
    if not database or "/" in database or _CONTROL_CHARACTER.search(database):
        raise DatabaseSourceError(f"{label} JDBC dbUrl requires one database name")
    return database


def _query_properties(query: str, label: str) -> dict[str, str]:
    if not query:
        return {}
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            max_num_fields=MAX_JDBC_PROPERTIES + 1,
        )
    except ValueError as exc:
        raise DatabaseSourceError(f"{label} JDBC dbUrl properties are invalid") from exc
    if len(pairs) > MAX_JDBC_PROPERTIES:
        raise DatabaseSourceError(f"{label} JDBC dbUrl has too many properties")
    result: dict[str, str] = {}
    for raw_name, value in pairs:
        name = raw_name.casefold()
        if not name or name in result:
            raise DatabaseSourceError(f"{label} JDBC dbUrl has duplicate properties")
        result[name] = value
    return result


def _semicolon_properties(value: str, label: str) -> dict[str, str]:
    segments = value.split(";")
    if segments and not segments[-1]:
        segments.pop()
    if len(segments) > MAX_JDBC_PROPERTIES:
        raise DatabaseSourceError(f"{label} JDBC dbUrl has too many properties")
    result: dict[str, str] = {}
    for segment in segments:
        name, separator, property_value = segment.partition("=")
        key = name.strip().casefold()
        if not separator or not key or not property_value.strip() or key in result:
            raise DatabaseSourceError(f"{label} JDBC dbUrl has malformed properties")
        result[key] = property_value.strip()
    return result


def _reject_unknown(properties: Mapping[str, str], allowed: set[str], label: str) -> None:
    if set(properties) - allowed:
        raise DatabaseSourceError(f"{label} JDBC dbUrl contains unsupported properties")


def _positive_integer_string(value: str, name: str) -> str:
    if not value.isdecimal() or int(value) < 1:
        raise DatabaseSourceError(f"Database property {name} must be a positive integer")
    return value


def _sql_server_encrypt_value(value: str) -> str:
    if value == "strict":
        return "strict"
    return "no" if value in {"false", "no"} else "yes"


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_local_insecure_opt_in(host: str, policy: DatabaseTlsPolicy) -> None:
    if (
        policy.environment.casefold() != "local"
        or not policy.allow_insecure_local_tls
        or not _is_loopback(host)
    ):
        raise DatabaseSourceError(
            "Insecure database TLS is permitted only for explicit loopback local development"
        )


def _bracket_ipv6(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == IPV6_VERSION else host
    except ValueError:
        return host


__all__ = [
    "DatabaseSourceError",
    "DatabaseTlsPolicy",
    "DatabaseVendor",
    "ResolvedDatabaseSource",
    "database_source_from_environment",
    "resolve_database_source",
]
