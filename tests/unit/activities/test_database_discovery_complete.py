"""Complete scoped Inspector-only database discovery coverage."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.engine import Connection

from plantain.activities.database import discovery
from plantain.activities.database.discovery import (
    _bounded_text,
    _column,
    _decode_cursor,
    _encode_cursor,
    _foreign_key,
    _index,
    _portable,
    _portable_mapping,
    _resolve_name,
    discover_metadata,
)
from plantain.activities.database.errors import DatabaseActivityError
from plantain.models.database import (
    DatabaseSource,
    DiscoverDatabaseParams,
    DiscoveryPhase,
)

DUMMY_CREDENTIAL = "synthetic-discovery-credential"
MAX_RESULT_BYTES = 16_384


class CompleteInspector:
    """Configurable reflection facade that never accesses application rows."""

    default_schema_name = "Brands"

    def __init__(self) -> None:
        self.schemas = ["Brands", "pg_catalog", "Brands", "sys"]
        self.tables: list[str] = []
        self.views = ["CatalogView"]
        self.optional_calls: list[str] = []

    def get_schema_names(self) -> list[str]:
        return self.schemas

    def get_table_names(self, *, schema: str | None = None) -> list[str]:
        assert schema == "Brands"
        return self.tables

    def get_view_names(self, *, schema: str | None = None) -> list[str]:
        assert schema == "Brands"
        return self.views

    def get_pk_constraint(self, table_name: str, *, schema: str | None = None) -> Any:
        assert (schema, table_name) == ("Brands", "CatalogView")
        return []

    def get_columns(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        assert (schema, table_name) == ("Brands", "CatalogView")
        return [
            {
                "name": "Label",
                "type": "VARCHAR(100)",
                "nullable": True,
                "computed": {"sqltext": "first_name || last_name"},
                "identity": "generated",
            }
        ]

    def _unsupported(self, name: str) -> None:
        self.optional_calls.append(name)
        raise NotImplementedError

    def get_table_comment(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported("comment")

    def get_foreign_keys(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported("foreign_keys")

    def get_indexes(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported("indexes")

    def get_unique_constraints(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported("unique_constraints")

    def get_check_constraints(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported("check_constraints")


def _source() -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "automation_reader",
            "password": DUMMY_CREDENTIAL,
            "dbUrl": "jdbc:postgresql://db.example.test/clothing",
        }
    )


def _params(**values: Any) -> DiscoverDatabaseParams:
    return DiscoverDatabaseParams.model_validate(
        {"id": "discover_database", "source": _source(), **values}
    )


def _connection() -> Connection:
    return cast("Connection", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))


def _install_inspector(
    monkeypatch: pytest.MonkeyPatch,
    inspector: CompleteInspector,
) -> None:
    monkeypatch.setattr(discovery, "inspect", lambda _connection: inspector)


def test_schema_discovery_includes_system_names_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = CompleteInspector()
    _install_inspector(monkeypatch, inspector)

    filtered = discover_metadata(
        _connection(),
        _params(phase="schemas"),
        max_result_bytes=MAX_RESULT_BYTES,
    )
    complete = discover_metadata(
        _connection(),
        _params(phase="schemas", includeSystemSchemas=True),
        max_result_bytes=MAX_RESULT_BYTES,
    )

    assert filtered.schemas == ["Brands"]
    assert complete.schemas == ["Brands", "pg_catalog", "sys"]
    assert complete.item_count == len(complete.schemas)


def test_server_schema_page_avoids_full_inspector_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = CompleteInspector()
    monkeypatch.setattr(
        inspector,
        "get_schema_names",
        lambda: pytest.fail("Inspector enumeration must not run"),
    )
    _install_inspector(monkeypatch, inspector)
    monkeypatch.setattr(
        discovery,
        "catalog_schema_page",
        lambda *_args, **_kwargs: ["Alpha", "Beta"],
    )

    result = discover_metadata(
        _connection(),
        _params(phase="schemas", pageSize=1),
        max_result_bytes=MAX_RESULT_BYTES,
    )

    assert result.schemas == ["Alpha"]
    assert result.has_more is True
    assert result.next_cursor is not None


def test_table_phase_omits_views_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = CompleteInspector()
    inspector.tables = ["Uno"]
    _install_inspector(monkeypatch, inspector)

    result = discover_metadata(
        _connection(),
        _params(phase="tables", schema="Brands"),
        max_result_bytes=MAX_RESULT_BYTES,
    )

    assert [(item.name, item.type) for item in result.objects] == [("Uno", "table")]


def test_table_metadata_uses_default_schema_and_falls_back_to_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = CompleteInspector()
    _install_inspector(monkeypatch, inspector)

    result = discover_metadata(
        _connection(),
        _params(phase="table", table="catalogview", includeViews=True),
        max_result_bytes=MAX_RESULT_BYTES,
    )

    metadata = result.table_metadata
    assert result.schema_name == "Brands"
    assert metadata is not None
    assert metadata.name == "CatalogView"
    assert metadata.type == "view"
    assert metadata.comment is None
    assert metadata.primary_key.columns == []
    assert metadata.foreign_keys == []
    assert metadata.indexes == []
    assert metadata.unique_constraints == []
    assert metadata.check_constraints == []
    assert metadata.columns[0].computed == {"sqltext": "first_name || last_name"}
    assert metadata.columns[0].identity == {"value": "generated"}
    assert inspector.optional_calls == [
        "comment",
        "foreign_keys",
        "indexes",
        "unique_constraints",
        "check_constraints",
    ]


def test_table_phase_does_not_silently_select_view_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = CompleteInspector()
    _install_inspector(monkeypatch, inspector)

    with pytest.raises(DatabaseActivityError, match="table does not exist"):
        discover_metadata(
            _connection(),
            _params(phase="table", table="CatalogView"),
            max_result_bytes=MAX_RESULT_BYTES,
        )


def test_column_foreign_key_and_index_metadata_are_validated() -> None:
    with pytest.raises(DatabaseActivityError, match="without a name"):
        _column({}, position=1, primary_keys=set())
    with pytest.raises(DatabaseActivityError, match="incomplete foreign-key"):
        _foreign_key({"constrained_columns": ["id"]})

    column = _column(
        {"name": "id", "type": None, "nullable": False, "autoincrement": None},
        position=1,
        primary_keys={"id"},
    )
    index = _index(
        {
            "name": None,
            "column_names": ["id", None],
            "unique": True,
            "expressions": ["lower(name)"],
        }
    )
    assert column.type == "unknown"
    assert column.primary_key is True
    assert index.columns == ["id", None]
    assert index.unique is True
    assert index.expressions == ["lower(name)"]


def test_name_resolution_handles_required_exact_folded_ambiguous_and_missing() -> None:
    assert _resolve_name(["Brands"], "Brands", kind="schema") == "Brands"
    assert _resolve_name(["Brands"], "brands", kind="schema") == "Brands"
    with pytest.raises(DatabaseActivityError, match="is required"):
        _resolve_name(["Brands"], None, kind="schema")
    with pytest.raises(DatabaseActivityError, match="case-ambiguous"):
        _resolve_name(["Brands", "BRANDS"], "brands", kind="schema")
    with pytest.raises(DatabaseActivityError, match="does not exist"):
        _resolve_name(["Brands"], "missing", kind="schema")


def _cursor(value: Any) -> str:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def test_cursor_round_trip_and_invalid_encoding() -> None:
    encoded = _encode_cursor(DiscoveryPhase.TABLES, "tables\0Brands", ("uno", "Uno"))
    assert _decode_cursor(
        encoded,
        phase=DiscoveryPhase.TABLES,
        scope="tables\0Brands",
    ) == ("uno", "Uno")
    assert _decode_cursor(None, phase=DiscoveryPhase.TABLES, scope="tables") is None

    with pytest.raises(DatabaseActivityError, match="cursor is invalid"):
        _decode_cursor("%%%", phase=DiscoveryPhase.TABLES, scope="tables")


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"version": 2, "phase": "tables", "scope": "tables", "key": ["uno"]},
        {"version": 1, "phase": "schemas", "scope": "tables", "key": ["uno"]},
        {"version": 1, "phase": "tables", "scope": "other", "key": ["uno"]},
        {"version": 1, "phase": "tables", "scope": "tables", "key": []},
        {"version": 1, "phase": "tables", "scope": "tables", "key": [1]},
    ],
)
def test_cursor_payload_must_match_phase_scope_and_key(value: Any) -> None:
    with pytest.raises(DatabaseActivityError, match="does not match this request"):
        _decode_cursor(
            _cursor(value),
            phase=DiscoveryPhase.TABLES,
            scope="tables",
        )


def test_portable_metadata_helpers_cover_driver_specific_shapes() -> None:
    assert _portable_mapping(None) is None
    assert _portable_mapping("generated") == {"value": "generated"}
    assert _portable_mapping({7: ("a", "b")}) == {"7": ["a", "b"]}
    assert _portable({"nested": {1, 2}})["nested"] in ([1, 2], [2, 1])
    assert _portable(SimpleNamespace(name="custom")) == "namespace(name='custom')"
    assert _bounded_text(None) is None
    assert _bounded_text(7) == "7"


def test_metadata_text_is_hard_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "MAX_METADATA_TEXT_LENGTH", 3)

    with pytest.raises(DatabaseActivityError, match="safety limit"):
        _bounded_text("four")
