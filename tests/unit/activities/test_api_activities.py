"""YAML-facing API activity orchestration tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api import activities as api_activities
from plantain.activities.api.activities import (
    _bounded_operation_value,
    _request_body,
    call_schema,
    register_api_activities,
    send_request,
    validate_schema,
)
from plantain.activities.api.client import ApiActivityError, BoundedResponse
from plantain.activities.api.openapi import LoadedOpenApi
from plantain.engine.registry import ActivityRegistry
from plantain.models import api as api_models
from plantain.models.api import (
    CallSchemaParams,
    HttpMethod,
    LoadApiSchemaParams,
    SendRequestParams,
    ValidateSchemaParams,
)
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

CREATED = 201
MAX_TRUNCATED_LOG_BYTES = 8_195
PET_ID = 7
SCHEMA_ID = "a" * 64


class FakeApi:
    def __init__(self, response: BoundedResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> BoundedResponse:
        self.calls.append(kwargs)
        return self.response


class FakeStore:
    def __init__(self, loaded: LoadedOpenApi) -> None:
        self.loaded = loaded
        self.schema_ids: list[str] = []
        self.schema_urls: list[tuple[str, dict[str, str]]] = []

    async def load_url(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> LoadedOpenApi:
        self.schema_urls.append((url, headers))
        return self.loaded

    async def load_id(self, schema_id: str) -> LoadedOpenApi:
        self.schema_ids.append(schema_id)
        return self.loaded


class FakeAdmission:
    async def run_process(
        self,
        function: Any,
        /,
        *args: Any,
        limits: object,
        **kwargs: Any,
    ) -> Any:
        del limits
        return function(*args, **kwargs)


class FakeServices:
    def __init__(self, api: FakeApi, store: FakeStore | None = None) -> None:
        self._api = api
        self._store = store
        self.admission = FakeAdmission()

    async def api(self) -> FakeApi:
        return self._api

    async def openapi_store(self) -> FakeStore:
        assert self._store is not None
        return self._store


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def info(self, _message: str, *, extra: dict[str, Any]) -> None:
        self.records.append(extra)

    def error(self, _message: str, *, extra: dict[str, Any]) -> None:
        self.records.append(extra)


def _context(
    api: FakeApi,
    store: FakeStore | None = None,
    *,
    sensitive_keys: tuple[str, ...] = (),
) -> SimpleNamespace:
    context = SimpleNamespace(
        services=FakeServices(api, store),
        settings=SimpleNamespace(),
        scenario=SimpleNamespace(scenario="API unit test"),
        secrets=SecretRegistry(sensitive_keys=sensitive_keys),
        current_activity="apiActivity",
        current_step_id="api_step",
        operations=[],
    )

    def add_operation(**operation: Any) -> None:
        context.operations.append(operation)

    context.add_operation = add_operation
    return context


def _response(status: int, body: Any) -> BoundedResponse:
    content = json.dumps(body).encode()
    return BoundedResponse(
        request_url="https://api.example.test/result",
        method=HttpMethod.POST,
        status_code=status,
        headers={"content-type": "application/json"},
        content=content,
        elapsed_ms=4,
    )


def _logged_response(logger: RecordingLogger) -> dict[str, Any]:
    return next(record for record in logger.records if "response_bytes" in record)


def _swagger(tmp_path: Path) -> LoadedOpenApi:
    document = {
        "swagger": "2.0",
        "host": "api.example.test",
        "basePath": "/v1",
        "schemes": ["https"],
        "paths": {
            "/pets/{petId}": {
                "post": {
                    "operationId": "replacePet",
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "type": "integer",
                        },
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {"$ref": "#/definitions/Pet"},
                        },
                    ],
                    "responses": {"201": {"schema": {"$ref": "#/definitions/Pet"}}},
                }
            }
        },
        "definitions": {
            "Pet": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    return LoadedOpenApi(
        schema_id=SCHEMA_ID,
        schema_version="2.0",
        source_url="https://api.example.test/swagger.json",
        base_url="https://api.example.test/v1",
        cache_file=tmp_path / f"{SCHEMA_ID}.json",
        document=document,
    )


def test_send_request_retains_resolved_request_body_for_chaining() -> None:
    request_body = {"pet": {"name": "Ada"}}
    api = FakeApi(_response(CREATED, {"id": PET_ID}))
    context = _context(api)
    params = SendRequestParams.model_validate(
        {
            "id": "create_pet",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
            "body": request_body,
            "expectedStatus": CREATED,
        }
    )

    result = asyncio.run(send_request(cast("Any", context), params))
    stored = result.model_dump(mode="python", by_alias=True)

    assert stored["requestBody"] == request_body
    assert stored["contentType"] == "application/json"
    assert stored["responseBody"] == {"id": PET_ID}
    assert api.calls[0]["body"] == request_body
    assert context.operations == [
        {
            "domain": "api",
            "phase": "request",
            "operation_type": "POST",
            "target": "https://api.example.test/pets",
            "status": "passed",
            "duration_ms": context.operations[0]["duration_ms"],
            "operation_input": {"headers": {}, "query": {}, "body": request_body},
            "operation_expected": {"statusCode": CREATED},
            "operation_actual": {
                "httpStatus": CREATED,
                "responseBytes": len(api.response.content),
                "elapsedMs": 4,
            },
        }
    ]


def test_send_request_respects_optional_method_policy() -> None:
    api = FakeApi(_response(CREATED, {"id": PET_ID}))
    context = _context(api)
    context.settings.api_allowed_methods = ("GET",)
    params = SendRequestParams.model_validate(
        {
            "id": "blocked_post",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
        }
    )

    with pytest.raises(ApiActivityError, match="PLANTAIN_API_ALLOWED_METHODS"):
        asyncio.run(send_request(cast("Any", context), params))

    assert api.calls == []


def test_response_log_is_nested_redacted_but_context_keeps_raw_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_value = "very-private-token"
    body = {
        "payload": [
            {"access_token": raw_value},
            {"message": "lookup access_token failed"},
        ],
        "opaque": "A" * 80,
        "summary": "Denied",
    }
    api = FakeApi(_response(CREATED, body))
    context = _context(api, sensitive_keys=("access_token",))
    recording_logger = RecordingLogger()
    monkeypatch.setattr(api_activities, "logger", recording_logger)
    params = SendRequestParams.model_validate(
        {
            "id": "create_pet",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
            "expectedStatus": CREATED,
            "expectedMessage": "denied",
        }
    )

    result = asyncio.run(send_request(cast("Any", context), params))

    assert result.response_body == body
    assert _logged_response(recording_logger) == {
        "scenario": "API unit test",
        "activity": "sendRequest",
        "step_id": "create_pet",
        "http_status": CREATED,
        "http_method": "POST",
        "response_bytes": len(api.response.content),
        "response_body_truncated": False,
        "response_body": {
            "payload": [
                {"access_token": REDACTED},
                {"message": REDACTED},
            ],
            "opaque": REDACTED,
            "summary": "Denied",
        },
    }


def test_operation_preview_uses_bounded_redaction_path() -> None:
    secrets = SimpleNamespace(
        redact_log=lambda value: {"safe": value["safe"]},
    )
    context = SimpleNamespace(secrets=secrets)

    assert _bounded_operation_value(context, {"safe": "retained", "raw": "excluded"}) == {
        "safe": "retained"
    }


def test_preflight_failure_records_minimal_sanitized_operation() -> None:
    api = FakeApi(_response(200, {}))
    context = _context(api)
    authored_target = "/private-authored-target"
    params = SendRequestParams.model_validate(
        {
            "id": "preflight_failure",
            "endpoint": authored_target,
            "method": "GET",
            "expectedStatus": 200,
        }
    )

    with pytest.raises(ApiActivityError):
        asyncio.run(send_request(cast("Any", context), params))

    assert api.calls == []
    assert len(context.operations) == 1
    evidence = context.operations[0]
    assert evidence["phase"] == "request_preflight"
    assert evidence["operation_type"] == "prepare"
    assert evidence["target"] == "sendRequest"
    assert evidence["status"] == "failed"
    assert authored_target not in json.dumps(evidence)


def test_response_is_logged_before_status_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi(_response(CREATED, {"message": "Created"}))
    context = _context(api)
    recording_logger = RecordingLogger()
    monkeypatch.setattr(api_activities, "logger", recording_logger)
    params = SendRequestParams.model_validate(
        {
            "id": "unexpected_status",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
            "expectedStatus": 200,
        }
    )

    with pytest.raises(ApiActivityError) as captured:
        asyncio.run(send_request(cast("Any", context), params))

    assert _logged_response(recording_logger)["http_status"] == CREATED
    assert context.operations[-1]["status"] == "failed"
    assert context.operations[-1]["operation_actual"] == {
        "failure_stage": "status_validation",
        "http_status": CREATED,
    }
    assert captured.value.safe_details == {
        "failure_stage": "status_validation",
        "http_status": CREATED,
    }


def test_expected_message_failure_never_echoes_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_message = "private server detail"
    api = FakeApi(_response(CREATED, {"message": server_message}))
    context = _context(api)
    monkeypatch.setattr(api_activities, "logger", RecordingLogger())
    params = SendRequestParams.model_validate(
        {
            "id": "message_check",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
            "expectedStatus": CREATED,
            "expectedMessage": "accepted private value",
        }
    )

    with pytest.raises(ApiActivityError) as captured:
        asyncio.run(send_request(cast("Any", context), params))

    rendered = str(captured.value)
    assert server_message not in rendered
    assert params.expected_message is not None
    assert params.expected_message not in rendered
    assert captured.value.safe_details == {
        "failure_stage": "response_message_validation",
        "http_status": CREATED,
    }


def test_response_body_logging_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi(_response(CREATED, {"message": "Created"}))
    context = _context(api)
    recording_logger = RecordingLogger()
    monkeypatch.setattr(api_activities, "logger", recording_logger)
    params = SendRequestParams.model_validate(
        {
            "id": "quiet_response",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
            "expectedStatus": CREATED,
            "logResponseBody": False,
        }
    )

    asyncio.run(send_request(cast("Any", context), params))

    response_record = _logged_response(recording_logger)
    assert response_record["http_status"] == CREATED
    assert "response_body" not in response_record


def test_large_response_log_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "public response value " * 1_000
    api = FakeApi(_response(CREATED, body))
    context = _context(api)
    recording_logger = RecordingLogger()
    monkeypatch.setattr(api_activities, "logger", recording_logger)
    params = SendRequestParams.model_validate(
        {
            "id": "large_response",
            "endpoint": "https://api.example.test/pets",
            "method": "POST",
            "expectedStatus": CREATED,
        }
    )

    result = asyncio.run(send_request(cast("Any", context), params))

    logged = _logged_response(recording_logger)
    assert result.response_body == body
    assert logged["response_body_truncated"] is True
    assert str(logged["response_body"]).endswith("…")
    assert len(str(logged["response_body"]).encode("utf-8")) <= MAX_TRUNCATED_LOG_BYTES


def test_expected_message_cannot_be_blank() -> None:
    with pytest.raises(ValueError, match="expectedMessage cannot be blank"):
        SendRequestParams.model_validate(
            {
                "id": "blank_message",
                "endpoint": "https://api.example.test/pets",
                "expectedMessage": "   ",
            }
        )


def test_schema_download_rejects_runtime_managed_conditional_headers() -> None:
    with pytest.raises(ValueError, match="managed by the HTTP client"):
        LoadApiSchemaParams.model_validate(
            {
                "id": "schema",
                "schemaUrl": "https://schema.example.test/openapi.json",
                "headers": {"If-None-Match": '"manual-etag"'},
            }
        )


def test_request_models_bound_headers_and_query_without_changing_authored_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = SendRequestParams.model_validate(
        {
            "id": "bounded_request",
            "endpoint": "https://api.example.test/items",
            "headers": {"X-Trace": "visible"},
            "query": {"tag": ["one", "two"]},
        }
    )
    assert params.headers == {"X-Trace": "visible"}

    with pytest.raises(ValueError, match="unique ignoring case"):
        SendRequestParams.model_validate(
            {
                "id": "duplicate_headers",
                "endpoint": "https://api.example.test/items",
                "headers": {"X-Trace": "one", "x-trace": "two"},
            }
        )

    monkeypatch.setattr(api_models, "MAX_HEADER_VALUE_BYTES", 4)
    with pytest.raises(ValueError, match="header value exceeds"):
        SendRequestParams.model_validate(
            {
                "id": "large_header",
                "endpoint": "https://api.example.test/items",
                "headers": {"X-Trace": "12345"},
            }
        )

    monkeypatch.setattr(api_models, "MAX_QUERY_VALUE_BYTES", 4)
    with pytest.raises(ValueError, match="Query value exceeds"):
        SendRequestParams.model_validate(
            {
                "id": "large_query",
                "endpoint": "https://api.example.test/items",
                "query": {"filter": "12345"},
            }
        )

    monkeypatch.setattr(api_models, "MAX_QUERY_VALUES", 1)
    with pytest.raises(ValueError, match="aggregate value limit"):
        SendRequestParams.model_validate(
            {
                "id": "many_query_values",
                "endpoint": "https://api.example.test/items",
                "query": {"tag": ["one", "two"]},
            }
        )
    with pytest.raises(ValueError, match="must be finite"):
        SendRequestParams.model_validate(
            {
                "id": "nonfinite_query",
                "endpoint": "https://api.example.test/items",
                "query": {"ratio": float("nan")},
            }
        )


def test_schema_headers_require_direct_schema_url() -> None:
    with pytest.raises(ValueError, match="schemaHeaders can be used only with schemaUrl"):
        CallSchemaParams.model_validate(
            {
                "id": "request",
                "schemaId": SCHEMA_ID,
                "schemaHeaders": {"Authorization": "Bearer credential"},
                "operationId": "replacePet",
            }
        )


def test_call_schema_validates_invokes_and_retains_request_body(tmp_path: Path) -> None:
    request_body = {"name": "Ada"}
    loaded = _swagger(tmp_path)
    api = FakeApi(_response(CREATED, request_body))
    store = FakeStore(loaded)
    context = _context(api, store)
    params = CallSchemaParams.model_validate(
        {
            "id": "replace_pet",
            "schemaId": SCHEMA_ID,
            "operationId": "replacePet",
            "pathParams": {"petId": PET_ID},
            "body": request_body,
            "expectedStatus": CREATED,
            "expectedMessage": "ada",
        }
    )

    result = asyncio.run(call_schema(cast("Any", context), params))

    assert result.request_body == request_body
    assert result.content_type == "application/json"
    assert result.response_body == request_body
    assert store.schema_ids == [SCHEMA_ID]
    assert api.calls[0]["url"] == f"https://api.example.test/v1/pets/{PET_ID}"
    assert context.operations[0]["phase"] == "schema_request"
    assert context.operations[0]["operation_type"] == "POST"
    assert context.operations[0]["target"] == (f"https://api.example.test/v1/pets/{PET_ID}")
    assert context.operations[0]["operation_actual"]["httpStatus"] == CREATED


def test_call_schema_respects_optional_method_policy(tmp_path: Path) -> None:
    api = FakeApi(_response(CREATED, {"name": "Ada"}))
    store = FakeStore(_swagger(tmp_path))
    context = _context(api, store)
    context.settings.api_allowed_methods = ("GET",)
    params = CallSchemaParams.model_validate(
        {
            "id": "blocked_replace",
            "schemaId": SCHEMA_ID,
            "operationId": "replacePet",
            "pathParams": {"petId": PET_ID},
        }
    )

    with pytest.raises(ApiActivityError, match="PLANTAIN_API_ALLOWED_METHODS"):
        asyncio.run(call_schema(cast("Any", context), params))

    assert api.calls == []


def test_schema_download_headers_are_separate_from_operation_headers(tmp_path: Path) -> None:
    loaded = _swagger(tmp_path)
    api = FakeApi(_response(CREATED, {"name": "Ada"}))
    store = FakeStore(loaded)
    context = _context(api, store)
    params = CallSchemaParams.model_validate(
        {
            "id": "replace_pet",
            "schemaUrl": "https://schema.example.test/openapi.json",
            "schemaHeaders": {"Authorization": "Bearer schema-credential"},
            "operationId": "replacePet",
            "pathParams": {"petId": PET_ID},
            "headers": {"X-Operation-Key": "operation-credential"},
            "body": {"name": "Ada"},
            "expectedStatus": CREATED,
        }
    )

    asyncio.run(call_schema(cast("Any", context), params))

    assert store.schema_urls == [
        (
            "https://schema.example.test/openapi.json",
            {"Authorization": "Bearer schema-credential"},
        )
    ]
    assert api.calls[0]["headers"] == {"X-Operation-Key": "operation-credential"}


def test_validate_schema_can_assert_expected_contract_drift(tmp_path: Path) -> None:
    loaded = _swagger(tmp_path)
    store = FakeStore(loaded)
    context = _context(FakeApi(_response(CREATED, {})), store)
    params = ValidateSchemaParams.model_validate(
        {
            "id": "contract_drift",
            "schemaId": SCHEMA_ID,
            "operationId": "replacePet",
            "statusCode": CREATED,
            "contentType": "application/json",
            "body": {},
            "expectedValid": False,
        }
    )

    result = asyncio.run(validate_schema(cast("Any", context), params))

    assert result.success is True
    assert result.expected_valid is False
    assert result.schema_valid is False
    assert params.content_type == "application/json"
    assert result.schema_path == "<root>"
    assert result.schema_rule == "required"
    assert context.operations[0]["phase"] == "schema_validation"
    assert context.operations[0]["operation_actual"]["schemaValid"] is False


def test_expected_contract_drift_fails_when_response_is_valid(tmp_path: Path) -> None:
    loaded = _swagger(tmp_path)
    store = FakeStore(loaded)
    context = _context(FakeApi(_response(CREATED, {})), store)
    params = ValidateSchemaParams.model_validate(
        {
            "id": "contract_drift",
            "schemaId": SCHEMA_ID,
            "operationId": "replacePet",
            "statusCode": CREATED,
            "body": {"name": "Ada"},
            "expectedValid": False,
        }
    )

    with pytest.raises(ApiActivityError, match="unexpectedly satisfied"):
        asyncio.run(validate_schema(cast("Any", context), params))
    assert context.operations[0]["status"] == "failed"


def test_api_data_file_cannot_escape_configured_root(tmp_path: Path) -> None:
    api_data = tmp_path / "api-data"
    api_data.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    context = SimpleNamespace(
        settings=SimpleNamespace(
            api_data_dir=api_data,
            api_max_response_bytes=1_024,
        )
    )

    with pytest.raises(ApiActivityError, match="must stay inside"):
        _request_body(cast("Any", context), "../outside.json", None)


def test_api_data_file_uses_the_separate_request_body_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_data = tmp_path / "api-data"
    api_data.mkdir()
    (api_data / "request.json").write_text('{"value":"large"}', encoding="utf-8")
    context = SimpleNamespace(settings=SimpleNamespace(api_data_dir=api_data))
    monkeypatch.setattr(api_activities, "MAX_REQUEST_BODY_BYTES", 4)

    with pytest.raises(ApiActivityError, match="configured byte limit"):
        _request_body(cast("Any", context), "request.json", None)


def test_api_data_file_rejects_symlinked_path(tmp_path: Path) -> None:
    api_data = tmp_path / "api-data"
    api_data.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"private":"outside"}', encoding="utf-8")
    (api_data / "request.json").symlink_to(outside)
    context = SimpleNamespace(settings=SimpleNamespace(api_data_dir=api_data))

    with pytest.raises(ApiActivityError, match="readable UTF-8"):
        _request_body(cast("Any", context), "request.json", None)

    assert outside.read_text(encoding="utf-8") == '{"private":"outside"}'


def test_registers_only_the_four_framework_api_activities() -> None:
    registry = ActivityRegistry()

    register_api_activities(registry)

    assert registry.names() == (
        "callSchema",
        "loadApiSchema",
        "sendRequest",
        "validateSchema",
    )
