"""Safe deterministic paths for per-scenario result persistence."""

from __future__ import annotations

import os
import re
from pathlib import Path

from plantain.errors import ActivityExecutionError

FALLBACK_REPORT_SLUG = "scenario"
MAX_REPORT_SLUG_LENGTH = 100
RUN_ID_LENGTH = 32
_RUN_ID_PATTERN = re.compile(rf"^[0-9a-f]{{{RUN_ID_LENGTH}}}$")


def is_valid_report_run_id(value: object) -> bool:
    """Return whether a value is a canonical native-result run identifier."""

    return isinstance(value, str) and _RUN_ID_PATTERN.fullmatch(value) is not None


def _lexical_absolute(path: Path) -> Path:
    # Path.resolve() is intentionally forbidden here because it hides symlink components.
    return Path(os.path.abspath(path))  # noqa: PTH100


def scenario_report_target(
    output_dir: Path,
    *,
    scenario: str,
    source_path: str | None,
    run_id: str,
) -> Path:
    """Resolve one immutable run target without permitting directory escape."""

    report_root = _lexical_absolute(output_dir / "results")
    if not is_valid_report_run_id(run_id):
        raise ActivityExecutionError("Scenario report run identifier is invalid")
    if source_path is None:
        slug = "".join(character.lower() if character.isalnum() else "_" for character in scenario)
        normalized = "_".join(filter(None, slug.split("_")))[:MAX_REPORT_SLUG_LENGTH]
        report_directory = report_root / (normalized or FALLBACK_REPORT_SLUG)
    else:
        source = Path(source_path)
        if source.is_absolute() or source.suffix.lower() not in {".yaml", ".yml"}:
            raise ActivityExecutionError("Scenario report source path is invalid")
        report_directory = _lexical_absolute((report_root / source).with_suffix(""))
    try:
        report_directory.relative_to(report_root)
    except ValueError as exc:
        raise ActivityExecutionError("Scenario report path escapes its output directory") from exc
    return report_directory / f"{run_id}.result.json"


__all__ = ["RUN_ID_LENGTH", "is_valid_report_run_id", "scenario_report_target"]
