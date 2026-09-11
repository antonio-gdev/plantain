"""Verified database discovery runs remain bounded backend-only evidence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from plantain.dashboard import database_evidence
from plantain.dashboard.agent.database_models import (
    DatabaseDiscoveryRequest,
    DatabaseRunEvidence,
    DatabaseSourceReferences,
)
from plantain.dashboard.database_evidence import (
    DatabaseEvidenceError,
    load_database_run_evidence,
)
from plantain.models.database import DatabaseDiscoveryResult
from plantain.persistence import write_json_atomic

RUN_ID = "a" * 32
STARTED_AT_MS = 1_756_742_400_000
YAML_MAX_BYTES = 100_000
YAML_MAX_DEPTH = 100
YAML_MAX_NODES = 10_000


def _reference(name: str) -> str:
    return f"env:{name}"


def _source() -> dict[str, str]:
    return DatabaseSourceReferences(
        username=_reference("APP_DB_USERNAME"),
        password=_reference("APP_DB_PASSWORD"),
        db_url=_reference("APP_DB_URL"),
    ).model_dump(by_alias=True)


def _request() -> DatabaseDiscoveryRequest:
    return DatabaseDiscoveryRequest(phase="schemas")


def _settings(
    root: Path,
    *,
    sensitive_keys: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=root,
        scenarios_dir=root / "scenarios",
        output_dir=root / "output",
        allure_results_enabled=False,
        zephyr_publish_results=False,
        zephyr_base_url="",
        zephyr_attach_report=False,
        zephyr_attachment_data_governance_approved=False,
        yaml_max_bytes=YAML_MAX_BYTES,
        yaml_max_depth=YAML_MAX_DEPTH,
        yaml_max_nodes=YAML_MAX_NODES,
        sensitive_key_names=sensitive_keys,
    )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    sensitive_keys: tuple[str, ...] = (),
) -> SimpleNamespace:
    settings = _settings(root, sensitive_keys=sensitive_keys)
    settings.scenarios_dir.mkdir(parents=True)
    monkeypatch.setattr(
        database_evidence.Settings,
        "from_env",
        lambda _root: settings,
    )
    return settings


def _write_scenario(
    settings: SimpleNamespace,
    *,
    source: dict[str, str] | None = None,
) -> Path:
    target = settings.scenarios_dir / "generated/database/discover-schemas.yaml"
    write_json_atomic(
        target,
        {
            "scenario": "Discover database schemas",
            "steps": [
                {
                    "discoverDatabase": {
                        "id": "discover_schemas",
                        "source": source or _source(),
                        "phase": "schemas",
                    }
                }
            ],
            "outputs": {"discovery": "${discover_schemas}"},
        },
    )
    return target


def _discovery(
    *,
    dialect: str = "postgresql",
    schemas: list[str] | None = None,
    has_more: bool = False,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    values = schemas if schemas is not None else ["public"]
    return {
        "success": True,
        "phase": "schemas",
        "dialect": dialect,
        "schema": None,
        "schemas": values,
        "objects": [],
        "tableMetadata": None,
        "itemCount": len(values),
        "hasMore": has_more,
        "nextCursor": next_cursor,
    }


def _report(
    discovery: dict[str, Any],
    *,
    status: str = "passed",
    scenario: str = "Discover database schemas",
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "status": status,
        "duration_ms": 20,
        "started_at_ms": STARTED_AT_MS,
        "correlation_id": RUN_ID,
        "source_path": "generated/database/discover-schemas.yaml",
        "jira_ticket": None,
        "test_case_key": None,
        "test_run_key": None,
        "tags": ["database"],
        "steps": [
            {
                "activity": "discoverDatabase",
                "step_id": "discover_schemas",
                "status": status,
                "duration_ms": 10,
            }
        ],
        "failure": None,
        "artifacts": [],
        "operations": [],
        "outputs": {"discovery": discovery},
        "integrations": {},
    }


def _write_report(settings: SimpleNamespace, value: dict[str, Any]) -> None:
    write_json_atomic(
        settings.output_dir
        / "results/generated/database/discover-schemas"
        / f"{RUN_ID}.result.json",
        value,
    )


def test_passed_discovery_loads_only_metadata_and_environment_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_scenario(settings)
    _write_report(settings, _report(_discovery()))

    evidence = load_database_run_evidence(tmp_path, RUN_ID)

    assert evidence.run_id == RUN_ID
    assert evidence.source.db_url == _reference("APP_DB_URL")
    assert evidence.source.password == _reference("APP_DB_PASSWORD")
    assert evidence.request.phase == "schemas"
    assert evidence.discovery.schemas == ["public"]
    assert evidence.discovery.table_metadata is None
    assert "result" not in evidence.model_dump()


def test_literal_database_source_is_never_retained_as_agent_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    source = _source()
    source["password"] = f"value:{RUN_ID}"
    _write_scenario(settings, source=source)
    _write_report(settings, _report(_discovery()))

    with pytest.raises(DatabaseEvidenceError, match="unavailable or invalid"):
        load_database_run_evidence(tmp_path, RUN_ID)


def test_failed_or_mismatched_run_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_scenario(settings)
    _write_report(settings, _report(_discovery(), status="failed"))

    with pytest.raises(DatabaseEvidenceError, match="must come from a passed run"):
        load_database_run_evidence(tmp_path, RUN_ID)

    _write_report(settings, _report(_discovery(), scenario="Different scenario"))
    with pytest.raises(DatabaseEvidenceError, match="does not match its scenario"):
        load_database_run_evidence(tmp_path, RUN_ID)


def test_protected_metadata_is_not_forwarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = "classified_schema"
    settings = _configure(
        monkeypatch,
        tmp_path,
        sensitive_keys=("classified",),
    )
    _write_scenario(settings)
    _write_report(settings, _report(_discovery(schemas=[protected])))

    with pytest.raises(DatabaseEvidenceError, match="protected metadata") as captured:
        load_database_run_evidence(tmp_path, RUN_ID)

    assert protected not in str(captured.value)


@pytest.mark.parametrize(
    "discovery",
    [
        _discovery(dialect="sqlite"),
        _discovery(has_more=True),
        _discovery(next_cursor="cursor-without-more"),
    ],
)
def test_evidence_model_rejects_unsupported_or_inconsistent_results(
    discovery: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        DatabaseRunEvidence(
            run_id=RUN_ID,
            scenario="Discover database schemas",
            source_path="generated/database/discover-schemas.yaml",
            source=DatabaseSourceReferences(
                username=_reference("APP_DB_USERNAME"),
                password=_reference("APP_DB_PASSWORD"),
                db_url=_reference("APP_DB_URL"),
            ),
            request=_request(),
            discovery=DatabaseDiscoveryResult.model_validate(discovery),
        )
