# Run tests

Plantain uses the same execution runtime from the dashboard and CLI. Choose the
scope in **Tests**, review the operation, and follow progress without leaving the
workspace.

## Before you run

Confirm:

- the selected test or filtered scope is correct;
- required environment values are available to the dashboard process;
- live targets are approved and reachable;
- UI browser support is installed or can be provisioned safely;
- optional result integrations are configured as intended.

Use validation first when a test was recently created or changed. Validation
checks the artifact; execution checks the live behavior.

## Run one test

Find the test and choose **Run test** on its row.

Plantain re-resolves the selected test from the current project, performs preflight
validation, and then starts the live run. This prevents a stale browser selection
from silently executing a changed or missing source.

## Run every matching test

Set the desired search and tag filters, then choose **Run matching**.

“Matching” means the complete filtered collection across all catalog pages—not
only the rows currently visible. Review the match count before starting a broad
scope.

## Run an explicit selection

Choose **Select tests**, select the intended rows, and choose **Run selected**.

Use explicit selection when the desired group cannot be expressed clearly with
search and tags. Selected rows remain visually identified by a yellow outline
until you clear the selection or choose **Done**.

## Follow live progress

The execution surface shows compact, value-free progress such as:

- queued;
- scenario started;
- current step;
- completed step count;
- scenario passed or failed;
- batch completion.

Progress messages identify work without exposing resolved credentials, raw
payloads, or database values. The live channel is bounded so a large batch cannot
grow browser state without limit.

## Understand batch behavior

Plantain may execute directory or selected scopes concurrently when configured to
do so, but:

- each scenario keeps isolated browser, HTTP, database, value, and secret state;
- result order remains deterministic;
- one scenario cannot read another scenario’s stored values;
- shared transports and pools close once at their owning boundary;
- every scenario receives its own immutable result.

Choose a concurrency level appropriate for the target and local machine when using
the CLI. The dashboard protects the active batch scope while queued tests run.

## Read the immediate outcome

A successful run shows a pass status, duration, and completed-step count. A failed
run shows a compact sanitized explanation and identifies the failure stage when
safe evidence is available.

The immediate message is intentionally concise. Open **Runs and Results** for
metadata, operations, artifacts, and failure evidence.

## Run from the CLI

One test:

```bash
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api/petstore/find_available_pets.yaml
```

A directory with bounded concurrency:

```bash
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api \
  --concurrency 4
```

The environment must provide the references required by the selected tests. See
the [CLI guide](../get-started/cli.md) for complete examples and filters.

## Rerun after a change

Review the failed evidence, correct the environment or ask the agent for a focused
test repair, then run the test again.

A rerun creates a new result. Plantain does not overwrite the earlier failure,
which lets you compare the original evidence with the corrected execution.

## When execution is blocked

Plantain stops before or during execution when:

- the selected artifact changed or cannot be validated;
- a required environment reference is absent;
- the live destination violates URL, DNS, or database policy;
- the selected browser is unavailable and cannot be provisioned safely;
- a request, response, query, result, or runtime exceeds a configured bound;
- an operation would weaken a security boundary;
- the process is cancelled.

Correct the scope or environment and start a new run. Do not replace missing
evidence with literal secrets or broader network access.

Next, [review the result](results.md).
