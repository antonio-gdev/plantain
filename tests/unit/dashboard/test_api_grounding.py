"""Schema-first grounding for dashboard-guided API test creation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from plantain.activities.api.client import ApiActivityError
from plantain.activities.api.openapi import LoadedOpenApi
from plantain.dashboard.agent import api_grounding
from plantain.dashboard.agent.api_grounding import (
    ApiGroundingError,
    ground_cached_api_operation,
    inspect_api_contract,
)
from plantain.dashboard.agent.api_models import (
    ApiSchemaHeaderReference,
    ApiWorkflowKind,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentAction,
    IntentDecision,
    UserIntent,
)
from plantain.security.secrets import SecretRegistry

SCHEMA_ID = "a" * 64
UNKNOWN_OPERATION_KEY = "f" * 64
SCHEMA_URL = "https://api.example.test/openapi.json"
EXPECTED_OPERATION_MATCHES = 2
OBSERVED_VALUE = "private-schema-credential"


class _Store:
    def __init__(
        self,
        loaded: LoadedOpenApi,
        *,
        error: ApiActivityError | None = None,
    ) -> None:
        self.loaded = loaded
        self.error = error
        self.url_calls: list[tuple[str, dict[str, str]]] = []
        self.id_calls: list[str] = []

    async def load_url(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> LoadedOpenApi:
        self.url_calls.append((url, dict(headers or {})))
        if self.error is not None:
            raise self.error
        return self.loaded

    async def load_id(self, schema_id: str) -> LoadedOpenApi:
        self.id_calls.append(schema_id)
        if self.error is not None:
            raise self.error
        return self.loaded


class _Services:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def openapi_store(self) -> _Store:
        return self.store


def _document() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "security": [{"ApiKey": []}],
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "summary": "List available pets",
                    "tags": ["pets"],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Pet"},
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/pets/{petId}": {
                "parameters": [{"$ref": "#/components/parameters/PetId"}],
                "get": {
                    "operationId": "getPet",
                    "summary": "Get one pet",
                    "tags": ["pets"],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                            }
                        }
                    },
                },
            },
        },
        "components": {
            "parameters": {
                "PetId": {
                    "name": "petId",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer"},
                }
            },
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                }
            },
            "securitySchemes": {
                "ApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                }
            },
        },
    }


def _loaded(
    tmp_path: Path,
    *,
    document: dict[str, Any] | None = None,
    source_url: str = SCHEMA_URL,
) -> LoadedOpenApi:
    return LoadedOpenApi(
        schema_id=SCHEMA_ID,
        schema_version="3.0.3",
        source_url=source_url,
        base_url="https://api.example.test",
        cache_file=tmp_path / f"{SCHEMA_ID}.json",
        document=document or _document(),
    )


def _decision(
    *,
    reference: str = SCHEMA_URL,
    query: str = "listPets",
    headers: list[ApiSchemaHeaderReference] | None = None,
) -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.API_CONTRACT,
        summary="Create a schema-grounded pet test.",
        plan_steps=["Inspect matching operations.", "Create a validated scenario."],
        api_workflow=ApiWorkflowKind.CONTRACT,
        api_schema_reference=reference,
        api_schema_headers=headers or [],
        api_operation_query=query,
    )


def _intent(
    *,
    reference: str = SCHEMA_URL,
    query: str = "listPets",
    header_reference: str | None = None,
) -> UserIntent:
    header = f" using {header_reference}" if header_reference is not None else ""
    return UserIntent(prompt=f"Use {reference}{header} to test {query}.")


def test_unique_operation_produces_bounded_contract_evidence(tmp_path: Path) -> None:
    store = _Store(_loaded(tmp_path))
    grounding = asyncio.run(
        inspect_api_contract(
            cast("Any", _Services(store)),
            SecretRegistry(),
            _intent(),
            _decision(),
        )
    )

    assert grounding.inspection is None
    assert grounding.evidence is not None
    payload = json.loads(grounding.evidence.content)
    assert payload["operation"]["operationId"] == "listPets"
    assert payload["operation"]["method"] == "GET"
    assert payload["localReferences"]["#/components/schemas/Pet"]["type"] == "object"
    assert payload["securitySchemes"][0]["schemeName"] == "ApiKey"
    assert payload["securitySchemes"][0]["definition"]["in"] == "header"
    effective = payload["operation"]["effectiveSecurity"]
    assert effective[0]["schemes"][0]["schemeName"] == "ApiKey"
    assert store.url_calls == [(SCHEMA_URL, {})]


def test_ambiguous_and_empty_queries_return_safe_deterministic_choices(
    tmp_path: Path,
) -> None:
    source = f"{SCHEMA_URL}?token={OBSERVED_VALUE}"
    store = _Store(_loaded(tmp_path, source_url=source))
    ambiguous = asyncio.run(
        inspect_api_contract(
            cast("Any", _Services(store)),
            SecretRegistry(("token",)),
            _intent(reference=source, query="pets"),
            _decision(reference=source, query="pets"),
        )
    )
    empty = asyncio.run(
        inspect_api_contract(
            cast("Any", _Services(store)),
            SecretRegistry(),
            _intent(query="orders"),
            _decision(query="orders"),
        )
    )

    assert ambiguous.evidence is None
    assert ambiguous.inspection is not None
    assert [item.operation_id for item in ambiguous.inspection.operations] == [
        "listPets",
        "getPet",
    ]
    assert ambiguous.inspection.total_matches == EXPECTED_OPERATION_MATCHES
    assert OBSERVED_VALUE not in ambiguous.inspection.source_url
    assert empty.inspection is not None
    assert empty.inspection.operations == []
    assert empty.inspection.total_matches == 0


def test_schema_header_environment_reference_is_resolved_and_redacted(
    tmp_path: Path,
) -> None:
    reference = "env:PETSTORE_SCHEMA_URL"
    header_reference = "env:PETSTORE_SCHEMA_TOKEN"
    header = ApiSchemaHeaderReference(
        name="Authorization",
        value=header_reference,
    )
    store = _Store(_loaded(tmp_path))
    secrets = SecretRegistry(("token",))

    grounding = asyncio.run(
        inspect_api_contract(
            cast("Any", _Services(store)),
            secrets,
            _intent(
                reference=reference,
                header_reference=header_reference,
            ),
            _decision(reference=reference, headers=[header]),
            environ={
                "PETSTORE_SCHEMA_URL": SCHEMA_URL,
                "PETSTORE_SCHEMA_TOKEN": OBSERVED_VALUE,
            },
        )
    )

    assert store.url_calls == [(SCHEMA_URL, {"Authorization": OBSERVED_VALUE})]
    assert grounding.evidence is not None
    assert OBSERVED_VALUE not in grounding.evidence.content


def test_grounding_rejects_unproven_inputs_external_refs_and_oversize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = cast("Any", _Services(_Store(_loaded(tmp_path))))
    with pytest.raises(ApiGroundingError, match="not supplied by the user"):
        asyncio.run(
            inspect_api_contract(
                services,
                SecretRegistry(),
                UserIntent(prompt="Test listPets."),
                _decision(),
            )
        )

    external = _document()
    external["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = {"$ref": "https://external.example.test/Pet.json"}
    with pytest.raises(ApiGroundingError, match="External API schema references"):
        asyncio.run(
            inspect_api_contract(
                cast("Any", _Services(_Store(_loaded(tmp_path, document=external)))),
                SecretRegistry(),
                _intent(),
                _decision(),
            )
        )

    monkeypatch.setattr(api_grounding, "MAX_API_CONTRACT_EVIDENCE_BYTES", 1)
    with pytest.raises(ApiGroundingError, match="too large"):
        asyncio.run(
            inspect_api_contract(
                services,
                SecretRegistry(),
                _intent(),
                _decision(),
            )
        )


def test_cached_selection_is_reverified_against_the_contract(tmp_path: Path) -> None:
    store = _Store(_loaded(tmp_path))
    services = cast("Any", _Services(store))
    ambiguous = asyncio.run(
        inspect_api_contract(
            services,
            SecretRegistry(),
            _intent(query="pets"),
            _decision(query="pets"),
        )
    )
    assert ambiguous.inspection is not None
    operation_key = ambiguous.inspection.operations[1].operation_key

    evidence = asyncio.run(
        ground_cached_api_operation(
            services,
            SecretRegistry(),
            SCHEMA_ID,
            operation_key,
        )
    )

    assert json.loads(evidence.content)["operation"]["operationId"] == "getPet"
    assert store.id_calls == [SCHEMA_ID]
    with pytest.raises(ApiGroundingError, match="no longer available"):
        asyncio.run(
            ground_cached_api_operation(
                services,
                SecretRegistry(),
                SCHEMA_ID,
                UNKNOWN_OPERATION_KEY,
            )
        )


def test_provider_cache_failure_is_translated_without_private_detail(
    tmp_path: Path,
) -> None:
    store = _Store(
        _loaded(tmp_path),
        error=ApiActivityError("synthetic private response detail"),
    )

    with pytest.raises(ApiGroundingError, match="could not be inspected safely") as captured:
        asyncio.run(
            inspect_api_contract(
                cast("Any", _Services(store)),
                SecretRegistry(),
                _intent(),
                _decision(),
            )
        )

    assert "synthetic private response detail" not in str(captured.value)
