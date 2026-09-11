"""Command-line interface for scenario validation and execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv

from plantain.activities import register_framework_activities
from plantain.activities.database.human_mutation import (
    HUMAN_MUTATION_ACKNOWLEDGEMENT,
    HumanMutationError,
    execute_human_mutation_file,
)
from plantain.config import Settings
from plantain.dashboard.launcher import launch_dashboard
from plantain.dashboard.runtime_contract import DEFAULT_DASHBOARD_PORT
from plantain.engine.loader import discover_scenario_paths, load_scenario
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioRunner
from plantain.engine.selection import ScenarioTagFilter, select_scenarios
from plantain.errors import PlantainError
from plantain.models.scenario import ScenarioDefinition
from plantain.observability import configure_logging, get_logger
from plantain.security.redaction import RedactionPolicy

MAX_PARAMETER_FILE_BYTES = 1_000_000
MAX_MUTATION_PARAMETERS = 1_000
_RUNTIME_COMMANDS = frozenset({"dashboard", "db-mutate", "run"})


def _stdout(value: str) -> None:
    sys.stdout.write(f"{value}\n")


def _stderr(value: str) -> None:
    sys.stderr.write(f"{value}\n")


def _add_tag_filters(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="required_tags",
        metavar="TAG",
        help="Require this tag; repeat to require every supplied tag.",
    )
    command.add_argument(
        "--tag-any",
        action="append",
        default=[],
        dest="any_tags",
        metavar="TAG",
        help="Require at least one of these tags; repeat to add alternatives.",
    )
    command.add_argument(
        "--exclude-tag",
        action="append",
        default=[],
        dest="excluded_tags",
        metavar="TAG",
        help="Exclude scenarios carrying this tag; repeat to exclude more tags.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plantain",
        description="Run YAML-driven UI, API, and database automation.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project containing scenarios, snapshots, and output directories.",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Do not load PROJECT_ROOT/.env into the process environment.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="Execute one or more scenario YAML files.")
    run.add_argument(
        "scenarios",
        nargs="+",
        type=Path,
        help="Scenario YAML files or directories below the project scenarios directory.",
    )
    run.add_argument("--concurrency", type=int, default=1)
    _add_tag_filters(run)
    run.add_argument(
        "--json",
        action="store_true",
        help="Print complete sanitized scenario results as JSON.",
    )

    validate = subcommands.add_parser(
        "validate",
        help="Validate scenario YAML without executing it.",
    )
    validate.add_argument(
        "scenarios",
        nargs="+",
        type=Path,
        help="Scenario YAML files or directories below the project scenarios directory.",
    )
    _add_tag_filters(validate)

    subcommands.add_parser("activities", help="List registered YAML activities.")

    dashboard = subcommands.add_parser(
        "dashboard",
        help="Start the local Plantain dashboard on loopback.",
    )
    dashboard.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help="Loopback port for the combined dashboard frontend and backend.",
    )

    mutation = subcommands.add_parser(
        "db-mutate",
        help="Human-only execution of one reviewed DML file; never available to scenarios.",
    )
    mutation.add_argument("sql_file", type=Path)
    mutation.add_argument(
        "--parameters",
        type=Path,
        help="Optional JSON object containing named SQL bind parameters.",
    )
    mutation.add_argument(
        "--acknowledge",
        required=True,
        help=f'Exact required phrase: "{HUMAN_MUTATION_ACKNOWLEDGEMENT}"',
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    project_root = args.project_root.expanduser().resolve()
    runtime_command = args.command in _RUNTIME_COMMANDS
    if runtime_command and not args.no_dotenv:
        load_dotenv(project_root / ".env", override=False)
    settings = Settings.from_env(project_root)
    if args.command != "db-mutate":
        settings = replace(settings, allow_db_mutations=False)
    if runtime_command:
        settings.ensure_runtime_directories()
        configure_logging(
            settings.output_dir,
            settings.log_level,
            sensitive_keys=settings.sensitive_key_names,
        )
    return settings


def _dashboard(args: argparse.Namespace, settings: Settings) -> int:
    return launch_dashboard(
        settings,
        announce=_stdout,
        port=args.port,
    )


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    runner = ScenarioRunner(settings)
    redaction = RedactionPolicy(settings.sensitive_key_names)
    definitions = _selected_scenarios(args, settings)
    results = await runner.run_many(definitions, concurrency=args.concurrency)
    failed = False
    for definition, result in zip(definitions, results, strict=True):
        if isinstance(result, BaseException):
            failed = True
            safe_error = redaction.redact_text(str(result))
            _stderr(f"FAILED {definition.scenario}: {safe_error}")
        elif args.json:
            _stdout(json.dumps(result.sanitized_dict(), ensure_ascii=False))
        else:
            _stdout(
                f"PASSED {result.scenario} ({result.duration_ms} ms, {len(result.steps)} steps)"
            )
            if result.outputs:
                _stdout(json.dumps({"outputs": result.outputs}, indent=2, ensure_ascii=False))
    return 1 if failed else 0


def _validate(args: argparse.Namespace, settings: Settings) -> int:
    runner = ScenarioRunner(settings)
    for definition in _selected_scenarios(args, settings):
        source = definition.source_path or definition.scenario
        try:
            runner.validate_scenario(definition)
        except PlantainError as exc:
            raise PlantainError(f"Invalid scenario {source}: {exc}") from exc
        _stdout(f"VALID {source}: {len(definition.steps)} steps")
    return 0


def _selected_scenarios(
    args: argparse.Namespace,
    settings: Settings,
) -> list[ScenarioDefinition]:
    paths = discover_scenario_paths(args.scenarios, settings)
    definitions = [load_scenario(path, settings) for path in paths]
    tag_filter = ScenarioTagFilter.create(
        required_all=getattr(args, "required_tags", ()),
        required_any=getattr(args, "any_tags", ()),
        excluded=getattr(args, "excluded_tags", ()),
    )
    return select_scenarios(definitions, tag_filter)


def _activities() -> int:
    registry = ActivityRegistry()
    register_framework_activities(registry)
    _stdout(json.dumps(registry.describe(), indent=2))
    return 0


async def _db_mutate(args: argparse.Namespace, settings: Settings) -> int:
    parameters = _mutation_parameters(args.parameters, settings.project_root)
    result = await execute_human_mutation_file(
        settings,
        sql_file=args.sql_file,
        parameters=parameters,
        acknowledgement=args.acknowledge,
    )
    _stdout(json.dumps(asdict(result), indent=2))
    return 0


def _mutation_parameters(path: Path | None, project_root: Path) -> dict[str, object]:
    if path is None:
        return {}
    candidate = path.expanduser()
    target = (
        (project_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    )
    try:
        target.relative_to(project_root)
    except ValueError as exc:
        raise HumanMutationError(
            "Mutation parameter files must stay inside the project root"
        ) from exc
    if not target.is_file():
        raise HumanMutationError("Mutation parameter file is missing")
    try:
        with target.open("rb") as handle:
            payload = handle.read(MAX_PARAMETER_FILE_BYTES + 1)
    except OSError as exc:
        raise HumanMutationError("Mutation parameters must be a readable JSON object") from exc
    if len(payload) > MAX_PARAMETER_FILE_BYTES:
        raise HumanMutationError("Mutation parameter file exceeds 1 MB")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HumanMutationError("Mutation parameters must be a readable JSON object") from exc
    if not isinstance(value, dict) or len(value) > MAX_MUTATION_PARAMETERS:
        raise HumanMutationError("Mutation parameters must be a JSON object with at most 1000 keys")
    return value


def main() -> NoReturn:
    args = _parser().parse_args()
    settings: Settings | None = None
    try:
        if args.command == "activities":
            status = _activities()
        else:
            settings = _settings(args)
            if args.command == "run":
                status = asyncio.run(_run(args, settings))
            elif args.command == "dashboard":
                status = _dashboard(args, settings)
            elif args.command == "validate":
                status = _validate(args, settings)
            else:
                status = asyncio.run(_db_mutate(args, settings))
    except (HumanMutationError, PlantainError, OSError, ValueError) as exc:
        sensitive_keys = settings.sensitive_key_names if settings is not None else ()
        safe_error = RedactionPolicy(sensitive_keys).redact_text(str(exc))
        get_logger("cli").error("Command failed: %s", safe_error)
        _stderr(f"ERROR: {safe_error}")
        status = 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary prevents raw traceback disclosure.
        safe_error = f"Command failed safely ({type(exc).__name__})"
        get_logger("cli").error("%s", safe_error)
        _stderr(f"ERROR: {safe_error}")
        status = 2
    raise SystemExit(status)
