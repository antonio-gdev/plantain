"""Human database CLI parsing and bounded parameter-file tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plantain import cli
from plantain.activities.database.human_mutation import (
    HUMAN_MUTATION_ACKNOWLEDGEMENT,
    HumanMutationError,
)


def test_mutation_is_a_separate_cli_command_with_required_acknowledgement() -> None:
    args = cli._parser().parse_args(
        [
            "--no-dotenv",
            "db-mutate",
            "reviewed/change.sql",
            "--acknowledge",
            HUMAN_MUTATION_ACKNOWLEDGEMENT,
        ]
    )

    assert args.command == "db-mutate"
    assert args.sql_file == Path("reviewed/change.sql")
    assert args.acknowledge == HUMAN_MUTATION_ACKNOWLEDGEMENT
    assert args.no_dotenv is True


def test_parameter_file_is_project_relative_and_must_be_an_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "parameters.json").write_text(
        json.dumps({"message": "reviewed value", "id": 7}),
        encoding="utf-8",
    )
    invalid = root / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli._mutation_parameters(Path("parameters.json"), root) == {
        "message": "reviewed value",
        "id": 7,
    }
    with pytest.raises(HumanMutationError, match="JSON object"):
        cli._mutation_parameters(Path("invalid.json"), root)


def test_parameter_file_is_contained_bounded_and_key_limited(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    oversized = root / "oversized.json"
    oversized.write_bytes(b" " * (cli.MAX_PARAMETER_FILE_BYTES + 1))
    too_many = root / "too-many.json"
    too_many.write_text(
        json.dumps({f"key_{index}": index for index in range(cli.MAX_MUTATION_PARAMETERS + 1)}),
        encoding="utf-8",
    )

    with pytest.raises(HumanMutationError, match="stay inside"):
        cli._mutation_parameters(outside, root)
    with pytest.raises(HumanMutationError, match="exceeds 1 MB"):
        cli._mutation_parameters(oversized, root)
    with pytest.raises(HumanMutationError, match="at most 1000 keys"):
        cli._mutation_parameters(too_many, root)
