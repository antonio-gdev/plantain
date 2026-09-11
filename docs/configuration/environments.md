# Provide environment values

Plantain separates test meaning from environment-specific values. The generated
test names the value it needs; the runtime supplies the current value when the
test executes.

This lets one reviewed test run in different approved environments without
embedding credentials or endpoints in the artifact.

## Understand an environment reference

In **View steps**, you may see a value such as:

```text
env:SAUCE_DEMO_PASSWORD
```

Read it as:

> Obtain `SAUCE_DEMO_PASSWORD` from the runtime environment when this step runs.

The generated test retains the name, not the resolved password. When Plantain
observes the value during resolution, it registers that value for sanitization
throughout the scenario.

## Decide where a value belongs

| Value type | Recommended source |
| --- | --- |
| Application URL or non-secret environment choice | Process environment or local `.env` |
| Application username, password, or token | Approved environment or secret-injection system |
| Database URL, username, and password | Separate approved environment values |
| Hosted agent credential | Process environment or dashboard session Settings |
| Optional integration credential | Process environment or dashboard session Settings |
| Test behavior and expected outcome | Plain-language intent |

Do not put resolved credentials in the intent, generated artifact, test name,
source path, tag, log, or documentation.

## Supply values to one command

Prefix a local run with the required values:

```bash
SAUCE_DEMO_URL="https://www.saucedemo.com" \
SAUCE_DEMO_USERNAME="standard_user" \
SAUCE_DEMO_PASSWORD="secret_sauce" \
uv run --locked --all-extras plantain --no-dotenv run \
  scenarios/discovery/saucedemo_checkout_journey.yaml
```

This is useful for public demonstrations and short-lived local sessions. For real
credentials, use your shell, workstation, or CI secret mechanism rather than
placing values in scripts or shell history.

## Use a local dotenv file

By default, `run` and `dashboard` look for `.env` in the selected project root.
Values already present in the process environment take precedence.

Use `.env.example` as the documented list of supported names. Keep the real `.env`
local and ignored by Git.

Plantain does not preload `.env` for `validate` because validation does not execute
live targets.

## Skip dotenv loading

Add `--no-dotenv` before the command name:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard
```

This skips the project `.env` file. It does not remove values already present in
the process, and it does not prevent you from entering supported agent or result
integration credentials in dashboard **Settings** for the active session.

## Dashboard session values

The dashboard supports process-lifetime session profiles for:

- agent provider configuration and credentials;
- Allure enablement;
- Zephyr publishing, endpoint, credential, and attachment choices.

These profiles are backend-only overlays. They are not a general-purpose store for
application passwords or database connections.

Restarting the dashboard discards session-entered values and returns to the process
environment.

## Keep database values separate

A database source has distinct URL, username, and password references. Do not
embed credentials in the database URL.

For example, a generated source may refer to:

- `APP_DB_URL`;
- `APP_DB_USERNAME`;
- `APP_DB_PASSWORD`.

Plantain resolves them only during execution and applies database-specific target,
TLS, timeout, pool, row, column, and byte limits.

## Use CI secrets

In continuous integration:

1. store credentials in the CI platform’s secret manager;
2. expose them only to the test job that needs them;
3. run Plantain with `--no-dotenv`;
4. avoid printing the environment;
5. retain only approved sanitized outputs;
6. remove credentials from child processes that do not need them.

See [CI/CD](../operations/ci-cd.md) for a complete workflow pattern.

## Use descriptive names

Choose names that identify purpose without revealing value:

- `STAGING_API_TOKEN`;
- `CHECKOUT_TEST_USERNAME`;
- `ORDERS_DB_PASSWORD`.

Avoid putting account values, host secrets, ticket data, or personal information
inside the name itself.

Plantain also uses `sensitive-keys.txt` and configured sensitive-name fragments to
recognize protected fields. These controls complement environment references; they
are not permission to embed secrets.

## If a value is reported missing

- Confirm the generated step references the intended name.
- Confirm the value exists in the process that started Plantain.
- Confirm `--project-root` points to the expected project.
- If using `.env`, confirm you did not launch with `--no-dotenv`.
- If using dashboard Settings, confirm the session profile is still ready.
- Restart the dashboard after changing its parent process environment.

Never resolve a missing-value error by pasting a credential into the test artifact.

Next, configure optional [results integrations](integrations.md) or review
[network access](network.md).
