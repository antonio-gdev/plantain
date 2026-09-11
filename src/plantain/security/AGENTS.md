# Security Specialist

<!--
AGENT METADATA
- Role: Redaction, URL Policy, and Secret-Taint Specialist
- Last Updated: 2026-08-30
-->

Read the root instructions first. Security changes require tests in every affected caller domain.

## Non-obvious contracts

- Never read `.env`. Environment values are observed only during expression resolution, and only
  values whose variable names match built-in or configured sensitive-key policy are retained in
  the per-scenario `SecretRegistry`.
- `sensitive-keys.txt` contains key fragments, not secret values. Matching is broader than exact
  dictionary keys: matching key/value pairs and any nested or free-form string containing a
  configured fragment must be masked.
- Built-in credential-shaped keys and opaque/unstructured credential patterns remain active even
  when the configured key file is empty.
- Observed environment values stay tainted after chaining under a different key. Do not rely only
  on destination field names.
- Bound resolved environment/expression strings, context paths, and each scenario's aggregate
  observed-secret bytes with generous internal ceilings. Mask classified short secrets only as
  whole or delimiter-bounded tokens so ordinary text remains usable.
- Artifact redaction traverses complete supported structures and fails explicitly beyond its
  generous internal nesting ceiling; it never silently truncates. Log redaction additionally
  bounds depth, collection size, strings, and binary representations. Do not use bounded log
  rendering when producing a complete sanitized attachment.
- All logging handlers use the central policy plus the currently bound scenario registry. Never
  create a second formatter-specific sanitizer.
- URL rendering for logs removes userinfo and masks sensitive query/fragment data while preserving
  enough route structure for diagnosis.
- Standard HTTP(S)/WS(S) policy permits public HTTPS/WSS after deny-rule, URL, DNS, and
  public-address validation through a hard-bounded TTL cache. Revalidate every redirect.
- Restricted policy additionally requires exact host rules and deployment egress attestation.
  `PLANTAIN_BLOCKED_HOSTS` wins in both modes; wildcard subdomains require an explicit `*.` rule.
- Private/reserved targets require an exact allow rule plus private-network opt-in. Plain HTTP/WS
  remains limited to explicitly enabled loopback local development.
- Application DNS checks cannot pin the peer selected later by Playwright, HTTPX, or a native
  database driver. Restricted URL policy, non-local database access, and Zephyr therefore require
  an actual deployment egress firewall/proxy; never treat the attestation flag as the control.
- Database targets are separately authorized as exact normalized `host:port` pairs and resolved
  afresh before each pool acquisition.
- Private/reserved/local targets and insecure schemes require explicit settings. Never infer trust
  from a hostname string before checking resolved addresses.
- Atomic persistence belongs to the shared persistence layer: private sibling stage, file `fsync`,
  `os.replace`, directory `fsync`, and cleanup. Unsupported filesystems fail closed.
- The mounted-workspace `r+b` truncate workaround is a human-approved development repair only. It
  must never appear as a runtime fallback.

## Fast feedback

```bash
uv run --locked --extra dev ruff check \
  src/plantain/security \
  src/plantain/observability.py \
  src/plantain/persistence.py \
  tests/unit/security \
  tests/unit/test_observability.py \
  tests/unit/test_persistence.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/security \
  tests/unit/test_observability.py \
  tests/unit/test_persistence.py \
  tests/unit/test_expressions.py \
  tests/unit/test_runtime.py
```

Add explicit non-leak assertions for every new diagnostic field or nested payload path.
