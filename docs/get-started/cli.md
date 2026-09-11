# Use the CLI

The CLI validates and runs the same agent-generated scenarios shown in the
dashboard. Use it when you need a repeatable local command, a scriptable workflow,
or continuous integration.

You do not need the CLI to create test artifacts. Start creation in the dashboard,
then use the CLI to validate or execute the resulting catalog.

## Command shape

Run Plantain through the locked project environment:

```bash
uv run --locked --all-extras plantain [global options] <command> [command options]
```

Global options such as `--project-root` and `--no-dotenv` must appear before the
command name.

## Validate without execution

Validate one scenario:

```bash
uv run --locked --all-extras plantain --no-dotenv validate \
  scenarios/api/petstore/find_available_pets.yaml
```

Validate the complete catalog:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
```

Validation loads the selected files and applies the registered activity contracts,
but it does not contact live application targets.

## Run a UI scenario

Supply required values to the process, then run the selected scenario:

```bash
SAUCE_DEMO_URL="https://www.saucedemo.com" \
SAUCE_DEMO_USERNAME="standard_user" \
SAUCE_DEMO_PASSWORD="secret_sauce" \
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/discovery/saucedemo_checkout_journey.yaml
```

The scenario keeps only environment references. Resolved values are registered for
redaction during execution.

## Run API scenarios

```bash
PETSTORE_API_KEY="special-key" \
PETSTORE_SCHEMA_URL="https://petstore.swagger.io/v2/swagger.json" \
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api/petstore/find_available_pets.yaml
```

```bash
PETSTORE_API_KEY="special-key" \
PETSTORE_SCHEMA_URL="https://petstore.swagger.io/v2/swagger.json" \
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api/petstore/get_inventory.yaml
```

These public-demo values illustrate command structure. Replace example values with
approved target and environment values for your own project; never commit real
credentials to scenario files or documentation.

## Run a directory

Plantain discovers selected scenario files deterministically. Directory execution
may run concurrently, while returned results remain in deterministic selection
order:

```bash
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api \
  --concurrency 4
```

Choose concurrency that the target and local machine can safely support.

## Filter by tags

Require every repeated `--tag`:

```bash
uv run --locked --all-extras plantain --no-dotenv run scenarios \
  --tag smoke \
  --tag api
```

Require at least one repeated `--tag-any`:

```bash
uv run --locked --all-extras plantain --no-dotenv run scenarios \
  --tag-any ui \
  --tag-any api
```

Exclude tagged scenarios:

```bash
uv run --locked --all-extras plantain --no-dotenv run scenarios \
  --exclude-tag slow
```

The same filters work with `validate`.

## Request sanitized JSON

For machine-readable results:

```bash
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api/petstore/find_available_pets.yaml \
  --json
```

Each completed scenario is printed as a sanitized JSON object. Without `--json`,
the CLI prints a compact pass line and sanitized declared outputs.

## Understand exit status

| Status | Meaning |
| --- | --- |
| `0` | The command completed successfully |
| `1` | At least one selected scenario failed |
| `2` | The command, configuration, validation, or runtime boundary failed safely |
| `130` | The local dashboard was stopped with ++ctrl+c++ |

Use the exit status in automation rather than parsing human-readable output.

## Dotenv behavior

By default, `run` and `dashboard` load `.env` from the selected project root
without overriding variables already present in the process. `--no-dotenv` skips
that file.

`validate` does not preload `.env` because it does not execute targets. Supplying
`--no-dotenv` consistently in validation and execution commands makes the intended
credential source explicit.

## Other commands

- `plantain activities` prints the registered internal YAML activity surface for
  audit and troubleshooting.
- `plantain dashboard` starts the supported loopback-only visual workspace.
- `plantain db-mutate` is a separate, human-only reviewed DML boundary. It is not
  available to scenarios or agent workflows.

For all options:

```bash
uv run --locked --all-extras plantain --help
```

Next, learn how to [run tests](../testing/run-tests.md) or integrate them into
[CI/CD](../operations/ci-cd.md).
