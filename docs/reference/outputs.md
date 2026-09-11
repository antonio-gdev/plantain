# Understand files and outputs

Plantain separates reviewable test source from private discovery evidence and
runtime output. This keeps versioned intent understandable while preventing local
results, credentials, caches, and browser data from becoming source artifacts.

## Project map

| Location | Purpose | Typical handling |
| --- | --- | --- |
| `scenarios/` | Reviewed runnable test cases | Version only after review and secret checks |
| `activities/` | Maintainer contracts for the eight internal activities | Version with runtime behavior |
| `generated/` | Intermediate agent-authored material | Keep local and ignored |
| `api-data/` | Bounded API support data and cache material | Keep local and ignored |
| `snapshots/` | Complete semantic UI evidence and registry | Private; keep local and ignored |
| `output/` | Results, logs, events, integrations, and dashboard runtime | Keep local and ignored |

The dashboard presents safe, bounded views of these records. It does not make the
browser responsible for choosing unrestricted filesystem paths.

## Scenarios

Accepted test cases live beneath:

```text
scenarios/
```

Folders communicate ownership or domain, while tags provide optional filtering and
reporting metadata.

Before versioning a generated scenario:

- inspect it through **View steps**;
- validate it;
- confirm every credential is an environment reference;
- confirm reporting identifiers are real and intentional;
- confirm database behavior is read-only;
- remove unrelated or excessive context.

See [Understand a generated test](generated-artifacts.md).

## Native results

Each execution receives a unique run identifier and one immutable JSON result:

```text
output/results/<scenario-source-without-extension>/<run-id>.result.json
```

If a scenario has no source path, Plantain creates a safe name-based result
directory instead.

The file contains the sanitized scenario result, ordered step outcomes, declared
outputs, operation evidence, artifact references, and integration status supported
by that run.

Running the same test again creates a new file. Plantain never overwrites an
earlier result to represent a rerun.

## Detailed logs

Each runtime command creates a private timestamped JSONL log:

```text
output/logger-<timestamp>-p<process>-<random>.jsonl
```

These logs contain detailed sanitized operational evidence. They are intended for
local diagnosis and may still reveal sensitive business context.

Do not publish them as routine CI console output or commit them to Git.

## Compact terminal events

Plantain writes compact terminal telemetry to:

```text
output/event/splunk-event-json.log
```

The rotating event log is suitable for an approved external collector. It does not
replace detailed local operation evidence and does not perform an outbound Splunk
upload itself.

## Allure raw results

When enabled, Plantain writes Allure-compatible raw result files beneath:

```text
output/allure/
```

These are optional integration artifacts. The native result remains authoritative,
and HTML report generation requires a separately managed Allure CLI.

## Zephyr outbox

When Zephyr publishing is enabled and eligible publication cannot complete
immediately, Plantain may retain a bounded durable outbox job under its
runtime-managed output structure.

Outbox records:

- contain publication intent and integrity metadata;
- never contain the Jira personal access token;
- are capacity- and byte-bounded;
- use atomic persistence and explicit state transitions;
- remain secondary to the local native result.

Let the reporting runtime manage these files. Do not edit or fabricate outbox
entries.

## Dashboard runtime

The supported launcher stages its writable Reflex entry point and packaged assets
under:

```text
output/dashboard/port-<port>/
```

This is disposable local runtime, not a second source tree. The authoritative logo
and favicon remain packaged with Plantain.

## Semantic snapshots

UI discovery retains complete semantic evidence beneath:

```text
snapshots/
```

The registry maps content-addressed evidence to its owning workflow. Snapshots are
private AI evidence and may contain page text, accessible names, URLs, or other
application context.

Do not:

- commit snapshots;
- manually mutate `snapshots/registry.json`;
- rename artifacts outside runtime transactions;
- share a snapshot without approved evidence review.

Complete discovery has no total element or frame limit. Transport chunks are not
silent truncation; if the configured capture envelope is exhausted, the operation
fails explicitly.

## Generated intermediates and API data

`generated/` and `api-data/` support agent and API workflows. They are local
working material rather than the authoritative test catalog or result history.

Review the accepted scenario in `scenarios/` and the immutable outcome in
`output/results/` instead of treating intermediate files as final records.

## Atomic persistence

Plantain uses atomic replacement for manifests, registries, caches, reports, and
terminal events. If the underlying filesystem cannot provide the required
durability, Plantain fails rather than downgrading to an in-place write.

A persistence error therefore means the result or registry may not be safely
committed. Correct the filesystem condition before retrying.

## Retention and cleanup

Apply your organization’s retention policy to private runtime data.

Before removing old local output:

1. stop active Plantain processes;
2. retain any result required for audit or investigation;
3. confirm optional publication or collection completed;
4. remove only the specific approved runtime directory;
5. leave versioned scenarios and activity contracts untouched.

Never use a broad recursive cleanup command against the project root.

For evidence handling decisions, read
[Security and privacy](../operations/security.md).
