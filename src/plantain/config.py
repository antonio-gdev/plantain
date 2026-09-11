"""Runtime settings loaded exclusively from process environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from plantain.errors import ConfigurationError
from plantain.persistence import ensure_private_directory
from plantain.security.url_policy import (
    normalize_allowed_hosts,
    normalize_allowed_tcp_targets,
    normalize_blocked_hosts,
    normalize_network_mode,
)

SENSITIVE_KEYS_FILENAME = "sensitive-keys.txt"
MAX_SENSITIVE_KEYS_FILE_BYTES = 262_144
MAX_SENSITIVE_KEY_NAMES = 10_000
MAX_SENSITIVE_KEY_LENGTH = 256
MAX_OPERATION_TIMEOUT_SECONDS = 86_400.0
MAX_MILLISECOND_DURATION = 86_400_000
MAX_IN_MEMORY_DOCUMENT_BYTES = 67_108_864
MAX_VIEWPORT_DIMENSION = 16_384
MAX_SCENARIO_CONCURRENCY = 64
MAX_BROWSER_SESSIONS = 32
MAX_API_REQUESTS = 256
MAX_DATABASE_OPERATIONS = 64
MAX_DATABASE_RESULT_BYTES = 67_108_864
MAX_DATABASE_ROWS = 10_000
MAX_DATABASE_COLUMNS = 10_000
MAX_DATABASE_POOL_SIZE = 32
MAX_DATABASE_POOL_OVERFLOW = 32
MAX_DATABASE_SOURCE_POOLS = 32
MAX_DATABASE_SOURCES_PER_SCENARIO = 16
MAX_DATABASE_FETCH_BATCH_SIZE = 10_000
MAX_RESOURCE_LIFETIME_SECONDS = 31_536_000
MAX_WORKER_THREADS = 64
MAX_WORKER_PROCESSES = 16
MAX_SNAPSHOT_CAPTURE_BYTES = 1_099_511_627_776
MAX_SNAPSHOT_WORKING_SET_BYTES = 67_108_864
MAX_SNAPSHOT_NETWORK_CORRELATIONS = 1_000_000
MAX_SCHEMA_VALIDATION_TIMEOUT_SECONDS = 120.0
MAX_SCHEMA_VALIDATION_CPU_SECONDS = 60
MAX_SCHEMA_VALIDATION_MEMORY_MIB = 2_048
MAX_TRACE_RETENTION_DAYS = 365
MAX_TRACE_ARCHIVES = 1_000
MAX_TRACE_BYTES = 10_737_418_240
MAX_ZEPHYR_OUTBOX_ENTRIES = 100_000
MAX_ZEPHYR_OUTBOX_RETRY_BATCH_SIZE = 16
MAX_ZEPHYR_OUTBOX_LOCK_TIMEOUT_SECONDS = 60.0
MAX_REPORTING_CONNECTIONS = 256
MAX_YAML_BYTES = 67_108_864
MAX_YAML_NODES = 1_000_000
MAX_YAML_DEPTH = 256
_API_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = _env(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}")
    return value


def _float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    raw = _env(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}")
    return value


def _hosts(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    return normalize_allowed_hosts(values)


def _blocked_hosts(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    return normalize_blocked_hosts(values)


def _api_methods(value: str) -> tuple[str, ...]:
    requested = {item.strip().upper() for item in value.split(",") if item.strip()}
    if requested.difference(_API_METHODS):
        raise ConfigurationError("PLANTAIN_API_ALLOWED_METHODS contains an unsupported HTTP method")
    return tuple(method for method in _API_METHODS if method in requested)


def _tcp_targets(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    return normalize_allowed_tcp_targets(values)


def _load_sensitive_key_names(root: Path) -> tuple[str, ...]:
    """Load user-maintained field names without logging or exposing their contents."""

    configured = root / SENSITIVE_KEYS_FILENAME
    if not configured.exists():
        return ()
    try:
        target = configured.resolve(strict=True)
        target.relative_to(root)
        metadata = target.stat()
        if not target.is_file() or metadata.st_size > MAX_SENSITIVE_KEYS_FILE_BYTES:
            raise ConfigurationError(
                f"{SENSITIVE_KEYS_FILENAME} must be a regular file no larger than "
                f"{MAX_SENSITIVE_KEYS_FILE_BYTES} bytes"
            )
        contents = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ConfigurationError(
            f"{SENSITIVE_KEYS_FILENAME} must be a readable UTF-8 file inside the project root"
        ) from exc

    names: set[str] = set()
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        candidate = raw_line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if "\x00" in candidate or len(candidate) > MAX_SENSITIVE_KEY_LENGTH:
            raise ConfigurationError(
                f"{SENSITIVE_KEYS_FILENAME} contains an invalid entry at line {line_number}"
            )
        names.add(candidate.casefold())
        if len(names) > MAX_SENSITIVE_KEY_NAMES:
            raise ConfigurationError(
                f"{SENSITIVE_KEYS_FILENAME} exceeds {MAX_SENSITIVE_KEY_NAMES} unique entries"
            )
    return tuple(sorted(names, key=lambda item: (-len(item), item)))


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated process settings shared by one or more scenario runs."""

    project_root: Path
    environment: str
    log_level: str
    scenario_timeout_seconds: float
    step_timeout_seconds: float
    browser: str
    headless: bool
    slow_mo_ms: int
    viewport_width: int
    viewport_height: int
    action_timeout_ms: int
    navigation_timeout_ms: int
    trace_mode: str
    storage_state_path: Path | None
    allow_private_networks: bool
    allowed_hosts: tuple[str, ...]
    api_connect_timeout_seconds: float
    api_timeout_seconds: float
    api_max_response_bytes: int
    openapi_max_bytes: int
    sensitive_key_names: tuple[str, ...]
    db_query_timeout_seconds: float
    db_max_rows: int
    db_max_columns: int
    db_max_result_bytes: int
    allow_db_mutations: bool
    network_mode: str = "standard"
    blocked_hosts: tuple[str, ...] = ()
    api_allowed_methods: tuple[str, ...] = ()
    trace_data_governance_approved: bool = False
    trace_storage_encrypted: bool = False
    trace_retention_days: int = 7
    trace_max_archives: int = 20
    trace_max_bytes: int = 1_073_741_824
    allow_insecure_local_http: bool = False
    egress_control_enforced: bool = False
    cleanup_timeout_seconds: float = 15.0
    max_scenario_concurrency: int = 4
    max_browser_sessions: int = 2
    max_api_requests: int = 16
    max_database_operations: int = 8
    max_worker_threads: int = 8
    max_worker_processes: int = 2
    snapshot_max_capture_bytes: int = 536_870_912
    snapshot_working_set_bytes: int = 1_048_576
    snapshot_max_network_correlations: int = 10_000
    schema_validation_timeout_seconds: float = 10.0
    schema_validation_cpu_seconds: int = 5
    schema_validation_memory_mib: int = 512
    db_allowed_targets: tuple[str, ...] = ()
    db_allow_private_networks: bool = False
    browser_auto_install: bool = True
    browser_install_timeout_seconds: float = 300.0
    allow_custom_playwright_download_hosts: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 0
    db_pool_timeout_seconds: float = 5.0
    db_pool_recycle_seconds: int = 1_800
    db_max_source_pools: int = 8
    db_max_sources_per_scenario: int = 4
    db_fetch_batch_size: int = 100
    allow_insecure_local_db_tls: bool = False
    yaml_max_bytes: int = 2_000_000
    yaml_max_nodes: int = 50_000
    yaml_max_depth: int = 100
    allure_results_enabled: bool = False
    zephyr_base_url: str = ""
    zephyr_publish_results: bool = False
    zephyr_attach_report: bool = False
    zephyr_attachment_data_governance_approved: bool = False
    zephyr_allow_private_networks: bool = False
    zephyr_allow_insecure_http: bool = False
    zephyr_timeout_seconds: float = 15.0
    zephyr_max_response_bytes: int = 1_048_576
    zephyr_max_attachment_bytes: int = 5_242_880
    zephyr_max_connections: int = 4
    zephyr_outbox_max_entries: int = 1_000
    zephyr_outbox_retry_batch_size: int = 1
    zephyr_outbox_lock_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        yaml_bounds = (
            (self.yaml_max_bytes, MAX_YAML_BYTES, "yaml_max_bytes"),
            (self.yaml_max_nodes, MAX_YAML_NODES, "yaml_max_nodes"),
            (self.yaml_max_depth, MAX_YAML_DEPTH, "yaml_max_depth"),
        )
        for value, maximum, name in yaml_bounds:
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
                or value > maximum
            ):
                raise ConfigurationError(f"{name} must be an integer between 1 and {maximum}")
        if self.snapshot_working_set_bytes > self.snapshot_max_capture_bytes:
            raise ConfigurationError(
                "PLANTAIN_SNAPSHOT_WORKING_SET_BYTES must not exceed "
                "PLANTAIN_SNAPSHOT_MAX_CAPTURE_BYTES"
            )
        if self.trace_mode != "off" and not self.trace_data_governance_approved:
            raise ConfigurationError(
                "PLANTAIN_TRACE_DATA_GOVERNANCE_APPROVED=true is required when tracing is enabled"
            )
        if self.trace_mode != "off" and not self.trace_storage_encrypted:
            raise ConfigurationError(
                "PLANTAIN_TRACE_STORAGE_ENCRYPTED=true is required when tracing is enabled"
            )
        if self.zephyr_attach_report and not self.zephyr_attachment_data_governance_approved:
            raise ConfigurationError(
                "PLANTAIN_ZEPHYR_ATTACHMENT_DATA_GOVERNANCE_APPROVED=true is required "
                "when report attachments are enabled"
            )

    @property
    def scenarios_dir(self) -> Path:
        return self.project_root / "scenarios"

    @property
    def api_data_dir(self) -> Path:
        return self.project_root / "api-data"

    @property
    def snapshots_dir(self) -> Path:
        return self.project_root / "snapshots"

    @property
    def output_dir(self) -> Path:
        return self.project_root / "output"

    @property
    def generated_dir(self) -> Path:
        return self.project_root / "generated"

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> Settings:
        root_value = project_root or Path(_env("PLANTAIN_PROJECT_ROOT", str(Path.cwd())))
        root = root_value.expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ConfigurationError(f"Project root does not exist or is not a directory: {root}")

        browser = _env("PLANTAIN_BROWSER", "chromium").strip().lower()
        if browser not in {"chromium", "firefox", "webkit"}:
            raise ConfigurationError("PLANTAIN_BROWSER must be chromium, firefox, or webkit")

        trace_mode = _env("PLANTAIN_TRACE_MODE", "off").strip().lower()
        if trace_mode not in {"off", "on", "retain-on-failure"}:
            raise ConfigurationError("PLANTAIN_TRACE_MODE must be off, on, or retain-on-failure")

        storage_raw = _env("PLANTAIN_STORAGE_STATE_PATH", "").strip()
        storage_path = cls._project_path(root, storage_raw) if storage_raw else None

        return cls(
            project_root=root,
            environment=_env("PLANTAIN_ENV", "local").strip() or "local",
            log_level=_env("PLANTAIN_LOG_LEVEL", "INFO").strip().upper(),
            scenario_timeout_seconds=_float(
                "PLANTAIN_SCENARIO_TIMEOUT_SECONDS",
                300.0,
                minimum=1.0,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            step_timeout_seconds=_float(
                "PLANTAIN_STEP_TIMEOUT_SECONDS",
                60.0,
                minimum=1.0,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            cleanup_timeout_seconds=_float(
                "PLANTAIN_CLEANUP_TIMEOUT_SECONDS",
                15.0,
                minimum=0.1,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            max_scenario_concurrency=_int(
                "PLANTAIN_MAX_SCENARIO_CONCURRENCY",
                4,
                minimum=1,
                maximum=MAX_SCENARIO_CONCURRENCY,
            ),
            max_browser_sessions=_int(
                "PLANTAIN_MAX_BROWSER_SESSIONS",
                2,
                minimum=1,
                maximum=MAX_BROWSER_SESSIONS,
            ),
            max_api_requests=_int(
                "PLANTAIN_MAX_API_REQUESTS",
                16,
                minimum=1,
                maximum=MAX_API_REQUESTS,
            ),
            max_database_operations=_int(
                "PLANTAIN_MAX_DATABASE_OPERATIONS",
                8,
                minimum=1,
                maximum=MAX_DATABASE_OPERATIONS,
            ),
            max_worker_threads=_int(
                "PLANTAIN_MAX_WORKER_THREADS",
                8,
                minimum=1,
                maximum=MAX_WORKER_THREADS,
            ),
            max_worker_processes=_int(
                "PLANTAIN_MAX_WORKER_PROCESSES",
                2,
                minimum=1,
                maximum=MAX_WORKER_PROCESSES,
            ),
            snapshot_max_capture_bytes=_int(
                "PLANTAIN_SNAPSHOT_MAX_CAPTURE_BYTES",
                536_870_912,
                minimum=1_048_576,
                maximum=MAX_SNAPSHOT_CAPTURE_BYTES,
            ),
            snapshot_working_set_bytes=_int(
                "PLANTAIN_SNAPSHOT_WORKING_SET_BYTES",
                1_048_576,
                minimum=65_536,
                maximum=MAX_SNAPSHOT_WORKING_SET_BYTES,
            ),
            snapshot_max_network_correlations=_int(
                "PLANTAIN_SNAPSHOT_MAX_NETWORK_CORRELATIONS",
                10_000,
                minimum=1,
                maximum=MAX_SNAPSHOT_NETWORK_CORRELATIONS,
            ),
            schema_validation_timeout_seconds=_float(
                "PLANTAIN_SCHEMA_VALIDATION_TIMEOUT_SECONDS",
                10.0,
                minimum=0.1,
                maximum=MAX_SCHEMA_VALIDATION_TIMEOUT_SECONDS,
            ),
            schema_validation_cpu_seconds=_int(
                "PLANTAIN_SCHEMA_VALIDATION_CPU_SECONDS",
                5,
                minimum=1,
                maximum=MAX_SCHEMA_VALIDATION_CPU_SECONDS,
            ),
            schema_validation_memory_mib=_int(
                "PLANTAIN_SCHEMA_VALIDATION_MEMORY_MIB",
                512,
                minimum=64,
                maximum=MAX_SCHEMA_VALIDATION_MEMORY_MIB,
            ),
            browser=browser,
            headless=_bool("PLANTAIN_HEADLESS", True),
            slow_mo_ms=_int(
                "PLANTAIN_SLOW_MO_MS",
                0,
                maximum=MAX_MILLISECOND_DURATION,
            ),
            viewport_width=_int(
                "PLANTAIN_VIEWPORT_WIDTH",
                1440,
                minimum=320,
                maximum=MAX_VIEWPORT_DIMENSION,
            ),
            viewport_height=_int(
                "PLANTAIN_VIEWPORT_HEIGHT",
                900,
                minimum=240,
                maximum=MAX_VIEWPORT_DIMENSION,
            ),
            action_timeout_ms=_int(
                "PLANTAIN_ACTION_TIMEOUT_MS",
                15_000,
                minimum=100,
                maximum=MAX_MILLISECOND_DURATION,
            ),
            navigation_timeout_ms=_int(
                "PLANTAIN_NAVIGATION_TIMEOUT_MS",
                30_000,
                minimum=100,
                maximum=MAX_MILLISECOND_DURATION,
            ),
            trace_mode=trace_mode,
            trace_data_governance_approved=_bool(
                "PLANTAIN_TRACE_DATA_GOVERNANCE_APPROVED",
                False,
            ),
            trace_storage_encrypted=_bool("PLANTAIN_TRACE_STORAGE_ENCRYPTED", False),
            trace_retention_days=_int(
                "PLANTAIN_TRACE_RETENTION_DAYS",
                7,
                minimum=1,
                maximum=MAX_TRACE_RETENTION_DAYS,
            ),
            trace_max_archives=_int(
                "PLANTAIN_TRACE_MAX_ARCHIVES",
                20,
                minimum=1,
                maximum=MAX_TRACE_ARCHIVES,
            ),
            trace_max_bytes=_int(
                "PLANTAIN_TRACE_MAX_BYTES",
                1_073_741_824,
                minimum=1_048_576,
                maximum=MAX_TRACE_BYTES,
            ),
            storage_state_path=storage_path,
            browser_auto_install=_bool("PLANTAIN_BROWSER_AUTO_INSTALL", True),
            browser_install_timeout_seconds=_float(
                "PLANTAIN_BROWSER_INSTALL_TIMEOUT_SECONDS",
                300.0,
                minimum=1.0,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            allow_custom_playwright_download_hosts=_bool(
                "PLANTAIN_ALLOW_CUSTOM_PLAYWRIGHT_DOWNLOAD_HOSTS", False
            ),
            allow_private_networks=_bool("PLANTAIN_ALLOW_PRIVATE_NETWORKS", False),
            allowed_hosts=_hosts(_env("PLANTAIN_ALLOWED_HOSTS", "")),
            network_mode=normalize_network_mode(_env("PLANTAIN_NETWORK_MODE", "standard")),
            blocked_hosts=_blocked_hosts(_env("PLANTAIN_BLOCKED_HOSTS", "")),
            api_allowed_methods=_api_methods(_env("PLANTAIN_API_ALLOWED_METHODS", "")),
            allow_insecure_local_http=_bool("PLANTAIN_ALLOW_INSECURE_LOCAL_HTTP", False),
            egress_control_enforced=_bool(
                "PLANTAIN_EGRESS_CONTROL_ENFORCED",
                False,
            ),
            api_connect_timeout_seconds=_float(
                "PLANTAIN_API_CONNECT_TIMEOUT_SECONDS",
                10.0,
                minimum=0.1,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            api_timeout_seconds=_float(
                "PLANTAIN_API_TIMEOUT_SECONDS",
                30.0,
                minimum=0.1,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            api_max_response_bytes=_int(
                "PLANTAIN_API_MAX_RESPONSE_BYTES",
                10_485_760,
                minimum=1_024,
                maximum=MAX_IN_MEMORY_DOCUMENT_BYTES,
            ),
            openapi_max_bytes=_int(
                "PLANTAIN_OPENAPI_MAX_BYTES",
                20_971_520,
                minimum=1_024,
                maximum=MAX_IN_MEMORY_DOCUMENT_BYTES,
            ),
            sensitive_key_names=_load_sensitive_key_names(root),
            db_query_timeout_seconds=_float(
                "PLANTAIN_DB_QUERY_TIMEOUT_SECONDS",
                15.0,
                minimum=0.1,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            db_max_rows=_int(
                "PLANTAIN_DB_MAX_ROWS",
                100,
                minimum=1,
                maximum=MAX_DATABASE_ROWS,
            ),
            db_max_columns=_int(
                "PLANTAIN_DB_MAX_COLUMNS",
                100,
                minimum=1,
                maximum=MAX_DATABASE_COLUMNS,
            ),
            db_max_result_bytes=_int(
                "PLANTAIN_DB_MAX_RESULT_BYTES",
                1_048_576,
                minimum=1_024,
                maximum=MAX_DATABASE_RESULT_BYTES,
            ),
            allow_db_mutations=_bool("PLANTAIN_ALLOW_DB_MUTATIONS", False),
            db_allowed_targets=_tcp_targets(_env("PLANTAIN_DB_ALLOWED_TARGETS", "")),
            db_allow_private_networks=_bool(
                "PLANTAIN_DB_ALLOW_PRIVATE_NETWORKS",
                False,
            ),
            db_pool_size=_int(
                "PLANTAIN_DB_POOL_SIZE",
                5,
                minimum=1,
                maximum=MAX_DATABASE_POOL_SIZE,
            ),
            db_max_overflow=_int(
                "PLANTAIN_DB_MAX_OVERFLOW",
                0,
                maximum=MAX_DATABASE_POOL_OVERFLOW,
            ),
            db_pool_timeout_seconds=_float(
                "PLANTAIN_DB_POOL_TIMEOUT_SECONDS",
                5.0,
                minimum=0.1,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            db_pool_recycle_seconds=_int(
                "PLANTAIN_DB_POOL_RECYCLE_SECONDS",
                1_800,
                minimum=1,
                maximum=MAX_RESOURCE_LIFETIME_SECONDS,
            ),
            db_max_source_pools=_int(
                "PLANTAIN_DB_MAX_SOURCE_POOLS",
                8,
                minimum=1,
                maximum=MAX_DATABASE_SOURCE_POOLS,
            ),
            db_max_sources_per_scenario=_int(
                "PLANTAIN_DB_MAX_SOURCES_PER_SCENARIO",
                4,
                minimum=1,
                maximum=MAX_DATABASE_SOURCES_PER_SCENARIO,
            ),
            db_fetch_batch_size=_int(
                "PLANTAIN_DB_FETCH_BATCH_SIZE",
                100,
                minimum=1,
                maximum=MAX_DATABASE_FETCH_BATCH_SIZE,
            ),
            allow_insecure_local_db_tls=_bool("PLANTAIN_ALLOW_INSECURE_LOCAL_DB_TLS", False),
            allure_results_enabled=_bool("PLANTAIN_ALLURE_RESULTS_ENABLED", False),
            zephyr_base_url=_env("JIRA_BASE_URL", "").strip(),
            zephyr_publish_results=_bool("PLANTAIN_ZEPHYR_PUBLISH_RESULTS", False),
            zephyr_attach_report=_bool("PLANTAIN_ZEPHYR_ATTACH_REPORT", False),
            zephyr_attachment_data_governance_approved=_bool(
                "PLANTAIN_ZEPHYR_ATTACHMENT_DATA_GOVERNANCE_APPROVED",
                False,
            ),
            zephyr_allow_private_networks=_bool("PLANTAIN_ZEPHYR_ALLOW_PRIVATE_NETWORKS", False),
            zephyr_allow_insecure_http=_bool("PLANTAIN_ZEPHYR_ALLOW_INSECURE_HTTP", False),
            zephyr_timeout_seconds=_float(
                "PLANTAIN_ZEPHYR_TIMEOUT_SECONDS",
                15.0,
                minimum=0.1,
                maximum=MAX_OPERATION_TIMEOUT_SECONDS,
            ),
            zephyr_max_response_bytes=_int(
                "PLANTAIN_ZEPHYR_MAX_RESPONSE_BYTES",
                1_048_576,
                minimum=1_024,
                maximum=MAX_IN_MEMORY_DOCUMENT_BYTES,
            ),
            zephyr_max_attachment_bytes=_int(
                "PLANTAIN_ZEPHYR_MAX_ATTACHMENT_BYTES",
                5_242_880,
                minimum=1_024,
                maximum=MAX_IN_MEMORY_DOCUMENT_BYTES,
            ),
            zephyr_max_connections=_int(
                "PLANTAIN_ZEPHYR_MAX_CONNECTIONS",
                4,
                minimum=1,
                maximum=MAX_REPORTING_CONNECTIONS,
            ),
            zephyr_outbox_max_entries=_int(
                "PLANTAIN_ZEPHYR_OUTBOX_MAX_ENTRIES",
                1_000,
                minimum=1,
                maximum=MAX_ZEPHYR_OUTBOX_ENTRIES,
            ),
            zephyr_outbox_retry_batch_size=_int(
                "PLANTAIN_ZEPHYR_OUTBOX_RETRY_BATCH_SIZE",
                1,
                minimum=1,
                maximum=MAX_ZEPHYR_OUTBOX_RETRY_BATCH_SIZE,
            ),
            zephyr_outbox_lock_timeout_seconds=_float(
                "PLANTAIN_ZEPHYR_OUTBOX_LOCK_TIMEOUT_SECONDS",
                5.0,
                minimum=0.1,
                maximum=MAX_ZEPHYR_OUTBOX_LOCK_TIMEOUT_SECONDS,
            ),
        )

    @staticmethod
    def _project_path(root: Path, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        resolved = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ConfigurationError(
                "Configured runtime paths must stay inside the project root"
            ) from exc
        return resolved

    def ensure_runtime_directories(self) -> None:
        for directory in (self.output_dir, self.snapshots_dir, self.generated_dir):
            ensure_private_directory(directory)


__all__ = ["Settings"]
