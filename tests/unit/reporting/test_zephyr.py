"""Security and contract tests for Zephyr Scale Server/DC reporting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from plantain.config import Settings
from plantain.errors import ConfigurationError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.reporting.outbox import PublicationOutbox, PublicationOutboxError
from plantain.reporting.zephyr import TOKEN_ENVIRONMENT_NAME, ZephyrReporter
from plantain.security import url_policy
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

SYNTHETIC_TOKEN = "synthetic-unit-test-pat"  # noqa: S105 - non-secret fixture.
HTTP_CREATED = 201
HTTP_REDIRECT = 302
PUBLISH_AND_ATTACHMENT_REQUESTS = 2
EXPECTED_RETRY_REQUESTS = 2
OUTBOX_JOB_ID = "a" * 32
OLDER_OUTBOX_JOB_ID = "b" * 32
NEWER_OUTBOX_JOB_ID = "c" * 32
OUTBOX_STARTED_AT_MS = 1_234


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "egress_control_enforced": True,
        "zephyr_base_url": "https://jira.example.test/context",
        "zephyr_publish_results": True,
        "zephyr_attach_report": False,
        "zephyr_attachment_data_governance_approved": False,
        "zephyr_allow_private_networks": True,
        "zephyr_allow_insecure_http": False,
        "zephyr_timeout_seconds": 1.0,
        "zephyr_max_response_bytes": 1_024,
        "zephyr_max_attachment_bytes": 4_096,
        "zephyr_max_connections": 2,
        "zephyr_outbox_enabled": False,
    }
    values.update(overrides)
    return cast("Settings", SimpleNamespace(**values))


@pytest.fixture(autouse=True)
def _resolve_reporting_host(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve_isolated(
        _admission: Any,
        _host: str,
        _port: int,
    ) -> set[str]:
        return {"127.0.0.1"}

    monkeypatch.setattr(url_policy, "_resolve_isolated", resolve_isolated)


def _scenario(
    *,
    jira_ticket: str | None = "QA-42",
    test_case_key: str | None = "QA-T42",
    test_run_key: str | int | None = "QA-R7",
) -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario="Zephyr unit scenario",
        jira_ticket=jira_ticket,
        test_case_key=test_case_key,
        test_run_key=test_run_key,
        steps=[StepDefinition(activity="probe", params={"id": "probe"})],
    )


def _publish(
    reporter: ZephyrReporter,
    scenario: ScenarioDefinition,
    *,
    status: str = "passed",
    report: dict[str, Any] | None = None,
) -> tuple[Any, SecretRegistry]:
    secrets = SecretRegistry()

    async def invoke() -> Any:
        try:
            return await reporter.publish(
                scenario,
                status=status,
                duration_ms=321,
                report=report or {"scenario": scenario.scenario},
                secrets=secrets,
            )
        finally:
            await reporter.close()

    return asyncio.run(invoke()), secrets


def _outbox_report() -> dict[str, Any]:
    return {
        "correlation_id": OUTBOX_JOB_ID,
        "started_at_ms": OUTBOX_STARTED_AT_MS,
        "scenario": "safe synthetic report",
    }


def _enqueue_queued_result(
    outbox: PublicationOutbox,
    *,
    job_id: str,
    created_at_ms: int,
    status: str = "Pass",
) -> None:
    outbox.enqueue(
        job_id=job_id,
        base_url="https://jira.example.test/context",
        test_case_key="QA-T42",
        test_run_key="QA-R7",
        payload={
            "testCaseKey": "QA-T42",
            "status": status,
            "comment": "queued synthetic report",
            "executionTime": 1,
        },
        created_at_ms=created_at_ms,
    )


def test_disabled_reporting_requires_no_metadata_configuration_or_token() -> None:
    reporter = ZephyrReporter(_settings(zephyr_publish_results=False, zephyr_base_url=""))
    scenario = _scenario(jira_ticket=None, test_case_key=None, test_run_key=None)

    reporter.validate_scenario(scenario)
    outcome, _secrets = _publish(reporter, scenario)

    assert outcome.status == "disabled"
    assert outcome.attachment_status == "disabled"


def test_explicit_environment_supplies_and_observes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_ENVIRONMENT_NAME, raising=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTP_CREATED, json={"id": "QA-E77"})

    reporter = ZephyrReporter(
        _settings(),
        transport=httpx.MockTransport(handler),
        environ={TOKEN_ENVIRONMENT_NAME: SYNTHETIC_TOKEN},
    )
    reporter.validate_scenario(_scenario())
    outcome, secrets = _publish(reporter, _scenario())

    assert outcome.status == "published"
    assert secrets.redact_text(SYNTHETIC_TOKEN) == REDACTED


def test_durable_outbox_prevents_duplicate_result_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(HTTP_CREATED, json={"id": "QA-E77"})

    settings = _settings(zephyr_outbox_enabled=True, output_dir=tmp_path)
    first, _first_secrets = _publish(
        ZephyrReporter(settings, transport=httpx.MockTransport(handler)),
        _scenario(),
        report=_outbox_report(),
    )
    second, _second_secrets = _publish(
        ZephyrReporter(settings, transport=httpx.MockTransport(handler)),
        _scenario(),
        report=_outbox_report(),
    )

    assert len(requests) == 1
    assert first.status == second.status == "published"
    assert first.outbox_id == second.outbox_id == OUTBOX_JOB_ID
    assert second.execution_id == "QA-E77"
    assert second.attachment_status == "not_retried"


def test_attachment_failure_is_retained_without_duplicate_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(HTTP_CREATED, json={"id": "QA-E79"})
        raise httpx.ConnectError("synthetic attachment failure", request=request)

    settings = _settings(
        zephyr_attach_report=True,
        zephyr_attachment_data_governance_approved=True,
        zephyr_outbox_enabled=True,
        output_dir=tmp_path,
    )
    first_reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    first, _first_secrets = _publish(first_reporter, _scenario(), report=_outbox_report())
    second_reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    second, _second_secrets = _publish(second_reporter, _scenario(), report=_outbox_report())

    assert len(requests) == PUBLISH_AND_ATTACHMENT_REQUESTS
    assert first.status == second.status == "published"
    assert first.attachment_status == "failed"
    assert second.attachment_status == "not_retried"


def test_durable_outbox_retries_only_confirmed_not_sent_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("synthetic offline", request=request)
        return httpx.Response(HTTP_CREATED, json={"id": "QA-E78"})

    settings = _settings(zephyr_outbox_enabled=True, output_dir=tmp_path)
    first_reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    first, _first_secrets = _publish(first_reporter, _scenario(), report=_outbox_report())
    second_reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    second, _second_secrets = _publish(second_reporter, _scenario(), report=_outbox_report())

    assert calls == EXPECTED_RETRY_REQUESTS
    assert first.status == first.retry_status == "queued"
    assert first.delivery_state == "not_sent"
    assert second.status == "published"
    assert second.execution_id == "QA-E78"


def test_durable_outbox_never_retries_ambiguous_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("synthetic ambiguous response", request=request)
        return httpx.Response(HTTP_CREATED, json={"id": "duplicate"})

    settings = _settings(zephyr_outbox_enabled=True, output_dir=tmp_path)
    first_reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    first, _first_secrets = _publish(first_reporter, _scenario(), report=_outbox_report())
    second_reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    second, _second_secrets = _publish(second_reporter, _scenario(), report=_outbox_report())

    assert calls == 1
    assert first.delivery_state == second.delivery_state == "ambiguous"
    assert first.retry_status == second.retry_status == "manual_reconciliation"
    assert first.status == second.status == "failed"


def test_outbox_persistence_failure_prevents_network_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)

    def fail_enqueue(_self: PublicationOutbox, **_kwargs: Any) -> Any:
        raise PublicationOutboxError("synthetic local failure")

    monkeypatch.setattr(PublicationOutbox, "enqueue", fail_enqueue)
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(HTTP_CREATED)

    reporter = ZephyrReporter(
        _settings(zephyr_outbox_enabled=True, output_dir=tmp_path),
        transport=httpx.MockTransport(handler),
    )
    outcome, _secrets = _publish(reporter, _scenario(), report=_outbox_report())

    assert called is False
    assert outcome.failure_stage == "publication_outbox"
    assert outcome.delivery_state == "not_sent"
    assert outcome.retry_status == "manual_reconciliation"


def test_success_drains_one_oldest_queued_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    outbox = PublicationOutbox(tmp_path / "reporting-outbox" / "zephyr_scale_server")
    _enqueue_queued_result(outbox, job_id=OLDER_OUTBOX_JOB_ID, created_at_ms=1)
    _enqueue_queued_result(outbox, job_id=NEWER_OUTBOX_JOB_ID, created_at_ms=2)
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(HTTP_CREATED, json={"id": f"QA-E{len(payloads)}"})

    settings = _settings(zephyr_outbox_enabled=True, output_dir=tmp_path)
    reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    outcome, _secrets = _publish(reporter, _scenario(), report=_outbox_report())

    assert outcome.status == "published"
    assert len(payloads) == EXPECTED_RETRY_REQUESTS
    assert payloads[1]["comment"] == "queued synthetic report"
    assert outbox.get(OLDER_OUTBOX_JOB_ID).state == "delivered"
    assert outbox.get(NEWER_OUTBOX_JOB_ID).state == "queued"


def test_queued_result_with_invalid_payload_is_never_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    outbox = PublicationOutbox(tmp_path / "reporting-outbox" / "zephyr_scale_server")
    _enqueue_queued_result(
        outbox,
        job_id=OLDER_OUTBOX_JOB_ID,
        created_at_ms=1,
        status="Unsupported",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(HTTP_CREATED, json={"id": "QA-E1"})

    settings = _settings(zephyr_outbox_enabled=True, output_dir=tmp_path)
    reporter = ZephyrReporter(settings, transport=httpx.MockTransport(handler))
    _outcome, _secrets = _publish(reporter, _scenario(), report=_outbox_report())
    queued = outbox.get(OLDER_OUTBOX_JOB_ID)

    assert len(requests) == 1
    assert queued.state == "rejected"
    assert queued.failure_stage == "outbox_validation"


def test_enabled_preflight_requires_only_case_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    reporter = ZephyrReporter(_settings())

    with pytest.raises(ConfigurationError, match="testCaseKey"):
        reporter.validate_scenario(_scenario(test_case_key=None))

    reporter.validate_scenario(_scenario(jira_ticket=None, test_run_key=None))
    reporter.validate_scenario(_scenario(jira_ticket="N/A", test_run_key="N/A"))
    with pytest.raises(ConfigurationError, match="control characters"):
        reporter.validate_scenario(_scenario(jira_ticket="QA-\n42"))


def test_cycle_result_uses_safe_endpoint_payload_and_observes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "QA-E99"})

    reporter = ZephyrReporter(_settings(), transport=httpx.MockTransport(handler))
    outcome, secrets = _publish(reporter, _scenario())

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/context/rest/atm/1.0/testrun/QA-R7/testcase/QA-T42/testresult"
    assert request.headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
    payload = json.loads(request.content)
    assert payload == {
        "testCaseKey": "QA-T42",
        "status": "Pass",
        "comment": "Plantain automated result: passed",
        "executionTime": 321,
    }
    assert b"Zephyr unit scenario" not in request.content
    assert outcome.status == "published"
    assert outcome.http_status == HTTP_CREATED
    assert outcome.execution_id == "QA-E99"
    assert outcome.attachment_status == "disabled"
    assert secrets.redact_text(SYNTHETIC_TOKEN) == REDACTED


def test_standalone_result_derives_project_and_accepts_array_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=[{"id": 1234}])

    reporter = ZephyrReporter(_settings(), transport=httpx.MockTransport(handler))
    outcome, _secrets = _publish(reporter, _scenario(test_run_key="N/A"), status="failed")

    assert payloads[0]["projectKey"] == "QA"
    assert payloads[0]["status"] == "Fail"
    assert outcome.execution_id == "1234"


def test_redirect_is_not_followed_and_remote_body_is_not_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    requests: list[httpx.Request] = []
    remote_secret = "remote-response-secret"  # noqa: S105 - leak-detection fixture.

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/result"},
            json={"password": remote_secret},
        )

    reporter = ZephyrReporter(_settings(), transport=httpx.MockTransport(handler))
    outcome, _secrets = _publish(reporter, _scenario())

    assert len(requests) == 1
    assert outcome.status == "failed"
    assert outcome.http_status == HTTP_REDIRECT
    assert outcome.failure_stage == "publish_http"
    assert remote_secret not in str(outcome.as_dict())


def test_response_is_stream_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    reporter = ZephyrReporter(
        _settings(zephyr_max_response_bytes=4),
        transport=httpx.MockTransport(handler),
    )
    outcome, _secrets = _publish(reporter, _scenario())

    assert outcome.status == "failed"
    assert outcome.failure_stage == "publish_response_limit"


def test_private_target_is_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"id": "QA-E1"})

    reporter = ZephyrReporter(
        _settings(
            zephyr_base_url="https://127.0.0.1",
            zephyr_allow_private_networks=False,
        ),
        transport=httpx.MockTransport(handler),
    )
    outcome, _secrets = _publish(reporter, _scenario())

    assert called is False
    assert outcome.status == "failed"
    assert outcome.failure_stage == "publish_policy"


def test_http_and_embedded_credentials_are_rejected_during_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)

    with pytest.raises(ConfigurationError, match="HTTPS"):
        ZephyrReporter(_settings(zephyr_base_url="http://jira.example.test")).validate_scenario(
            _scenario()
        )
    with pytest.raises(ConfigurationError, match="embed credentials"):
        ZephyrReporter(
            _settings(zephyr_base_url="https://user:pass@jira.example.test")
        ).validate_scenario(_scenario())


@pytest.mark.parametrize(
    ("environment", "base_url"),
    [
        ("production", "http://127.0.0.1"),
        ("local", "http://jira.example.test"),
    ],
)
def test_insecure_reporting_is_restricted_to_local_loopback(
    environment: str,
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(HTTP_CREATED, json={"id": "QA-E1"})

    reporter = ZephyrReporter(
        _settings(
            environment=environment,
            zephyr_base_url=base_url,
            zephyr_allow_insecure_http=True,
        ),
        transport=httpx.MockTransport(handler),
    )
    outcome, _secrets = _publish(reporter, _scenario())

    assert called is False
    assert outcome.status == "failed"
    assert outcome.failure_stage == "publish_policy"


def test_explicit_local_loopback_reporting_can_use_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)

    reporter = ZephyrReporter(
        _settings(
            environment="local",
            zephyr_base_url="http://127.0.0.1",
            zephyr_allow_insecure_http=True,
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(HTTP_CREATED, json={"id": "QA-E1"})
        ),
    )
    outcome, _secrets = _publish(reporter, _scenario())

    assert outcome.status == "published"


def test_report_attachment_is_separately_opted_in_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(201, json={"id": "QA-E44"})
        return httpx.Response(201, json={"attached": True})

    reporter = ZephyrReporter(
        _settings(
            zephyr_attach_report=True,
            zephyr_attachment_data_governance_approved=True,
        ),
        transport=httpx.MockTransport(handler),
    )
    outcome, _secrets = _publish(
        reporter,
        _scenario(),
        report={"scenario": "safe", "password": REDACTED},
    )

    assert len(requests) == PUBLISH_AND_ATTACHMENT_REQUESTS
    assert requests[1].url.path.endswith("/testresult/QA-E44/attachments")
    attachment = requests[1].read()
    assert b"scenario-result.json" in attachment
    assert REDACTED.encode() in attachment
    assert SYNTHETIC_TOKEN.encode() not in attachment
    assert outcome.attachment_status == "attached"
    assert outcome.attachment_http_status == HTTP_CREATED

    requests.clear()
    bounded = ZephyrReporter(
        _settings(
            zephyr_attach_report=True,
            zephyr_attachment_data_governance_approved=True,
            zephyr_max_attachment_bytes=10,
        ),
        transport=httpx.MockTransport(handler),
    )
    bounded_outcome, _secrets = _publish(
        bounded,
        _scenario(),
        report={"content": "larger than ten bytes"},
    )
    assert len(requests) == 1
    assert bounded_outcome.status == "published"
    assert bounded_outcome.attachment_status == "skipped_too_large"


def test_report_attachment_is_blocked_without_governance_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_NAME, SYNTHETIC_TOKEN)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(HTTP_CREATED, json={"id": "QA-E45"})

    reporter = ZephyrReporter(
        _settings(zephyr_attach_report=True),
        transport=httpx.MockTransport(handler),
    )
    outcome, _secrets = _publish(reporter, _scenario())

    assert len(requests) == 1
    assert outcome.status == "published"
    assert outcome.attachment_status == "blocked_governance"
