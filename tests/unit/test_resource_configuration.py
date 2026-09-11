"""Global resource-admission configuration tests."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from plantain.config import Settings
from plantain.errors import ConfigurationError
from plantain.persistence import PRIVATE_DIRECTORY_MODE

EXPECTED_LIMITS = (3, 2, 7, 5, 6, 2)
EXPECTED_SCHEMA_LIMITS = (7.5, 3, 256)
EXPECTED_SNAPSHOT_BYTES = 8_388_608
EXPECTED_SNAPSHOT_WORKING_BYTES = 2_097_152
EXPECTED_NETWORK_CORRELATIONS = 4_096
EXPECTED_TRACE_RETENTION = (3, 4, 8_388_608)


def test_settings_load_global_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANTAIN_MAX_SCENARIO_CONCURRENCY", "3")
    monkeypatch.setenv("PLANTAIN_MAX_BROWSER_SESSIONS", "2")
    monkeypatch.setenv("PLANTAIN_MAX_API_REQUESTS", "7")
    monkeypatch.setenv("PLANTAIN_MAX_DATABASE_OPERATIONS", "5")
    monkeypatch.setenv("PLANTAIN_MAX_WORKER_THREADS", "6")
    monkeypatch.setenv("PLANTAIN_MAX_WORKER_PROCESSES", "2")
    monkeypatch.setenv(
        "PLANTAIN_SNAPSHOT_MAX_CAPTURE_BYTES",
        str(EXPECTED_SNAPSHOT_BYTES),
    )
    monkeypatch.setenv(
        "PLANTAIN_SNAPSHOT_WORKING_SET_BYTES",
        str(EXPECTED_SNAPSHOT_WORKING_BYTES),
    )
    monkeypatch.setenv(
        "PLANTAIN_SNAPSHOT_MAX_NETWORK_CORRELATIONS",
        str(EXPECTED_NETWORK_CORRELATIONS),
    )
    monkeypatch.setenv("PLANTAIN_SCHEMA_VALIDATION_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("PLANTAIN_SCHEMA_VALIDATION_CPU_SECONDS", "3")
    monkeypatch.setenv("PLANTAIN_SCHEMA_VALIDATION_MEMORY_MIB", "256")

    settings = Settings.from_env(tmp_path)

    assert (
        settings.max_scenario_concurrency,
        settings.max_browser_sessions,
        settings.max_api_requests,
        settings.max_database_operations,
        settings.max_worker_threads,
        settings.max_worker_processes,
    ) == EXPECTED_LIMITS
    assert settings.snapshot_max_capture_bytes == EXPECTED_SNAPSHOT_BYTES
    assert settings.snapshot_working_set_bytes == EXPECTED_SNAPSHOT_WORKING_BYTES
    assert settings.snapshot_max_network_correlations == EXPECTED_NETWORK_CORRELATIONS
    assert (
        settings.schema_validation_timeout_seconds,
        settings.schema_validation_cpu_seconds,
        settings.schema_validation_memory_mib,
    ) == EXPECTED_SCHEMA_LIMITS


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLANTAIN_MAX_SCENARIO_CONCURRENCY", "65"),
        ("PLANTAIN_MAX_BROWSER_SESSIONS", "33"),
        ("PLANTAIN_MAX_API_REQUESTS", "257"),
        ("PLANTAIN_MAX_DATABASE_OPERATIONS", "65"),
        ("PLANTAIN_DB_MAX_RESULT_BYTES", "67108865"),
        ("PLANTAIN_MAX_WORKER_THREADS", "65"),
        ("PLANTAIN_MAX_WORKER_PROCESSES", "17"),
        ("PLANTAIN_SNAPSHOT_MAX_CAPTURE_BYTES", "1099511627777"),
        ("PLANTAIN_SNAPSHOT_WORKING_SET_BYTES", "67108865"),
        ("PLANTAIN_SNAPSHOT_MAX_NETWORK_CORRELATIONS", "1000001"),
        ("PLANTAIN_SCHEMA_VALIDATION_TIMEOUT_SECONDS", "121"),
        ("PLANTAIN_SCHEMA_VALIDATION_CPU_SECONDS", "61"),
        ("PLANTAIN_SCHEMA_VALIDATION_MEMORY_MIB", "2049"),
        ("PLANTAIN_TRACE_RETENTION_DAYS", "366"),
        ("PLANTAIN_TRACE_MAX_ARCHIVES", "1001"),
        ("PLANTAIN_TRACE_MAX_BYTES", "10737418241"),
        ("PLANTAIN_SCENARIO_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_STEP_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_CLEANUP_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_SLOW_MO_MS", "86400001"),
        ("PLANTAIN_VIEWPORT_WIDTH", "16385"),
        ("PLANTAIN_VIEWPORT_HEIGHT", "16385"),
        ("PLANTAIN_ACTION_TIMEOUT_MS", "86400001"),
        ("PLANTAIN_NAVIGATION_TIMEOUT_MS", "86400001"),
        ("PLANTAIN_BROWSER_INSTALL_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_API_CONNECT_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_API_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_API_MAX_RESPONSE_BYTES", "67108865"),
        ("PLANTAIN_OPENAPI_MAX_BYTES", "67108865"),
        ("PLANTAIN_DB_QUERY_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_DB_MAX_ROWS", "10001"),
        ("PLANTAIN_DB_MAX_COLUMNS", "10001"),
        ("PLANTAIN_DB_POOL_SIZE", "33"),
        ("PLANTAIN_DB_MAX_OVERFLOW", "33"),
        ("PLANTAIN_DB_POOL_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_DB_POOL_RECYCLE_SECONDS", "31536001"),
        ("PLANTAIN_DB_MAX_SOURCE_POOLS", "33"),
        ("PLANTAIN_DB_MAX_SOURCES_PER_SCENARIO", "17"),
        ("PLANTAIN_DB_FETCH_BATCH_SIZE", "10001"),
        ("PLANTAIN_ZEPHYR_TIMEOUT_SECONDS", "86401"),
        ("PLANTAIN_ZEPHYR_MAX_RESPONSE_BYTES", "67108865"),
        ("PLANTAIN_ZEPHYR_MAX_ATTACHMENT_BYTES", "67108865"),
        ("PLANTAIN_ZEPHYR_MAX_CONNECTIONS", "257"),
    ],
)
def test_settings_reject_resource_limits_above_hard_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env(tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("yaml_max_bytes", 67_108_865),
        ("yaml_max_nodes", 1_000_001),
        ("yaml_max_depth", 257),
    ],
)
def test_direct_yaml_settings_reject_excessive_bounds(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    settings = Settings.from_env(tmp_path)

    with pytest.raises(ConfigurationError, match=field):
        replace(settings, **{field: value})


def test_settings_reject_snapshot_working_set_above_capture_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANTAIN_SNAPSHOT_MAX_CAPTURE_BYTES", "1048576")
    monkeypatch.setenv("PLANTAIN_SNAPSHOT_WORKING_SET_BYTES", "2097152")

    with pytest.raises(
        ConfigurationError,
        match="PLANTAIN_SNAPSHOT_WORKING_SET_BYTES",
    ):
        Settings.from_env(tmp_path)


def test_enabled_tracing_requires_governance_and_encrypted_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANTAIN_TRACE_MODE", "on")
    with pytest.raises(ConfigurationError, match="TRACE_DATA_GOVERNANCE_APPROVED"):
        Settings.from_env(tmp_path)

    monkeypatch.setenv("PLANTAIN_TRACE_DATA_GOVERNANCE_APPROVED", "true")
    with pytest.raises(ConfigurationError, match="TRACE_STORAGE_ENCRYPTED"):
        Settings.from_env(tmp_path)

    monkeypatch.setenv("PLANTAIN_TRACE_STORAGE_ENCRYPTED", "true")
    monkeypatch.setenv("PLANTAIN_TRACE_RETENTION_DAYS", "3")
    monkeypatch.setenv("PLANTAIN_TRACE_MAX_ARCHIVES", "4")
    monkeypatch.setenv("PLANTAIN_TRACE_MAX_BYTES", "8388608")
    settings = Settings.from_env(tmp_path)
    assert (
        settings.trace_retention_days,
        settings.trace_max_archives,
        settings.trace_max_bytes,
    ) == EXPECTED_TRACE_RETENTION


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_settings_create_and_repair_private_runtime_directories(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path)
    settings.output_dir.mkdir()
    settings.output_dir.chmod(0o777)

    settings.ensure_runtime_directories()

    for directory in (
        settings.output_dir,
        settings.snapshots_dir,
        settings.generated_dir,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == PRIVATE_DIRECTORY_MODE
