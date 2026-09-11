"""Strict JDBC-to-native-driver source resolution tests."""

from __future__ import annotations

import pytest

from plantain.activities.database.source import (
    DatabaseSourceError,
    DatabaseTlsPolicy,
    DatabaseVendor,
    database_source_from_environment,
    resolve_database_source,
)
from plantain.models.database import DatabaseSource

DUMMY_VALUE = "unit-test-value"
PRODUCTION_TLS = DatabaseTlsPolicy(environment="production")
LOCAL_INSECURE_TLS = DatabaseTlsPolicy(
    environment="local",
    allow_insecure_local_tls=True,
)


def _source(db_url: str, *, credential: str = DUMMY_VALUE) -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "automation_reader",
            "password": credential,
            "dbUrl": db_url,
        }
    )


def test_resolves_all_four_native_driver_urls_without_rendering_credentials() -> None:
    postgres = resolve_database_source(
        _source("jdbc:postgresql://postgres.example.test:5432/clothing"),
        tls_policy=PRODUCTION_TLS,
    )
    sql_server = resolve_database_source(
        _source(
            "jdbc:sqlserver://sql.example.test:1433;"
            "databaseName=clothing;encrypt=strict;trustServerCertificate=false"
        ),
        tls_policy=PRODUCTION_TLS,
    )
    mysql = resolve_database_source(
        _source("jdbc:mysql://mysql.example.test:3306/clothing?sslMode=VERIFY_IDENTITY"),
        tls_policy=PRODUCTION_TLS,
    )
    oracle = resolve_database_source(
        _source("jdbc:oracle:thin:@tcps://oracle.example.test:1521/clothing"),
        tls_policy=PRODUCTION_TLS,
    )

    assert postgres.vendor is DatabaseVendor.POSTGRESQL
    assert postgres.sqlalchemy_url.drivername == "postgresql+psycopg"
    assert postgres.sqlalchemy_url.query["sslmode"] == "verify-full"
    assert sql_server.vendor is DatabaseVendor.SQL_SERVER
    assert sql_server.sqlalchemy_url.drivername == "mssql+pyodbc"
    assert sql_server.sqlalchemy_url.query["ApplicationIntent"] == "ReadOnly"
    assert sql_server.sqlalchemy_url.query["TrustServerCertificate"] == "no"
    assert mysql.vendor is DatabaseVendor.MYSQL
    assert mysql.sqlalchemy_url.drivername == "mysql+mysqlconnector"
    assert mysql.connect_args["ssl_verify_identity"] is True
    assert oracle.vendor is DatabaseVendor.ORACLE
    assert oracle.sqlalchemy_url.drivername == "oracle+oracledb"
    assert oracle.connect_args["dsn"] == "tcps://oracle.example.test:1521/clothing"
    for resolved in (postgres, sql_server, mysql, oracle):
        assert DUMMY_VALUE not in repr(resolved)
        assert DUMMY_VALUE not in str(resolved.sqlalchemy_url)


@pytest.mark.parametrize(
    "db_url",
    [
        "jdbc:postgresql://127.0.0.1:5432/app?sslmode=disable",
        (
            "jdbc:sqlserver://127.0.0.1:1433;"
            "databaseName=app;encrypt=true;trustServerCertificate=true"
        ),
        "jdbc:mysql://127.0.0.1:3306/app?useSSL=false",
        "jdbc:oracle:thin:@127.0.0.1:1521/app",
    ],
)
def test_insecure_transport_requires_explicit_loopback_local_opt_in(db_url: str) -> None:
    with pytest.raises(DatabaseSourceError, match="loopback local development"):
        resolve_database_source(_source(db_url), tls_policy=PRODUCTION_TLS)

    resolved = resolve_database_source(_source(db_url), tls_policy=LOCAL_INSECURE_TLS)

    assert resolved.host == "127.0.0.1"


@pytest.mark.parametrize(
    "db_url",
    [
        "jdbc:postgresql://db.example.test/app?sslmode=require",
        (
            "jdbc:sqlserver://db.example.test:1433;"
            "databaseName=app;encrypt=false;trustServerCertificate=false"
        ),
        "jdbc:mysql://db.example.test/app?sslMode=PREFERRED",
        "jdbc:oracle:thin:@db.example.test:1521/app",
    ],
)
def test_remote_sources_cannot_disable_verified_transport(db_url: str) -> None:
    with pytest.raises(DatabaseSourceError):
        resolve_database_source(_source(db_url), tls_policy=PRODUCTION_TLS)


def test_embedded_credentials_and_unknown_properties_are_rejected_safely() -> None:
    embedded = "jdbc:postgresql://url_user:url_password@db.example.test/app"
    with pytest.raises(DatabaseSourceError) as embedded_error:
        resolve_database_source(_source(embedded), tls_policy=PRODUCTION_TLS)
    assert "url_password" not in str(embedded_error.value)

    unknown = "jdbc:postgresql://db.example.test/app?secretProperty=hidden-value"
    with pytest.raises(DatabaseSourceError) as property_error:
        resolve_database_source(_source(unknown), tls_policy=PRODUCTION_TLS)
    assert "hidden-value" not in str(property_error.value)


def test_environment_source_uses_only_fixed_names_and_reports_missing_names() -> None:
    source = database_source_from_environment(
        {
            "APP_DB_USERNAME": "reader",
            "APP_DB_PASSWORD": DUMMY_VALUE,
            "APP_DB_URL": "jdbc:postgresql://db.example.test/app",
            "UNRELATED_VALUE": "ignored",
        }
    )

    assert source.username == "reader"
    assert source.password.get_secret_value() == DUMMY_VALUE
    with pytest.raises(DatabaseSourceError, match="APP_DB_PASSWORD"):
        database_source_from_environment(
            {
                "APP_DB_USERNAME": "reader",
                "APP_DB_URL": "jdbc:postgresql://db.example.test/app",
            }
        )
