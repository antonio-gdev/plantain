# Run Plantain in CI/CD

Use CI to validate and run reviewed Plantain tests. Create or revise tests through
the agent workflow first, inspect them in **View steps**, and version the accepted
scenario before automation uses it.

CI should not turn an unreviewed agent response directly into a live test.

## Recommended pipeline

Separate the workflow into two decisions:

1. **Validate** generated scenarios without credentials or live target access.
2. **Run** an approved scope in a protected environment with only the secrets it
   needs.

This makes artifact errors visible before a job receives application credentials.

## Minimal validation job

```yaml
name: Plantain validation

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

env:
  UV_FROZEN: "1"
  UV_NO_PROGRESS: "1"

jobs:
  validate:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - name: Check out source
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
        with:
          python-version: "3.14"

      - name: Set up uv
        uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.12.5"

      - name: Install locked dependencies
        run: uv sync --locked --all-extras

      - name: Validate reviewed scenarios
        run: >-
          uv run --locked --all-extras plantain
          --no-dotenv validate scenarios
```

The actions are pinned to immutable commit SHAs so a mutable version tag cannot
change the code executed by the workflow. Review and update those pins through your
normal dependency process.

## Add a protected live-test job

Use a separate job and an approved GitHub environment:

```yaml
  run-smoke:
    needs: validate
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    environment: plantain-test
    env:
      APP_URL: ${{ secrets.APP_URL }}
      APP_USERNAME: ${{ secrets.APP_USERNAME }}
      APP_PASSWORD: ${{ secrets.APP_PASSWORD }}
    steps:
      - name: Check out source
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
        with:
          python-version: "3.14"

      - name: Set up uv
        uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.12.5"

      - name: Install locked dependencies
        run: uv sync --locked --all-extras

      - name: Run approved smoke tests
        run: >-
          uv run --locked --all-extras plantain
          --no-dotenv run scenarios/smoke
```

Replace the example environment names and scope with reviewed project values. Do
not print the environment or place resolved secrets in workflow arguments.

## Prepare UI runners

Plantain can lazily install the selected browser when allowed, but it never installs
privileged operating-system packages automatically.

For predictable CI:

- use a runner image with required browser system libraries;
- install only the selected browser through an approved step;
- or pre-provision `.playwright-driver/` in the controlled runner image;
- keep application credentials out of the browser-installer child environment.

To install Chromium without privileged operating-system changes:

```bash
uv run --locked python -m playwright install chromium
```

API and database-only jobs do not trigger browser provisioning.

## Select a safe scope

Prefer one of:

- a reviewed directory owned by the job;
- required and excluded tags;
- a small explicit file list.

Use `--concurrency` only after confirming that the target, database, and runner can
support the chosen load. Plantain enforces internal admission limits, but those do
not authorize load against a shared environment.

## Handle results deliberately

The job exit status should decide success or failure.

If you upload `output/` as a CI artifact:

- treat it as potentially sensitive;
- restrict artifact access;
- use the shortest approved retention;
- avoid publishing detailed logs to a public job summary;
- upload only the exact result paths required for investigation.

Optional Allure or Zephyr output does not replace the native local result.

## Configure private targets

A CI secret can provide a credential, but it does not authorize a network
destination.

Private, restricted, database, and Zephyr targets require their matching exact
policy plus real deployment egress controls. Configure the runner network before
setting `PLANTAIN_EGRESS_CONTROL_ENFORCED=true`.

See [Network access](../configuration/network.md).

## Contributor quality gates

Before merging runtime changes, Plantain’s complete repository gates are:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
uv run --locked --all-extras ruff check .
uv run --locked --all-extras ruff format --check .
uv run --locked --all-extras mypy src
uv run --locked --all-extras bandit -c pyproject.toml -r src
uv run --locked --all-extras pytest -q -p no:cacheprovider \
  --cov=plantain --cov-report=term-missing --cov-fail-under=80 tests/unit
uv export --locked --all-extras --no-emit-project \
  --output-file /tmp/plantain-audit-requirements.txt
uv run --locked --extra dev pip-audit \
  --requirement /tmp/plantain-audit-requirements.txt \
  --disable-pip --progress-spinner off
uv build
uv run --locked --all-extras python tests/packaging/verify_artifacts.py dist
```

Live UI, API, database, or Zephyr checks additionally require explicitly approved
targets and credentials.

## Build the documentation

Repository documentation uses Zensical:

```bash
uv run --locked --extra docs zensical build \
  --config-file zensical.toml \
  --clean \
  --strict
```

The Pages workflow should build in a clean runner and deploy the static `site/`
artifact through GitHub’s official Pages actions. It does not need a `gh-pages`
branch.
