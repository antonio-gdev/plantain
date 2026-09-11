"""Complete offline security-boundary coverage for database source resolution."""

from __future__ import annotations

from urllib.parse import SplitResult

import pytest
from pydantic import ValidationError

from plantain.activities.database import source as database_source
from plantain.activities.database.source import (
    DatabaseSourceError,
    DatabaseTlsPolicy,
    DatabaseVendor,
    _bracket_ipv6,
    _database_path,
    _hostname,
    _is_loopback,
    _positive_integer_string,
    _query_properties,
    _semicolon_properties,
    _sql_server_encrypt_value,
    _validated_port,
    resolve_database_source,
)
from plantain.models.database import DatabaseSource

DUMMY_CREDENTIAL = "synthetic-database-credential"
PRODUCTION_TLS = DatabaseTlsPolicy(environment="production")


def _source(db_url: str, *, credential: str = DUMMY_CREDENTIAL) -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "automation_reader",
            "password": credential,
            "dbUrl": db_url,
        }
    )


@pytest.mark.parametrize(
    "db_url",
    [
        " jdbc:postgresql://db.example.test/app",
        "jdbc:postgresql://db.example.test/app\n",
    ],
)
def test_source_model_rejects_ambiguous_url_before_normalization(db_url: str) -> None:
    with pytest.raises(ValidationError, match="whitespace or controls"):
        _source(db_url)


def test_source_rejects_unsupported_protocol() -> None:
    with pytest.raises(DatabaseSourceError, match="Unsupported database protocol"):
        resolve_database_source(
            _source("postgresql://db.example.test/app"),
            tls_policy=PRODUCTION_TLS,
        )


def test_source_rejects_empty_database_credential() -> None:
    with pytest.raises(DatabaseSourceError, match="password cannot be empty"):
        resolve_database_source(
            _source("jdbc:postgresql://db.example.test/app", credential=""),
            tls_policy=PRODUCTION_TLS,
        )


def test_postgresql_preserves_supported_properties_and_defaults() -> None:
    resolved = resolve_database_source(
        _source(
            "jdbc:postgresql://db.example.test/clothing?"
            "sslmode=verify-full&application_name=qa&connect_timeout=4"
        ),
        tls_policy=PRODUCTION_TLS,
    )

    assert resolved.vendor is DatabaseVendor.POSTGRESQL
    assert resolved.sqlalchemy_url.port == database_source.DEFAULT_POSTGRESQL_PORT
    assert resolved.sqlalchemy_url.query == {
        "sslmode": "verify-full",
        "application_name": "qa",
        "connect_timeout": "4",
    }


def test_postgresql_decodes_one_database_name() -> None:
    resolved = resolve_database_source(
        _source("jdbc:postgresql://db.example.test/clothing%20catalog"),
        tls_policy=PRODUCTION_TLS,
    )

    assert resolved.database == "clothing catalog"
    with pytest.raises(DatabaseSourceError, match="one database name"):
        resolve_database_source(
            _source("jdbc:postgresql://db.example.test/one/two"),
            tls_policy=PRODUCTION_TLS,
        )


def test_sql_server_optional_properties_and_non_read_only_intent() -> None:
    resolved = resolve_database_source(
        _source(
            "jdbc:sqlserver://sql.example.test;databaseName=clothing;"
            "encrypt=yes;trustServerCertificate=no;"
            "hostNameInCertificate=db.internal.test;loginTimeout=12"
        ),
        tls_policy=PRODUCTION_TLS,
        read_only_intent=False,
    )

    assert resolved.sqlalchemy_url.port == database_source.DEFAULT_SQL_SERVER_PORT
    assert resolved.sqlalchemy_url.query == {
        "driver": "ODBC Driver 18 for SQL Server",
        "Encrypt": "yes",
        "TrustServerCertificate": "no",
        "HostNameInCertificate": "db.internal.test",
        "LoginTimeout": "12",
    }


@pytest.mark.parametrize("encrypt, expected", [("strict", "strict"), ("mandatory", "yes")])
def test_sql_server_normalizes_secure_encrypt_values(encrypt: str, expected: str) -> None:
    resolved = resolve_database_source(
        _source(
            "jdbc:sqlserver://sql.example.test;databaseName=clothing;"
            f"encrypt={encrypt};trustServerCertificate=false"
        ),
        tls_policy=PRODUCTION_TLS,
    )

    assert resolved.sqlalchemy_url.query["Encrypt"] == expected


@pytest.mark.parametrize(
    "db_url, message",
    [
        (
            "jdbc:sqlserver://sql.example.test;encrypt=true",
            "requires databaseName",
        ),
        (
            "jdbc:sqlserver://sql.example.test;databaseName=app;trustServerCertificate=maybe",
            "must be true or false",
        ),
        (
            "jdbc:sqlserver://sql.example.test;databaseName=app;loginTimeout=0",
            "positive integer",
        ),
        (
            "jdbc:sqlserver://sql.example.test;databaseName=app;loginTimeout=abc",
            "positive integer",
        ),
    ],
)
def test_sql_server_rejects_invalid_properties(db_url: str, message: str) -> None:
    with pytest.raises(DatabaseSourceError, match=message):
        resolve_database_source(_source(db_url), tls_policy=PRODUCTION_TLS)


def test_mysql_maps_certificates_and_rounds_millisecond_timeout() -> None:
    resolved = resolve_database_source(
        _source(
            "jdbc:mysql://mysql.example.test/clothing?sslMode=VERIFY_IDENTITY&"
            "sslCa=%2Fcerts%2Fca.pem&sslCert=%2Fcerts%2Fclient.pem&"
            "sslKey=%2Fcerts%2Fclient.key&connectTimeout=1001"
        ),
        tls_policy=PRODUCTION_TLS,
    )

    assert resolved.sqlalchemy_url.port == database_source.DEFAULT_MYSQL_PORT
    assert resolved.sqlalchemy_url.query == {"charset": "utf8mb4"}
    assert resolved.connect_args == {
        "ssl_disabled": False,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
        "ssl_ca": "/certs/ca.pem",
        "ssl_cert": "/certs/client.pem",
        "ssl_key": "/certs/client.key",
        "connection_timeout": 2,
    }


def test_mysql_secure_defaults_do_not_require_explicit_ssl_mode() -> None:
    resolved = resolve_database_source(
        _source("jdbc:mysql://mysql.example.test/clothing?requireSSL=true"),
        tls_policy=PRODUCTION_TLS,
    )

    assert resolved.connect_args["ssl_verify_identity"] is True
    with pytest.raises(DatabaseSourceError, match="must be VERIFY_IDENTITY"):
        resolve_database_source(
            _source("jdbc:mysql://mysql.example.test/clothing?sslMode=REQUIRED"),
            tls_policy=PRODUCTION_TLS,
        )


def test_oracle_supports_tcps_ipv6_and_rejects_unsafe_service() -> None:
    resolved = resolve_database_source(
        _source("jdbc:oracle:thin:@tcps://[::1]:1521/clothing"),
        tls_policy=PRODUCTION_TLS,
    )

    assert resolved.connect_args["dsn"] == "tcps://[::1]:1521/clothing"
    with pytest.raises(DatabaseSourceError, match="service name is invalid"):
        resolve_database_source(
            _source("jdbc:oracle:thin:@tcps://oracle.example.test:1521/app%0Aname"),
            tls_policy=PRODUCTION_TLS,
        )


@pytest.mark.parametrize(
    "db_url",
    [
        "jdbc:oracle:thin:@oracle.example.test/app",
        "jdbc:oracle:thin:@(DESCRIPTION=unsafe)",
    ],
)
def test_oracle_rejects_unsupported_connect_descriptor(db_url: str) -> None:
    with pytest.raises(DatabaseSourceError, match="host:port/service"):
        resolve_database_source(_source(db_url), tls_policy=PRODUCTION_TLS)


@pytest.mark.parametrize(
    "db_url, message",
    [
        ("jdbc:postgresql://db.example.test:invalid/app", "invalid network address"),
        ("jdbc:postgresql://[invalid/app", "invalid network address"),
        ("jdbc:postgresql://db.example.test/app#fragment", "cannot contain a fragment"),
        ("jdbc:postgresql:///app", "requires a host"),
        ("jdbc:postgresql://db.example.test/", "one database name"),
        ("jdbc:postgresql://db.example.test/app%0Aname", "one database name"),
    ],
)
def test_network_url_components_fail_closed(db_url: str, message: str) -> None:
    with pytest.raises(DatabaseSourceError, match=message):
        resolve_database_source(_source(db_url), tls_policy=PRODUCTION_TLS)


def test_host_length_and_property_count_are_bounded() -> None:
    long_host = "a" * (database_source.MAX_HOST_LENGTH + 1)
    with pytest.raises(DatabaseSourceError, match="invalid host"):
        resolve_database_source(
            _source(f"jdbc:postgresql://{long_host}/app"),
            tls_policy=PRODUCTION_TLS,
        )

    maximum_plus_one = "&".join(
        f"property{index}=value" for index in range(database_source.MAX_JDBC_PROPERTIES + 1)
    )
    with pytest.raises(DatabaseSourceError, match="too many properties"):
        _query_properties(maximum_plus_one, "Database")

    parser_overflow = maximum_plus_one + "&overflow=value"
    with pytest.raises(DatabaseSourceError, match="properties are invalid"):
        _query_properties(parser_overflow, "Database")


@pytest.mark.parametrize("query", ["=value", "sslmode=verify-full&SSLMODE=verify-full"])
def test_query_properties_reject_blank_and_duplicate_names(query: str) -> None:
    with pytest.raises(DatabaseSourceError, match="duplicate properties"):
        _query_properties(query, "Database")


def test_semicolon_properties_are_strict_and_bounded() -> None:
    assert _semicolon_properties("databaseName=app;encrypt=true;", "SQL Server") == {
        "databasename": "app",
        "encrypt": "true",
    }
    for value in ("databaseName", "databaseName=", "databaseName=app;DATABASEname=other"):
        with pytest.raises(DatabaseSourceError, match="malformed properties"):
            _semicolon_properties(value, "SQL Server")

    overflow = ";".join(
        f"property{index}=value" for index in range(database_source.MAX_JDBC_PROPERTIES + 1)
    )
    with pytest.raises(DatabaseSourceError, match="too many properties"):
        _semicolon_properties(overflow, "SQL Server")


def test_small_source_helpers_cover_safe_edge_cases() -> None:
    assert _positive_integer_string("7", "timeout") == "7"
    assert _sql_server_encrypt_value("false") == "no"
    assert _sql_server_encrypt_value("true") == "yes"
    assert _is_loopback(None) is False
    assert _is_loopback("localhost") is True
    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("db.example.test") is False
    assert _bracket_ipv6("::1") == "[::1]"
    assert _bracket_ipv6("db.example.test") == "db.example.test"
    assert _database_path("/app", "Database") == "app"
    assert (
        _hostname(
            SplitResult("postgresql", "db.example.test", "/app", "", ""),
            "Database",
        )
        == "db.example.test"
    )

    with pytest.raises(DatabaseSourceError, match="invalid port"):
        _validated_port(0, "Database")
    with pytest.raises(DatabaseSourceError, match="invalid port"):
        _validated_port(database_source.MAX_TCP_PORT + 1, "Database")
    with pytest.raises(DatabaseSourceError, match="requires a host"):
        _hostname(SplitResult("postgresql", "", "/app", "", ""), "Database")
