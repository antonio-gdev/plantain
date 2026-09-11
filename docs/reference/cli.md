# CLI reference

The Plantain CLI validates and runs agent-generated scenarios, starts the local
dashboard, lists the internal activity surface, and exposes one separate
human-authorized database mutation boundary.

## General form

```bash
uv run --locked --all-extras plantain \
  [global options] \
  <command> \
  [command options]
```

Place global options before the command.

## Global options

| Option | Meaning |
| --- | --- |
| `--project-root PATH` | Select the project containing scenarios, snapshots, generated artifacts, and output |
| `--no-dotenv` | Do not load `PROJECT_ROOT/.env` for runtime commands |
| `--help` | Show command help |

The current directory is the default project root.

## `validate`

Validate one or more scenario files or directories without executing live targets:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
```

Options:

| Option | Meaning |
| --- | --- |
| `--tag TAG` | Require this tag; repeat to require every supplied tag |
| `--tag-any TAG` | Require at least one supplied alternative; repeat to add alternatives |
| `--exclude-tag TAG` | Exclude this tag; repeat to exclude more |

Each valid scenario prints its source and step count. A validation error exits
safely without running the selected target.

## `run`

Execute one or more scenario files or directories:

```bash
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/api
```

Options:

| Option | Meaning |
| --- | --- |
| `--concurrency N` | Maximum selected scenarios submitted concurrently; default `1` |
| `--tag TAG` | Require every repeated tag |
| `--tag-any TAG` | Require at least one repeated alternative |
| `--exclude-tag TAG` | Exclude every repeated tag |
| `--json` | Print complete sanitized scenario results as JSON objects |

Selection and returned result order remain deterministic even when execution is
concurrent.

Without `--json`, each success prints the scenario, duration, and step count,
followed by sanitized declared outputs when present. Failures print a sanitized
message to standard error.

## `dashboard`

Start the supported local dashboard:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard
```

Option:

| Option | Meaning |
| --- | --- |
| `--port PORT` | Combined loopback frontend/backend port; default `3000` |

The port must be an integer from 1 through 65535. The launcher prints the URL and
returns `130` when stopped with ++ctrl+c++.

## `activities`

Print the registered internal YAML activity descriptions:

```bash
uv run --locked --all-extras plantain activities
```

This is an audit and troubleshooting command. Nontechnical users normally review
activity meaning through **Tests** → **View steps** instead.

## `db-mutate`

`db-mutate` executes one explicitly reviewed DML file through a separate human-only
boundary. It is never available to the agent, scenario activity registry, or
dashboard creation workflow.

It requires all of the following:

- `PLANTAIN_ALLOW_DB_MUTATIONS=true`;
- one readable UTF-8 SQL file inside the project root;
- exactly one `INSERT`, `UPDATE`, `DELETE`, or `MERGE`;
- optional named parameters in one bounded JSON object;
- the exact acknowledgement `I AUTHORIZE THIS DATABASE MUTATION`;
- the same database target, TLS, and egress controls used by approved access.

Command form:

```bash
uv run --locked --all-extras plantain --no-dotenv db-mutate \
  path/to/reviewed.sql \
  --parameters path/to/parameters.json \
  --acknowledge "I AUTHORIZE THIS DATABASE MUTATION"
```

This command is for an authorized human who has independently reviewed the SQL and
target. Never use it as an automated workaround for the permanent read-only agent
boundary.

## Dotenv loading

`run`, `dashboard`, and `db-mutate` load `PROJECT_ROOT/.env` unless
`--no-dotenv` is present. Existing process values are not overridden.

`validate` and `activities` do not need runtime dotenv loading.

## Exit status

| Status | Meaning |
| --- | --- |
| `0` | Command completed successfully |
| `1` | At least one executed scenario failed |
| `2` | Configuration, validation, command, or runtime boundary failed safely |
| `130` | Dashboard process was interrupted with ++ctrl+c++ |

Unexpected exceptions are translated at the CLI boundary rather than printing a
raw traceback with potentially sensitive values.

## Get command-specific help

```bash
uv run --locked --all-extras plantain --help
uv run --locked --all-extras plantain run --help
uv run --locked --all-extras plantain validate --help
uv run --locked --all-extras plantain dashboard --help
```

For task-focused examples, use the [CLI guide](../get-started/cli.md).
