# Security and privacy

Plantain is local-first, not network-isolated by definition. Tests and results stay
in the selected project by default, while approved live targets, hosted agent
providers, or enabled result integrations may receive the minimum context required
for their operation.

Your main responsibility is to approve the target, credential source, and context.
Plantain’s responsibility is to enforce the runtime boundary.

## Security model at a glance

| Boundary | Plantain behavior |
| --- | --- |
| Credentials | Resolves environment or session values only when needed and never serializes them into generated tests |
| Agent context | Sends only bounded, sanitized context selected for the active operation |
| Dashboard | Binds the supported launcher to one loopback port |
| HTTP and schemas | Validates URL, DNS, address, redirects, size, timeout, and supported contract behavior |
| UI browser | Isolates contexts and keeps tracing disabled by default |
| Database agent work | Allows metadata discovery and one proven read-only query; mutation is rejected |
| Results | Persists immutable, sanitized local records before optional publication |
| Shared resources | Enforces bounded admission and closes resources once at their owner |
| Persistence | Uses atomic replacement and fails on unsupported durability |

## Keep secrets out of test meaning

Use environment names for:

- application passwords;
- API keys and authorization tokens;
- database URLs, usernames, and passwords;
- agent-provider credentials;
- Jira personal access tokens;
- private certificates, wallets, or storage-state values.

Do not place resolved values in an intent, scenario, path, tag, result identifier,
command argument, log message, screenshot, or documentation.

Plantain tracks values observed during environment resolution so later aliases are
sanitized too. Logs, reports, operation evidence, and user-facing errors pass
through central recursive sanitization.

Redaction reduces accidental disclosure; it does not make unnecessary data safe to
collect.

## Understand agent-provider exposure

The dashboard does not send the complete project to an agent provider.

For each operation, Plantain selects a bounded context packet from approved sources
such as:

- the current intent and clarification;
- a selected existing test;
- a selected prior result;
- current API contract evidence;
- database discovery metadata;
- private semantic UI evidence required for the workflow.

Review context before submission. Choose a local provider when external processing
is not approved.

Credentials entered in **Settings** remain in backend process memory and are never
returned as browser-readable status.

## Approve outbound targets

Standard mode permits public HTTPS and WSS only after host, URL, DNS, and
public-address validation. Deny rules always win.

Restricted, private, loopback HTTP, database, and Zephyr access require their
specific opt-ins and exact rules. An egress-control attestation never creates the
external firewall or proxy it claims.

Plantain revalidates redirects and relevant DNS decisions, strips credentials
across origins, and blocks secure-to-insecure downgrade.

See [Network access](../configuration/network.md).

## Protect browser evidence

Every UI scenario uses an isolated browser context. Browser storage, cookies, and
page state do not pass between scenarios.

Semantic snapshots are private AI evidence. They may contain application text,
accessible labels, URLs, and frame content. Keep `snapshots/` ignored and let
runtime transactions own the registry.

Tracing remains off by default because a trace archive can retain page and network
data. Enabling it requires both reviewed data governance and encrypted storage
attestations plus a retention policy.

Lazy browser installation is browser-specific, shell-free, lock-protected, and
uses an allowlisted child environment that excludes application credentials.

## Preserve the read-only database boundary

Agent-driven database work:

- discovers catalogs and table metadata without scanning application rows;
- executes one provably read-only statement;
- requires exact named binds;
- bounds rows, columns, bytes, time, pools, and source counts;
- uses per-scenario sessions;
- rolls back when returning a connection.

`INSERT`, `UPDATE`, `DELETE`, `MERGE`, DDL, procedure calls, locking reads, and
equivalent mutations are rejected.

The separate human mutation CLI requires independent environment enablement,
reviewed SQL, exact acknowledgement, and direct human execution. It is never an
agent tool.

## Keep execution isolated

Each scenario receives separate:

- browser context;
- HTTP cookie state;
- database session or lease;
- value context;
- secret registry.

Concurrent execution does not create cross-scenario value sharing. Shared engines,
transports, pools, and reporters close exactly once at the runtime boundary that
owns them.

## Treat results as sensitive

Native results are sanitized and immutable, but they can still reveal:

- application structure;
- business identifiers;
- expected and actual behavior;
- failure timing;
- selected response or database evidence;
- artifact names.

Restrict access to `output/` and `snapshots/`, apply retention, and share only the
specific evidence needed for an approved purpose.

The compact terminal event intentionally contains less detail than the native
result and JSONL trace.

## Enable integrations deliberately

Allure and Zephyr are optional result destinations.

Zephyr publication requires a real existing `testCaseKey`; Plantain never
fabricates one. Full report attachment is a separate opt-in requiring explicit
data-governance approval.

Local persistence occurs first. The durable publication outbox never stores the
Jira token.

## Expect fail-closed behavior

Plantain stops instead of guessing when:

- a target cannot be validated;
- a schema feature is unsupported;
- evidence is stale or missing;
- a result exceeds a bound;
- an atomic write cannot be guaranteed;
- a database statement cannot be proven read-only;
- a required security attestation is absent.

Treat the error as a request to correct the environment, evidence, or policy—not
as a prompt to disable the boundary.

## User checklist

Before a live run:

- [ ] The target and environment are approved.
- [ ] Credentials come from an approved runtime source.
- [ ] The intent and selected context contain no literal secrets.
- [ ] The generated steps prove only the required behavior.
- [ ] Database work is read-only.
- [ ] Private or restricted access has exact policy and real egress enforcement.
- [ ] Optional destinations are approved for the selected evidence.
- [ ] Output and snapshot retention are understood.

If an unexpected value appears in a user-facing surface, stop, preserve only the
minimum safe diagnostic context, rotate the exposed credential when applicable,
and investigate the sanitization boundary before running again.
