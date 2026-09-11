"""Native report paths preserve containment and private-path validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from plantain.engine.report_store import scenario_report_target
from plantain.errors import ActivityExecutionError, AtomicPersistenceError
from plantain.persistence import write_json_atomic

RUN_ID = "a" * 32
SECOND_RUN_ID = "b" * 32
EXPECTED_UNIQUE_TARGETS = 2


def test_report_target_normalizes_nested_source_without_escape(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"

    target = scenario_report_target(
        output_dir,
        scenario="Checkout",
        source_path="ui/checkout.yaml",
        run_id=RUN_ID,
    )

    assert target == output_dir / f"results/ui/checkout/{RUN_ID}.result.json"
    with pytest.raises(ActivityExecutionError, match="escapes"):
        scenario_report_target(
            output_dir,
            scenario="Escape",
            source_path="../escape.yaml",
            run_id=RUN_ID,
        )


def test_report_target_preserves_symlink_for_private_writer_rejection(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output_dir / "results").symlink_to(outside, target_is_directory=True)
    target = scenario_report_target(
        output_dir,
        scenario="Checkout",
        source_path=None,
        run_id=RUN_ID,
    )

    with pytest.raises(AtomicPersistenceError, match="symbolic link"):
        write_json_atomic(target, {"status": "passed"})

    assert not (outside / "checkout" / f"{RUN_ID}.result.json").exists()


def test_report_targets_are_unique_per_run(tmp_path: Path) -> None:
    targets = {
        scenario_report_target(
            tmp_path / "output",
            scenario="Checkout",
            source_path="ui/checkout.yaml",
            run_id=run_id,
        )
        for run_id in (RUN_ID, SECOND_RUN_ID)
    }

    assert len(targets) == EXPECTED_UNIQUE_TARGETS


@pytest.mark.parametrize("run_id", ["", "a" * 31, "A" * 32, "../unsafe-run"])
def test_report_target_rejects_invalid_run_identifier(
    tmp_path: Path,
    run_id: str,
) -> None:
    with pytest.raises(ActivityExecutionError, match="identifier is invalid"):
        scenario_report_target(
            tmp_path / "output",
            scenario="Checkout",
            source_path=None,
            run_id=run_id,
        )
