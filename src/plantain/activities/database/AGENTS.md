# Database Activity Specialist

<!--
AGENT METADATA
- Role: Read-Only Database Discovery and Pooling Specialist
- Last Updated: 2026-08-26
-->

Read the parent activity and root instructions first.

## Immutable boundary

Agent-authored YAML, skills, and activity code are read-only. Never generate or execute mutation,
DDL, procedure calls, locking reads, multiple statements, or syntax that cannot be proven
read-only. Do not use the human mutation CLI as a workaround.

## Non-obvious contracts

- `discoverDatabase` is catalog discovery, not a SQL row scanner. Preserve the progressive
  schemas → tables → one table/view phases, opaque pagination, case resolution, and optional
  system-schema/view inclusion.
- Page and filter catalog names server-side for supported vendors using bound SQLAlchemy Core
  expressions. Quietly fall back to Inspector for unsupported catalog behavior; never require
  another user setting or synthesize application-row SQL.
- `queryDatabase` accepts exactly one explicit statement, exact named binds, a bounded row/byte
  result, and an explicit scalar/row/rows result mode. Keep both AST validation and the
  engine-level execution firewall. Project-relative SQL files use descriptor-bound no-follow
  reads below `sql/`.
- `verifyDatabaseResult` supports scalar, single-row, and multi-row evidence. Structural mismatch
  diagnostics contain paths/reasons, not database values. Complete-result assertions reject a
  truncated envelope. Unordered partial rows use projected multisets for consistent shapes and a
  bounded non-recursive exact matcher otherwise. `${query}` preserves that envelope;
  `${query.result}` intentionally selects only its data and is not suitable when completeness must
  be proven.
- JDBC-form PostgreSQL, SQL Server, MySQL, and Oracle URLs are compatibility inputs only. Translate
  them to SQLAlchemy plus `psycopg`, `pyodbc`, `mysql-connector-python`, or `oracledb`; never start a
  JVM or load a JDBC driver.
- Authorize the resolved destination through `PLANTAIN_DB_ALLOWED_TARGETS` as an exact
  `host:port` before engine creation. Private addresses need
  `PLANTAIN_DB_ALLOW_PRIVATE_NETWORKS=true`; non-local execution also needs the externally enforced
  egress-control attestation.
- TLS parsing is vendor-specific and fail closed. Do not silently append driver-specific flags or
  accept ambiguous duplicate/conflicting URL properties.
- Bounded `QueuePool` engines are keyed by a credential-safe HMAC of source and pool settings.
  Identical sources reuse an owned engine; credential rotation gets a distinct opaque key.
  Individual pool settings and their aggregate configured connection capacity remain bounded.
  Count in-flight per-source creation reservations toward that capacity, share identical-source
  creation, and never hold the manager lock across unrelated driver initialization.
- Scenario sessions/leases always release in `finally`. Pools are disposed exactly once by the
  runner/batch that owns them. Do not let per-scenario services close a shared manager.
- The separately invoked human mutation path uses `NullPool`, one reviewed contained file, exact
  acknowledgement, one explicit transaction, and unconditional disposal. It must remain absent
  from activity registration and agent skills.

## Fast feedback

```bash
uv run --locked --extra dev ruff check \
  src/plantain/activities/database \
  tests/unit/activities/test_database_*.py \
  tests/unit/test_database_cli.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/activities/test_database_*.py \
  tests/unit/test_database_cli.py
```

Unit tests use fakes and do not prove vendor connectivity. Live parity for each database requires
human-supplied credentials, TLS policy, optional schema scope, and explicit approval.
