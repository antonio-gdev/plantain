# Install Plantain

Plantain runs directly from its Python source checkout. You do not need to download
or trust a prebuilt executable, and you do not need to build the project before
starting the dashboard.

## Before you start

You need:

- Python 3.11, 3.12, 3.13, or 3.14;
- `uv` 0.12.5;
- a local copy of the Plantain repository;
- permission to access any application, API, or database you plan to test.

Live targets and credentials are not required to install Plantain or validate the
included scenarios.

## Supported platforms

Plantain runs natively on Linux and macOS.

On Windows, run Plantain inside WSL2 or a Linux container. Install Python, `uv`,
Plantain, and any browser or database dependencies inside that Linux environment,
then run Plantain commands from its shell.

Native Windows is not currently supported. Plantain’s private-artifact persistence
verifies POSIX ownership and exact owner-only permissions; equivalent native
Windows support requires a dedicated Windows ACL backend that has not yet been
implemented. Plantain fails explicitly instead of silently weakening those
protections.

## 1. Open the project

Clone or download the Plantain repository, then open a terminal in its root
directory:

```bash
cd plantain
```

The project root contains `pyproject.toml`, `uv.lock`, `scenarios/`, and
`zensical.toml`.

## 2. Install the locked environment

```bash
uv sync --locked --all-extras
```

This installs the CLI, dashboard, supported database drivers, browser integration,
documentation tooling, and contributor checks from the committed lockfile.

`--locked` prevents the installation from silently selecting different dependency
versions.

## 3. Validate the installation

```bash
uv run --locked --all-extras plantain --help
```

Then validate the included scenario collection without contacting live targets:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
```

A successful validation confirms that the runtime, activity contracts, and
checked-in scenario artifacts can be loaded locally.

## 4. Start the dashboard

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard
```

Open:

```text
http://127.0.0.1:3000
```

The dashboard frontend and its local API share that one loopback port. Plantain
stages writable dashboard runtime files under ignored `output/`; it does not require
a package build first.

[Continue with the dashboard guide](dashboard.md).

## Browser setup

Chromium is the default browser. When browser auto-installation is enabled, the
first UI test installs only the selected Playwright browser if it is missing.

To install Chromium yourself:

```bash
uv run --locked python -m playwright install chromium
```

Plantain also supports Firefox and WebKit through `PLANTAIN_BROWSER`. Browser
installation is browser-specific, lock-protected, and excludes application
credentials from the child process. Plantain never installs privileged
operating-system packages automatically.

## Understand `--no-dotenv`

`--no-dotenv` tells Plantain not to load values from a project `.env` file. It does
not erase variables already supplied to the command:

```bash
APP_URL="https://example.test" \
uv run --locked --all-extras plantain --no-dotenv dashboard
```

In the dashboard, supported agent and integration credentials can also be entered
through **Settings** for the current session. Application credentials still belong
in the process environment and are referenced by name in generated tests.

## No build is required for normal use

Use `uv build` only when you intentionally need Python wheel and source-distribution
artifacts. Everyday CLI and dashboard use runs directly through the locked source
environment:

```bash
uv run --locked --all-extras plantain ...
```

## Keep local runtime data out of Git

Do not commit:

- `output/` results, logs, and dashboard runtime;
- `.playwright-driver/` browser binaries;
- `.venv/`;
- semantic snapshots;
- credentials or local environment files.

These paths are runtime state, not source artifacts.

Next, [create your first test](first-test.md).
