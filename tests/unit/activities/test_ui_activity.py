"""Orchestration tests for the sole YAML-facing UI activity."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from plantain.activities import ui_activity
from plantain.activities.ui_activity import capture_page_snapshot
from plantain.activities.ui_errors import (
    BrowserLifecycleError,
    SnapshotConsistencyError,
    SnapshotError,
    UiActionError,
    UiAssertionError,
)
from plantain.engine.admission import ResourceAdmission
from plantain.models.ui import CapturePageSnapshotParams, LocatorSpec, SnapshotOptions, UiAction
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

FAILURE_ACTION_POSITION = 2
POST_CAPTURE_POLICY_CHECK = 3
EXPECTED_RESPONSE_STATUS = 201
EXPECTED_RESPONSE_TIMEOUT_MS = 1_234


class FakePage:
    url = "https://example.test/inventory"

    async def title(self) -> str:
        return "Inventory"


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.telemetry = cast(
            "Any",
            SimpleNamespace(committed_cursor=0),
        )
        self.policy_checks = 0
        self.policy_failure: BrowserLifecycleError | None = None

    async def start(self) -> FakePage:
        return self.page

    async def navigate(self, _url: str, *, wait_until: str) -> FakePage:
        assert wait_until == "domcontentloaded"
        return self.page

    def activate(self, page: FakePage) -> None:
        self.page = page

    async def assert_policy_compliant(self) -> None:
        self.policy_checks += 1
        if self.policy_failure is not None:
            raise self.policy_failure


class _ResponseWaiter:
    def __init__(self, events: list[str], response: Any) -> None:
        self.events = events
        self.response = response
        self.value: asyncio.Future[Any]

    async def __aenter__(self) -> _ResponseWaiter:
        self.events.append("listener:armed")
        self.value = asyncio.get_running_loop().create_future()
        self.value.set_result(self.response)
        return self

    async def __aexit__(self, *_: object) -> None:
        self.events.append("listener:removed")


class _ResponseBrowser(FakeBrowser):
    def __init__(self, page: FakePage, events: list[str]) -> None:
        super().__init__(page)
        self.events = events
        self.options: dict[str, Any] = {}

    def expect_response(self, **options: Any) -> _ResponseWaiter:
        self.options = options
        response = SimpleNamespace(
            url="https://example.test/api/orders",
            request=SimpleNamespace(method="POST"),
            status=EXPECTED_RESPONSE_STATUS,
            body="must-not-appear",
        )
        return _ResponseWaiter(self.events, response)


class FakeStagedSnapshot:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.cleanup_calls = 0

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.events.append("cleanup")


class _CancellationRegistry:
    def __init__(self, staged: FakeStagedSnapshot) -> None:
        self.staged = staged
        self.started = Event()
        self.release = Event()
        self.finished = Event()
        self.cancelled = False

    def register_batch(
        self,
        pending: list[Any],
        *,
        cancellation: Any,
    ) -> list[Any]:
        assert pending[0].staged is self.staged
        self.started.set()
        if not self.release.wait(timeout=1):
            raise AssertionError("registration test release timed out")
        try:
            cancellation.raise_if_cancelled()
        except SnapshotError:
            self.cancelled = True
            raise
        else:
            return []
        finally:
            self.staged.cleanup()
            self.finished.set()


class _PromotingRegistry:
    def __init__(
        self,
        staged: FakeStagedSnapshot,
        events: list[str],
        *,
        fail_promotion: bool = False,
    ) -> None:
        self.staged = staged
        self.events = events
        self.fail_promotion = fail_promotion

    def register_batch(self, pending: list[Any], *, cancellation: Any) -> list[Any]:
        cancellation.raise_if_cancelled()
        self.events.append("register")
        assert len(pending) == 1
        assert pending[0].evidence_state == "diagnostic"
        assert pending[0].failure_stage == "verification"
        pending[0].staged.cleanup()
        return [
            SimpleNamespace(
                activity="inventory",
                canonical_file="inventory.semantic.json",
                status="created",
                evidence_state="diagnostic",
                failure_stage="verification",
            )
        ]

    def promote_diagnostic(
        self,
        filename: str,
        activity: str,
        *,
        cancellation: Any,
    ) -> Any:
        cancellation.raise_if_cancelled()
        self.events.append("promote")
        assert (filename, activity) == ("inventory.semantic.json", "inventory")
        if self.fail_promotion:
            raise RuntimeError("private promotion failure")
        return SimpleNamespace(evidence_state="verified", failure_stage=None)


class _SingleSnapshotExtractor:
    staged: FakeStagedSnapshot | None = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def capture(self, _page: Any, *, activity: str) -> FakeStagedSnapshot:
        assert activity == "inventory"
        if self.staged is None:
            raise AssertionError("snapshot test stage was not configured")
        return self.staged


class _ConsistencyRetryExtractor:
    staged: FakeStagedSnapshot | None = None
    browser: FakeBrowser | None = None
    attempts = 0
    policy_checks_by_attempt: ClassVar[list[int]] = []
    succeed_on_attempt = ui_activity.MAX_SNAPSHOT_CAPTURE_ATTEMPTS

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def capture(self, _page: Any, *, activity: str) -> FakeStagedSnapshot:
        assert activity == "inventory"
        extractor_type = type(self)
        if extractor_type.browser is None:
            raise AssertionError("snapshot test browser was not configured")
        extractor_type.policy_checks_by_attempt.append(extractor_type.browser.policy_checks)
        extractor_type.attempts += 1
        if extractor_type.attempts < extractor_type.succeed_on_attempt:
            raise SnapshotConsistencyError("DOM changed during semantic capture")
        if self.staged is None:
            raise AssertionError("snapshot test stage was not configured")
        return self.staged


class _PassingAssertions:
    events: list[str] | None = None

    def __init__(self, *_args: Any) -> None:
        pass

    async def verify(self, _page: Any, _assertion: Any) -> None:
        if self.events is None:
            raise AssertionError("passing assertion events were not configured")
        self.events.append("verify")


class _FirstActionFails:
    def __init__(self, *_args: Any) -> None:
        pass

    async def execute(self, _page: Any, _action: Any) -> Any:
        raise RuntimeError("private action failure")


class _FailingSnapshotExtractor:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def capture(self, _page: Any, *, activity: str) -> Any:
        assert activity == "inventory"
        raise SnapshotError("private diagnostic capture failure")


class _FailingRegistrationRegistry:
    def register_batch(self, pending: list[Any], *, cancellation: Any) -> list[Any]:
        cancellation.raise_if_cancelled()
        for item in pending:
            item.staged.cleanup()
        raise SnapshotError("private diagnostic registration failure")


class _ActionDiagnosticRegistry:
    def __init__(self, staged: FakeStagedSnapshot) -> None:
        self.staged = staged

    def register_batch(self, pending: list[Any], *, cancellation: Any) -> list[Any]:
        cancellation.raise_if_cancelled()
        assert len(pending) == 1
        item = pending[0]
        assert item.staged is self.staged
        assert (item.evidence_state, item.failure_stage) == ("diagnostic", "action")
        item.staged.cleanup()
        return [
            SimpleNamespace(
                activity=item.activity,
                canonical_file="action-diagnostic.semantic.json",
                status="created",
                evidence_state=item.evidence_state,
                failure_stage=item.failure_stage,
            )
        ]


class FakeContext:
    def __init__(
        self,
        *,
        tmp_path: Path,
        browser: FakeBrowser,
        registry: Any,
    ) -> None:
        self.settings = SimpleNamespace(
            action_timeout_ms=15_000,
            snapshots_dir=tmp_path / "snapshots",
        )
        self.secrets = SecretRegistry(sensitive_keys=())
        self.scenario = SimpleNamespace(scenario="UI orchestration test")
        self.correlation_id = "ui-run-1"
        self.current_activity = "capturePageSnapshot"
        self.current_step_id = "ui_step"
        self.artifacts: list[dict[str, str]] = []
        self.operations: list[dict[str, Any]] = []
        self.services = _FakeServices(browser, registry)

    def add_artifact(self, *, kind: str, path: str, description: str) -> None:
        self.artifacts.append({"kind": kind, "path": path, "description": description})

    def add_operation(self, **operation: Any) -> None:
        self.operations.append(
            {key: value for key, value in operation.items() if value is not None}
        )


class _FakeServices:
    def __init__(self, browser: FakeBrowser, registry: Any) -> None:
        self._browser = browser
        self._registry = registry
        self.admission = ResourceAdmission(SimpleNamespace())

    async def browser(self) -> FakeBrowser:
        return self._browser

    async def snapshot_registry(self) -> Any:
        if isinstance(self._registry, BaseException):
            raise self._registry
        return self._registry


def _params(
    *,
    actions: list[Any] | None = None,
    verify: list[Any] | None = None,
    capture_after_each_action: bool = False,
    snapshot_enabled: bool = True,
) -> CapturePageSnapshotParams:
    return CapturePageSnapshotParams.model_construct(
        url=None,
        activity="inventory",
        actions=actions or [],
        verify=verify or [],
        snapshot=SnapshotOptions(
            enabled=snapshot_enabled,
            capture_after_each_action=capture_after_each_action,
        ),
        timeout_ms=None,
        wait_until="domcontentloaded",
    )


def _visible_assertion() -> Any:
    return SimpleNamespace(
        assertion=SimpleNamespace(value="visible"),
        target=LocatorSpec(css="#inventory"),
    )


class _ResponseActions:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def execute(self, page: Any, _action: Any) -> Any:
        self.events.append("action")
        if self.fail:
            raise RuntimeError("synthetic action failure")
        return SimpleNamespace(page=page, popup_opened=False)


def _response_action() -> UiAction:
    return UiAction.model_validate(
        {
            "action": "click",
            "target": {"css": "#submit"},
            "expectResponse": {
                "url": "**/api/orders",
                "method": "POST",
                "status": [200, EXPECTED_RESPONSE_STATUS],
                "timeoutMs": EXPECTED_RESPONSE_TIMEOUT_MS,
            },
        }
    )


def test_action_response_expectation_records_causal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    browser = _ResponseBrowser(FakePage(), events)
    actions = _ResponseActions(events)
    monkeypatch.setattr(ui_activity, "UiActionExecutor", lambda *_: actions)
    context = FakeContext(tmp_path=tmp_path, browser=browser, registry=None)

    asyncio.run(
        capture_page_snapshot(
            cast("Any", context),
            _params(actions=[_response_action()], snapshot_enabled=False),
        )
    )

    assert events == ["listener:armed", "action", "listener:removed"]
    assert browser.options["timeout_ms"] == EXPECTED_RESPONSE_TIMEOUT_MS
    operation = context.operations[-1]
    assert "**/api/orders" in operation["operation_expected"]
    assert "https://example.test/api/orders" in operation["operation_actual"]
    assert "must-not-appear" not in operation["operation_actual"]


def test_action_response_waiter_is_removed_when_action_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    browser = _ResponseBrowser(FakePage(), events)
    actions = _ResponseActions(events, fail=True)
    monkeypatch.setattr(ui_activity, "UiActionExecutor", lambda *_: actions)
    context = FakeContext(tmp_path=tmp_path, browser=browser, registry=None)

    with pytest.raises(UiActionError, match="UI action 1/1 failed"):
        asyncio.run(
            capture_page_snapshot(
                cast("Any", context),
                _params(actions=[_response_action()], snapshot_enabled=False),
            )
        )

    assert events == ["listener:armed", "action", "listener:removed"]
    assert "operation_actual" not in context.operations[-1]


def test_failing_assertion_retains_diagnostic_without_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staged = FakeStagedSnapshot(events)

    class Extractor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def capture(self, _page: Any, *, activity: str) -> Any:
            events.append(f"capture:{activity}")
            return staged

        def commit_telemetry(self) -> None:
            events.append("telemetry-commit")

    class Assertions:
        def __init__(self, *_args: Any) -> None:
            pass

        async def verify(self, _page: Any, _assertion: Any) -> None:
            events.append("verify")
            raise RuntimeError("expected assertion failure")

    class Registry:
        def register_batch(
            self,
            pending: list[Any],
            *,
            cancellation: Any,
        ) -> list[Any]:
            cancellation.raise_if_cancelled()
            events.append("register")
            assert pending[0].staged is staged
            assert pending[0].evidence_state == "diagnostic"
            assert pending[0].failure_stage == "verification"
            return [
                SimpleNamespace(
                    activity="inventory",
                    canonical_file="inventory.semantic.json",
                    status="created",
                    evidence_state="diagnostic",
                    failure_stage="verification",
                )
            ]

        def promote_diagnostic(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("failed verification must not promote diagnostic evidence")

    monkeypatch.setattr(ui_activity, "CompleteSemanticSnapshotExtractor", Extractor)
    monkeypatch.setattr(ui_activity, "UiAssertionEngine", Assertions)
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=Registry(),
    )

    assertion = SimpleNamespace(
        assertion=SimpleNamespace(value="visible"),
        target=LocatorSpec(css="#inventory"),
    )
    with pytest.raises(
        UiAssertionError,
        match="UI verification 1/1 failed",
    ) as failure:
        asyncio.run(
            capture_page_snapshot(
                cast("Any", context),
                _params(verify=[assertion]),
            )
        )

    assert events == [
        "capture:inventory",
        "register",
        "telemetry-commit",
        "verify",
    ]
    assert staged.cleanup_calls == 0
    assert context.artifacts[0]["path"] == "snapshots/inventory.semantic.json"
    assert context.artifacts[0]["kind"] == "semantic-snapshot-diagnostic"
    assert failure.value.safe_details["diagnostic_snapshot"] == (
        "snapshots/inventory.semantic.json"
    )
    assert failure.value.safe_details["evidence_state"] == "diagnostic"
    assert [(item["phase"], item["status"]) for item in context.operations] == [
        ("navigation", "passed"),
        ("snapshot_capture", "passed"),
        ("snapshot_registration", "passed"),
        ("verification", "failed"),
    ]


def test_passing_verification_promotes_diagnostic_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staged = FakeStagedSnapshot(events)
    _SingleSnapshotExtractor.staged = staged
    _PassingAssertions.events = events
    monkeypatch.setattr(ui_activity, "CompleteSemanticSnapshotExtractor", _SingleSnapshotExtractor)
    monkeypatch.setattr(ui_activity, "UiAssertionEngine", _PassingAssertions)
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=_PromotingRegistry(staged, events),
    )
    target = LocatorSpec(css="#inventory")
    assertion = SimpleNamespace(
        assertion=SimpleNamespace(value="visible"),
        target=target,
    )
    result = asyncio.run(capture_page_snapshot(cast("Any", context), _params(verify=[assertion])))

    assert events == ["register", "cleanup", "verify", "promote"]
    assert result.snapshots[0].evidence_state == "verified"
    assert result.snapshots[0].failure_stage is None
    assert context.artifacts[0]["kind"] == "semantic-snapshot"
    assert context.operations[-1]["phase"] == "snapshot_promotion"
    assert context.operations[-1]["status"] == "passed"


def test_capture_retries_transient_consistency_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staged = FakeStagedSnapshot(events)
    browser = FakeBrowser(FakePage())
    _ConsistencyRetryExtractor.staged = staged
    _ConsistencyRetryExtractor.browser = browser
    _ConsistencyRetryExtractor.attempts = 0
    _ConsistencyRetryExtractor.policy_checks_by_attempt = []
    _ConsistencyRetryExtractor.succeed_on_attempt = ui_activity.MAX_SNAPSHOT_CAPTURE_ATTEMPTS
    _PassingAssertions.events = events
    monkeypatch.setattr(
        ui_activity, "CompleteSemanticSnapshotExtractor", _ConsistencyRetryExtractor
    )
    monkeypatch.setattr(ui_activity, "UiAssertionEngine", _PassingAssertions)
    context = FakeContext(
        tmp_path=tmp_path, browser=browser, registry=_PromotingRegistry(staged, events)
    )
    result = asyncio.run(
        capture_page_snapshot(cast("Any", context), _params(verify=[_visible_assertion()]))
    )
    attempts = ui_activity.MAX_SNAPSHOT_CAPTURE_ATTEMPTS
    assert _ConsistencyRetryExtractor.attempts == attempts
    observed = _ConsistencyRetryExtractor.policy_checks_by_attempt
    assert observed == list(range(observed[0], observed[0] + attempts))
    assert result.snapshots[0].evidence_state == "verified"


def test_capture_consistency_retry_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage())
    _ConsistencyRetryExtractor.staged = None
    _ConsistencyRetryExtractor.browser = browser
    _ConsistencyRetryExtractor.attempts = 0
    _ConsistencyRetryExtractor.policy_checks_by_attempt = []
    _ConsistencyRetryExtractor.succeed_on_attempt = ui_activity.MAX_SNAPSHOT_CAPTURE_ATTEMPTS + 1
    monkeypatch.setattr(
        ui_activity, "CompleteSemanticSnapshotExtractor", _ConsistencyRetryExtractor
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=browser,
        registry=AssertionError("registry must not be reached"),
    )
    with pytest.raises(SnapshotError, match="UI snapshot 1/1 capture failed"):
        asyncio.run(capture_page_snapshot(cast("Any", context), _params()))

    attempts = ui_activity.MAX_SNAPSHOT_CAPTURE_ATTEMPTS
    assert _ConsistencyRetryExtractor.attempts == attempts
    observed = _ConsistencyRetryExtractor.policy_checks_by_attempt
    assert observed == list(range(observed[0], observed[0] + attempts))
    assert context.artifacts == []
    assert context.operations[-1]["status"] == "failed"


def test_promotion_failure_preserves_diagnostic_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staged = FakeStagedSnapshot(events)
    _SingleSnapshotExtractor.staged = staged
    _PassingAssertions.events = events
    monkeypatch.setattr(ui_activity, "CompleteSemanticSnapshotExtractor", _SingleSnapshotExtractor)
    monkeypatch.setattr(ui_activity, "UiAssertionEngine", _PassingAssertions)
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=_PromotingRegistry(staged, events, fail_promotion=True),
    )

    with pytest.raises(SnapshotError, match="UI snapshot promotion failed") as failure:
        asyncio.run(
            capture_page_snapshot(
                cast("Any", context),
                _params(verify=[_visible_assertion()]),
            )
        )

    assert events == ["register", "cleanup", "verify", "promote"]
    assert context.artifacts[0]["kind"] == "semantic-snapshot-diagnostic"
    assert failure.value.safe_details["failure_stage"] == "snapshot_promotion"
    assert failure.value.safe_details["operation_error_type"] == "RuntimeError"
    assert context.operations[-1]["phase"] == "snapshot_promotion"
    assert context.operations[-1]["status"] == "failed"


def test_registration_cancellation_waits_for_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staged = FakeStagedSnapshot(events)
    registry = _CancellationRegistry(staged)
    _SingleSnapshotExtractor.staged = staged
    monkeypatch.setattr(
        ui_activity,
        "CompleteSemanticSnapshotExtractor",
        _SingleSnapshotExtractor,
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=registry,
    )

    async def exercise() -> None:
        task = asyncio.create_task(capture_page_snapshot(cast("Any", context), _params()))
        assert await asyncio.wait_for(asyncio.to_thread(registry.started.wait), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        registry.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert registry.cancelled is True
    assert registry.finished.is_set()
    assert staged.cleanup_calls == 1
    assert context.artifacts == []


def test_action_failure_registers_complete_diagnostic_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staged = [FakeStagedSnapshot(events), FakeStagedSnapshot(events)]

    class Extractor:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls = 0

        async def capture(self, _page: Any, *, activity: str) -> Any:
            events.append(f"capture:{activity}")
            captured = staged[self.calls]
            self.calls += 1
            return captured

    class Actions:
        def __init__(self, *_args: Any) -> None:
            self.calls = 0

        async def execute(self, page: Any, _action: Any) -> Any:
            self.calls += 1
            events.append(f"action:{self.calls}")
            if self.calls == FAILURE_ACTION_POSITION:
                raise RuntimeError("expected action failure")
            return SimpleNamespace(page=page, popup_opened=False)

    class Registry:
        def register_batch(
            self,
            pending: list[Any],
            *,
            cancellation: Any,
        ) -> list[Any]:
            cancellation.raise_if_cancelled()
            events.append("register")
            assert [item.evidence_state for item in pending] == [
                "verified",
                "diagnostic",
            ]
            assert [item.failure_stage for item in pending] == [None, "action"]
            for item in pending:
                item.staged.cleanup()
            return [
                SimpleNamespace(
                    activity=item.activity,
                    canonical_file=f"evidence_{index}.semantic.json",
                    status="created",
                    evidence_state=item.evidence_state,
                    failure_stage=item.failure_stage,
                )
                for index, item in enumerate(pending, start=1)
            ]

        def promote_diagnostic(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("action diagnostics must not be promoted")

    monkeypatch.setattr(ui_activity, "CompleteSemanticSnapshotExtractor", Extractor)
    monkeypatch.setattr(ui_activity, "UiActionExecutor", Actions)
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=Registry(),
    )

    action = SimpleNamespace(
        action=SimpleNamespace(value="click"),
        target=LocatorSpec(css="#inventory-item"),
    )
    with pytest.raises(UiActionError, match="UI action 2/2 failed") as failure:
        asyncio.run(
            capture_page_snapshot(
                cast("Any", context),
                _params(
                    actions=[action, action],
                    capture_after_each_action=True,
                ),
            )
        )

    assert events == [
        "action:1",
        "capture:inventory__after_1",
        "action:2",
        "capture:inventory",
        "register",
        "cleanup",
        "cleanup",
    ]
    assert [item.cleanup_calls for item in staged] == [1, 1]
    assert [item["kind"] for item in context.artifacts] == [
        "semantic-snapshot",
        "semantic-snapshot-diagnostic",
    ]
    assert context.artifacts[1]["path"] == "snapshots/evidence_2.semantic.json"
    assert failure.value.safe_details == {
        "failure_stage": "ui_action",
        "operation_index": 2,
        "operation_total": 2,
        "operation_type": "click",
        "operation_target": "css:#inventory-item",
        "operation_error_type": "RuntimeError",
        "diagnostic_snapshot": "snapshots/evidence_2.semantic.json",
        "evidence_state": "diagnostic",
    }
    failed_action = next(
        item
        for item in context.operations
        if item["phase"] == "action" and item["status"] == "failed"
    )
    assert failed_action["operation_type"] == "click"
    assert failed_action["target"] == "css:#inventory-item"
    assert context.operations[-1]["operation_type"] == "register"
    assert context.operations[-1]["status"] == "passed"


def test_diagnostic_capture_failure_preserves_primary_action_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui_activity, "UiActionExecutor", _FirstActionFails)
    monkeypatch.setattr(
        ui_activity,
        "CompleteSemanticSnapshotExtractor",
        _FailingSnapshotExtractor,
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=AssertionError("registry must not be reached"),
    )
    action = SimpleNamespace(
        action=SimpleNamespace(value="click"),
        target=LocatorSpec(css="#inventory-item"),
    )

    with pytest.raises(UiActionError, match="UI action 1/1 failed") as failure:
        asyncio.run(capture_page_snapshot(cast("Any", context), _params(actions=[action])))

    assert failure.value.safe_details["failure_stage"] == "ui_action"
    assert failure.value.safe_details["diagnostic_status"] == "capture_failed"
    assert "private" not in str(failure.value)
    assert context.artifacts == []
    assert context.operations[-1]["phase"] == "snapshot_capture"
    assert context.operations[-1]["status"] == "failed"


def test_diagnostic_registration_failure_preserves_primary_action_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = FakeStagedSnapshot([])
    _SingleSnapshotExtractor.staged = staged
    monkeypatch.setattr(ui_activity, "UiActionExecutor", _FirstActionFails)
    monkeypatch.setattr(
        ui_activity,
        "CompleteSemanticSnapshotExtractor",
        _SingleSnapshotExtractor,
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=_FailingRegistrationRegistry(),
    )
    action = SimpleNamespace(
        action=SimpleNamespace(value="click"),
        target=LocatorSpec(css="#inventory-item"),
    )

    with pytest.raises(UiActionError, match="UI action 1/1 failed") as failure:
        asyncio.run(capture_page_snapshot(cast("Any", context), _params(actions=[action])))

    assert staged.cleanup_calls == 1
    assert failure.value.safe_details["diagnostic_status"] == "registration_failed"
    assert "private" not in str(failure.value)
    assert context.artifacts == []
    assert context.operations[-1]["phase"] == "snapshot_registration"
    assert context.operations[-1]["status"] == "failed"


def test_registry_acquisition_failure_cleans_action_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = FakeStagedSnapshot([])
    _SingleSnapshotExtractor.staged = staged
    monkeypatch.setattr(ui_activity, "UiActionExecutor", _FirstActionFails)
    monkeypatch.setattr(
        ui_activity,
        "CompleteSemanticSnapshotExtractor",
        _SingleSnapshotExtractor,
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=RuntimeError("private registry failure"),
    )
    action = SimpleNamespace(
        action=SimpleNamespace(value="click"),
        target=LocatorSpec(css="#inventory-item"),
    )

    with pytest.raises(UiActionError, match="UI action 1/1 failed") as failure:
        asyncio.run(capture_page_snapshot(cast("Any", context), _params(actions=[action])))

    assert staged.cleanup_calls == 1
    assert failure.value.safe_details["diagnostic_status"] == "registration_unavailable"
    assert "private" not in str(failure.value)
    assert context.artifacts == []


def test_browser_policy_violation_fails_the_current_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage())
    staged = FakeStagedSnapshot([])
    _SingleSnapshotExtractor.staged = staged

    class Actions:
        def __init__(self, *_args: Any) -> None:
            pass

        async def execute(self, page: Any, _action: Any) -> Any:
            browser.policy_failure = BrowserLifecycleError(
                "Browser application traffic was blocked by outbound network policy"
            )
            return SimpleNamespace(page=page, popup_opened=False)

    monkeypatch.setattr(ui_activity, "UiActionExecutor", Actions)
    monkeypatch.setattr(
        ui_activity,
        "CompleteSemanticSnapshotExtractor",
        _SingleSnapshotExtractor,
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=browser,
        registry=_ActionDiagnosticRegistry(staged),
    )
    action = SimpleNamespace(
        action=SimpleNamespace(value="click"),
        target=LocatorSpec(css="#submit"),
    )

    with pytest.raises(UiActionError, match="UI action 1/1 failed") as failure:
        asyncio.run(
            capture_page_snapshot(
                cast("Any", context),
                _params(actions=[action]),
            )
        )

    assert staged.cleanup_calls == 1
    assert context.artifacts[0]["kind"] == "semantic-snapshot-diagnostic"
    assert failure.value.safe_details["evidence_state"] == "diagnostic"
    failed_action = next(item for item in context.operations if item["phase"] == "action")
    assert failed_action["status"] == "failed"


def test_post_capture_policy_failure_cleans_private_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = FakeStagedSnapshot([])
    _SingleSnapshotExtractor.staged = staged

    class Browser(FakeBrowser):
        def __init__(self, page: FakePage) -> None:
            super().__init__(page)
            self.checks = 0

        async def assert_policy_compliant(self) -> None:
            self.checks += 1
            if self.checks == POST_CAPTURE_POLICY_CHECK:
                raise BrowserLifecycleError("blocked application request")

    browser = Browser(FakePage())
    monkeypatch.setattr(
        ui_activity,
        "CompleteSemanticSnapshotExtractor",
        _SingleSnapshotExtractor,
    )
    context = FakeContext(
        tmp_path=tmp_path,
        browser=browser,
        registry=AssertionError("registry must not be reached"),
    )

    with pytest.raises(SnapshotError, match="UI snapshot 1/1 capture failed"):
        asyncio.run(capture_page_snapshot(cast("Any", context), _params()))

    assert browser.checks == POST_CAPTURE_POLICY_CHECK
    assert staged.cleanup_calls == 1
    assert context.artifacts == []
    assert context.operations[-1]["phase"] == "snapshot_capture"
    assert context.operations[-1]["status"] == "failed"


def test_ui_lifecycle_logs_targets_with_sanitized_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[tuple[str, dict[str, object]]] = []
    observed_value = "synthetic-sensitive-value"

    class RecordingLogger:
        def info(
            self,
            message: str,
            *args: object,
            extra: dict[str, object],
        ) -> None:
            records.append((message % args, extra))

        def error(
            self,
            message: str,
            *args: object,
            extra: dict[str, object],
        ) -> None:
            records.append((message % args, extra))

    class Actions:
        def __init__(self, *_args: Any) -> None:
            pass

        async def execute(self, page: Any, _action: Any) -> Any:
            return SimpleNamespace(page=page, popup_opened=False)

    class Assertions:
        def __init__(self, *_args: Any) -> None:
            pass

        async def verify(self, _page: Any, _assertion: Any) -> None:
            pass

    monkeypatch.setattr(ui_activity, "logger", RecordingLogger())
    monkeypatch.setattr(ui_activity, "UiActionExecutor", Actions)
    monkeypatch.setattr(ui_activity, "UiAssertionEngine", Assertions)
    context = FakeContext(
        tmp_path=tmp_path,
        browser=FakeBrowser(FakePage()),
        registry=AssertionError("registry must not be reached"),
    )
    username_action = SimpleNamespace(
        action=SimpleNamespace(value="fill"),
        value="standard_user",
        target=LocatorSpec(css="#username"),
    )
    password_action = SimpleNamespace(
        action=SimpleNamespace(value="fill"),
        value=observed_value,
        target=LocatorSpec(css="#password"),
    )
    assertion = SimpleNamespace(
        assertion=SimpleNamespace(value="text"),
        contains="Thank you for your order!",
        target=LocatorSpec(css="#result"),
    )

    asyncio.run(
        capture_page_snapshot(
            cast("Any", context),
            _params(
                actions=[username_action, password_action],
                verify=[assertion],
                snapshot_enabled=False,
            ),
        )
    )

    rendered = json.dumps(records, ensure_ascii=False)
    messages = [message for message, _extra in records]
    assert any("UI action 1/2 started" in message for message in messages)
    assert any("UI action 2/2 passed" in message for message in messages)
    assert any("UI verification 1/1 started" in message for message in messages)
    assert any("UI verification 1/1 passed" in message for message in messages)
    assert observed_value not in rendered
    assert "standard_user" in rendered
    assert "Thank you for your order!" in rendered
    assert REDACTED in rendered
    assert "css:#username" in rendered
    assert "css:#password" in rendered
    assert "css:#result" in rendered
    action_operations = [item for item in context.operations if item["phase"] == "action"]
    assert [item["operation_input"] for item in action_operations] == [
        "standard_user",
        REDACTED,
    ]
    assert context.operations[-1]["operation_expected"] == (
        "{'contains': 'Thank you for your order!'}"
    )
