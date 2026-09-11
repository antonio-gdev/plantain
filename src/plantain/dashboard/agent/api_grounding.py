"""Local OpenAPI inspection and bounded operation grounding for the dashboard."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from plantain.activities.api.client import ApiActivityError
from plantain.activities.api.openapi import (
    LoadedOpenApi,
    OpenApiOperation,
    resolve_local_ref,
)
from plantain.dashboard.agent.api_models import (
    MAX_API_CONTRACT_EVIDENCE_BYTES,
    MAX_API_OPERATION_MATCHES,
    MAX_API_PREVIEW_TEXT_LENGTH,
    ApiContractEvidence,
    ApiContractInspection,
    ApiOperationCandidate,
    ApiWorkflowKind,
)
from plantain.dashboard.agent.models import IntentDecision, UserIntent
from plantain.engine.context import ScenarioContext
from plantain.engine.expressions import ExpressionResolver
from plantain.engine.runtime import ExecutionServices
from plantain.errors import ExpressionResolutionError, PlantainError
from plantain.security.redaction import redact_url
from plantain.security.secrets import SecretRegistry

MAX_API_LOCAL_REFERENCES = 2_048
_BROAD_QUERIES = frozenset({"*", "all", "all operations", "every operation"})
_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "api",
        "check",
        "contract",
        "create",
        "for",
        "operation",
        "schema",
        "test",
        "the",
        "validate",
    }
)
_SCHEMA_ID = re.compile(r"^[a-f0-9]{64}$")
_SEARCH_TOKEN = re.compile(r"[a-z0-9]+")


class ApiGroundingError(PlantainError):
    """Raised when a contract cannot be grounded without guessing or disclosure."""


@dataclass(frozen=True, slots=True)
class ApiContractGrounding:
    """Exactly one authoring packet or one browser-safe operation selection."""

    evidence: ApiContractEvidence | None = None
    inspection: ApiContractInspection | None = None

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.inspection is None):
            raise ValueError("API grounding requires exactly one result")


@dataclass(frozen=True, slots=True)
class _RankedOperation:
    operation: OpenApiOperation
    operation_key: str
    score: int
    exact: bool


async def inspect_api_contract(
    services: ExecutionServices,
    secrets: SecretRegistry,
    intent: UserIntent,
    decision: IntentDecision,
    *,
    environ: Mapping[str, str] | None = None,
) -> ApiContractGrounding:
    """Inspect one explicit contract and ground a unique operation when possible."""

    if decision.api_workflow is not ApiWorkflowKind.CONTRACT:
        raise ApiGroundingError("The selected API workflow does not use a contract")
    reference = decision.api_schema_reference
    query = decision.api_operation_query
    if reference is None or query is None:
        raise ApiGroundingError("API contract source or operation scope is missing")
    _verify_provenance(intent.prompt, reference, decision)
    try:
        loaded = await _load_contract(
            services,
            secrets,
            reference,
            decision,
            environ,
        )
        ranked = _rank_operations(loaded, query)
        selected = _unique_operation(ranked)
        if selected is not None:
            return ApiContractGrounding(
                evidence=_contract_evidence(
                    loaded,
                    selected.operation,
                    selected.operation_key,
                    secrets,
                )
            )
        return ApiContractGrounding(inspection=_inspection(loaded, query, ranked, secrets))
    except (ApiActivityError, ExpressionResolutionError) as exc:
        raise ApiGroundingError("The API contract could not be inspected safely") from exc


async def ground_cached_api_operation(
    services: ExecutionServices,
    secrets: SecretRegistry,
    schema_id: str,
    operation_key: str,
) -> ApiContractEvidence:
    """Reload and ground one browser-selected operation by its opaque identity."""

    if _SCHEMA_ID.fullmatch(schema_id) is None or _SCHEMA_ID.fullmatch(operation_key) is None:
        raise ApiGroundingError("The selected API operation is invalid")
    try:
        loaded = await (await services.openapi_store()).load_id(schema_id)
    except ApiActivityError as exc:
        raise ApiGroundingError("The selected API contract is unavailable") from exc
    matches = [
        operation
        for operation in loaded.operations()
        if _operation_key(loaded.schema_id, operation) == operation_key
    ]
    if len(matches) != 1:
        raise ApiGroundingError("The selected API operation is no longer available")
    return _contract_evidence(
        loaded,
        matches[0],
        operation_key,
        secrets,
    )


def _verify_provenance(
    prompt: str,
    reference: str,
    decision: IntentDecision,
) -> None:
    if reference not in prompt:
        raise ApiGroundingError("The API schema reference was not supplied by the user")
    if any(header.value not in prompt for header in decision.api_schema_headers):
        raise ApiGroundingError("A schema credential reference was not supplied by the user")


async def _load_contract(
    services: ExecutionServices,
    secrets: SecretRegistry,
    reference: str,
    decision: IntentDecision,
    environ: Mapping[str, str] | None,
) -> LoadedOpenApi:
    resolver = ExpressionResolver(
        ScenarioContext(),
        environment_observer=secrets.observe_environment,
        environment=environ,
    )
    resolved = resolver.resolve(reference)
    if not isinstance(resolved, str) or not resolved:
        raise ApiGroundingError("The API schema reference did not resolve to text")
    headers: dict[str, str] = {}
    for header in decision.api_schema_headers:
        value = resolver.resolve(header.value)
        if not isinstance(value, str):
            raise ApiGroundingError("A schema header did not resolve to text")
        headers[header.name] = value
    secrets.observe(headers)
    store = await services.openapi_store()
    if _SCHEMA_ID.fullmatch(resolved):
        if headers:
            raise ApiGroundingError("Cached schemas cannot use download headers")
        return await store.load_id(resolved)
    parsed = urlsplit(resolved)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ApiGroundingError("The API schema reference is not an HTTP(S) URL")
    return await store.load_url(resolved, headers=headers)


def _rank_operations(
    loaded: LoadedOpenApi,
    query: str,
) -> list[_RankedOperation]:
    normalized = query.strip().casefold()
    query_tokens = set(_SEARCH_TOKEN.findall(normalized)) - _QUERY_STOPWORDS
    broad = normalized in _BROAD_QUERIES or not query_tokens
    ranked: list[_RankedOperation] = []
    for operation in loaded.operations():
        score, exact = _operation_score(operation, normalized, query_tokens, broad)
        if score <= 0:
            continue
        ranked.append(
            _RankedOperation(
                operation=operation,
                operation_key=_operation_key(loaded.schema_id, operation),
                score=score,
                exact=exact,
            )
        )
    ranked.sort(
        key=lambda item: (
            not item.exact,
            -item.score,
            item.operation.path.casefold(),
            item.operation.method.value,
            item.operation.operation_id or "",
        )
    )
    return ranked


def _operation_score(
    operation: OpenApiOperation,
    normalized_query: str,
    query_tokens: set[str],
    broad: bool,
) -> tuple[int, bool]:
    operation_id = operation.operation_id or ""
    exact_values = {
        operation_id.casefold(),
        operation.path.casefold(),
        f"{operation.method.value} {operation.path}".casefold(),
    }
    exact = bool(normalized_query) and normalized_query in exact_values
    if broad:
        return 1, exact
    summary = operation.operation.get("summary")
    tags = operation.operation.get("tags")
    text = " ".join(
        (
            operation_id,
            operation.method.value,
            operation.path,
            summary if isinstance(summary, str) else "",
            " ".join(item for item in tags if isinstance(item, str))
            if isinstance(tags, list)
            else "",
        )
    ).casefold()
    tokens = set(_SEARCH_TOKEN.findall(text))
    overlap = len(query_tokens & tokens)
    contains = normalized_query in text
    score = overlap * 100 + (500 if contains else 0) + (10_000 if exact else 0)
    return score, exact


def _unique_operation(
    ranked: list[_RankedOperation],
) -> _RankedOperation | None:
    exact = [item for item in ranked if item.exact]
    if len(exact) == 1:
        return exact[0]
    if len(ranked) == 1:
        return ranked[0]
    return None


def _inspection(
    loaded: LoadedOpenApi,
    query: str,
    ranked: list[_RankedOperation],
    secrets: SecretRegistry,
) -> ApiContractInspection:
    visible = ranked[:MAX_API_OPERATION_MATCHES]
    return ApiContractInspection(
        schema_id=loaded.schema_id,
        schema_version=loaded.schema_version,
        source_url=_safe_url(loaded.source_url, secrets),
        base_url=_safe_url(loaded.base_url, secrets),
        query=query,
        operations=[_operation_candidate(item.operation, item.operation_key) for item in visible],
        total_matches=len(ranked),
        matches_limited=len(ranked) > len(visible),
    )


def _operation_candidate(
    operation: OpenApiOperation,
    operation_key: str,
) -> ApiOperationCandidate:
    operation_id, operation_id_limited = _preview(operation.operation_id or "")
    path, path_limited = _preview(operation.path)
    raw_summary = operation.operation.get("summary")
    summary, summary_limited = _preview(raw_summary if isinstance(raw_summary, str) else "")
    return ApiOperationCandidate(
        operation_key=operation_key,
        operation_id=operation_id,
        method=operation.method,
        path=path,
        summary=summary,
        display_limited=operation_id_limited or path_limited or summary_limited,
    )


def _preview(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_API_PREVIEW_TEXT_LENGTH:
        return value, False
    return f"{value[: MAX_API_PREVIEW_TEXT_LENGTH - 1]}…", True


def _operation_key(schema_id: str, operation: OpenApiOperation) -> str:
    identity = (
        f"{schema_id}\0{operation.method.value}\0{operation.path}\0{operation.operation_id or ''}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _contract_evidence(
    loaded: LoadedOpenApi,
    operation: OpenApiOperation,
    operation_key: str,
    secrets: SecretRegistry,
) -> ApiContractEvidence:
    payload: dict[str, Any] = {
        "schemaId": loaded.schema_id,
        "schemaVersion": loaded.schema_version,
        "sourceUrl": _safe_url(loaded.source_url, secrets),
        "baseUrl": _safe_url(loaded.base_url, secrets),
        "operation": {
            "method": operation.method.value,
            "path": operation.path,
            "operationId": operation.operation_id,
            "parameters": operation.parameters,
            "contract": operation.operation,
            "effectiveSecurity": _security_requirements(
                operation.operation.get(
                    "security",
                    loaded.document.get("security"),
                )
            ),
        },
        "globalDefaults": {
            key: loaded.document[key] for key in ("consumes", "produces") if key in loaded.document
        },
        "securitySchemes": _selected_security_schemes(loaded, operation),
    }
    payload["localReferences"] = _local_reference_closure(
        loaded.document,
        payload,
    )
    safe_payload = secrets.redact(payload)
    try:
        content = json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        size = len(content.encode())
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ApiGroundingError("API operation evidence could not be encoded safely") from exc
    if size > MAX_API_CONTRACT_EVIDENCE_BYTES:
        raise ApiGroundingError("The selected API operation is too large for bounded agent context")
    if secrets.contains_observed_text(content):
        raise ApiGroundingError("API operation evidence retained a protected value")
    return ApiContractEvidence(
        schema_id=loaded.schema_id,
        operation_key=operation_key,
        content=content,
    )


def _selected_security_schemes(
    loaded: LoadedOpenApi,
    operation: OpenApiOperation,
) -> list[dict[str, Any]]:
    requirements = operation.operation.get(
        "security",
        loaded.document.get("security"),
    )
    if not isinstance(requirements, list):
        return []
    names = {
        name
        for requirement in requirements
        if isinstance(requirement, dict)
        for name in requirement
        if isinstance(name, str)
    }
    components = loaded.document.get("components")
    openapi_schemes = components.get("securitySchemes") if isinstance(components, dict) else None
    swagger_schemes = loaded.document.get("securityDefinitions")
    source = openapi_schemes if isinstance(openapi_schemes, dict) else swagger_schemes
    if not isinstance(source, dict):
        return []
    return [
        {
            "schemeName": name,
            "definition": source[name],
        }
        for name in sorted(names)
        if name in source
    ]


def _security_requirements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for requirement in value:
        if not isinstance(requirement, dict):
            continue
        schemes = [
            {
                "schemeName": name,
                "scopes": scopes if isinstance(scopes, list) else [],
            }
            for name, scopes in sorted(requirement.items())
            if isinstance(name, str)
        ]
        normalized.append({"schemes": schemes})
    return normalized


def _local_reference_closure(
    document: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    pending = deque(sorted(_local_references(value)))
    queued = set(pending)
    retained: dict[str, Any] = {}
    while pending:
        reference = pending.popleft()
        queued.discard(reference)
        if reference in retained:
            continue
        if len(retained) >= MAX_API_LOCAL_REFERENCES:
            raise ApiGroundingError(
                "The selected API operation uses too many local schema references"
            )
        target = resolve_local_ref(document, {"$ref": reference})
        retained[reference] = target
        discovered = sorted(
            item
            for item in _local_references(target)
            if item not in retained and item not in queued
        )
        pending.extend(discovered)
        queued.update(discovered)
    return retained


def _local_references(value: Any) -> set[str]:
    references: set[str] = set()
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            marker = id(current)
            if marker in visited:
                continue
            visited.add(marker)
            reference = current.get("$ref")
            if isinstance(reference, str):
                if not reference.startswith("#/"):
                    raise ApiGroundingError("External API schema references are not supported")
                references.add(reference)
            pending.extend(current.values())
        elif isinstance(current, list):
            marker = id(current)
            if marker in visited:
                continue
            visited.add(marker)
            pending.extend(current)
    return references


def _safe_url(value: str, secrets: SecretRegistry) -> str:
    redacted = secrets.redact_text(redact_url(value))
    return redacted or "Unavailable"


__all__ = [
    "ApiContractGrounding",
    "ApiGroundingError",
    "ground_cached_api_operation",
    "inspect_api_contract",
]
