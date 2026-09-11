# API Activity Specialist

<!--
AGENT METADATA
- Role: API and Runtime Schema Specialist
- Last Updated: 2026-08-30
-->

Read the parent activity and root instructions first.

## Non-obvious contracts

- Shared API response results retain the observed response media type as nullable `contentType`
  so later `validateSchema` steps never need to guess it.
- The optional process-level API method allowlist is permissive when empty and applies to
  application operations without adding policy fields to scenario YAML.
- Runtime schema interpretation is deliberate. Do not add generated endpoint models, builders,
  flow-specific clients, or dispatcher branches.
- Keep the four activities distinct: `sendRequest` is schema-independent; `loadApiSchema` discovers
  and caches a contract; `callSchema` selects and validates an operation; `validateSchema`
  validates stored evidence and can assert expected contract drift.
- `schemaHeaders` authenticate only a direct schema download. Operation `headers` authenticate only
  the target call. Never merge them or include protected schema headers in cache identity,
  conditional requests, logs, or outputs.
- The bounded content-addressed schema cache and source index use atomic persistence. Recompute and
  verify the digest, full document contract, and unambiguous source identity on every cached load;
  corrupt entries and unsupported schema versions fail closed. Cache documents and API body files
  use descriptor-relative no-follow reads rather than separate path checks and reads.
- OpenAPI and JSON Schema validation run only through the shared, killable spawned-worker boundary.
  Preserve wall, CPU, memory, process, and thread limits; child environments and unexpected error
  values must never cross back into the parent.
- Preserve dialect-specific validation and actual-media selection. Reject cookie, form-data,
  response-header, and serialization contracts the client cannot faithfully represent.
- External references are blocked before validation. Do not fetch a remote `$ref` implicitly.
- Every request and redirect passes URL/DNS policy. Strip non-allowlisted headers on cross-origin
  redirect, block HTTPS downgrade, cap redirect count, share one timeout and response-byte budget
  across the complete chain, and retry only operations proven safe.
- Encode query parameters before final URL validation. Bound header/query components and request
  bytes internally, reject case-insensitive header duplicates, and retain only a conservative
  header allowlist when an origin changes.
- Reject non-finite request and response JSON numbers, and bound decoded JSON depth and item count
  internally without adding scenario settings.
- Validate the request before transport and the response after transport when enabled. Negative
  server-validation tests may explicitly disable request validation; contract-drift tests disable
  response validation and assert `expectedValid: false` separately.
- Context retains complete request/response bodies for same-scenario chaining. Logs and reports get
  only bounded recursively sanitized previews. Schema errors expose paths and rule names, never
  rejected values.
- Public activity failures before normal operation tracking begins retain one minimal sanitized
  preparation record; never copy authored request values into that fallback evidence.
- Preserve HTTP status in both result evidence and failures. A response is logged before status,
  message, or schema assertions so sanitized server diagnostics remain traceable.
- Use the first declared OpenAPI server deterministically and resolve variables only from declared
  defaults; every resulting target still passes URL/DNS policy. Unsupported non-default server
  selection, serialization, multipart, callbacks/webhooks, and external references fail rather
  than being guessed.

## Fast feedback

```bash
uv run --locked --extra dev ruff check \
  src/plantain/activities/api \
  tests/unit/activities/test_api_*.py \
  tests/unit/activities/test_openapi*.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/activities/test_api_*.py \
  tests/unit/activities/test_openapi*.py
```

Live schema/API checks require a human-approved schema URL and operation target. Standard mode
permits public HTTPS targets after safety validation; restricted or private targets require exact
allow rules. Keep public-service drift explicit: a repaired response may correctly invert an
expected-invalid assertion.
