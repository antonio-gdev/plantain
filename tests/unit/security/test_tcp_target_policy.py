"""Exact TCP target authorization and DNS policy tests."""

from __future__ import annotations

from threading import Event

import pytest

from plantain.engine.process_worker import ProcessWorkerTimeoutError
from plantain.errors import ConfigurationError
from plantain.security import url_policy
from plantain.security.url_policy import (
    MAX_ALLOWED_TCP_TARGETS,
    TcpTargetPolicy,
    normalize_allowed_tcp_targets,
)

DB_PORT = 5432
OTHER_PORT = 5433
EXPECTED_DNS_CALLS = 2


class RecordingAdmission:
    def __init__(self) -> None:
        self.cancelled: Event | None = None

    def run_process_sync(
        self,
        _function: object,
        args: tuple[object, ...],
        _kwargs: dict[str, object],
        *,
        limits: object,
        cancelled: Event,
    ) -> set[str]:
        del limits
        assert args == ("db.example.test", DB_PORT)
        self.cancelled = cancelled
        return {"8.8.8.8"}


def test_tcp_targets_normalize_exact_ipv4_ipv6_and_idna_values() -> None:
    assert normalize_allowed_tcp_targets(
        (
            "DB.Example.Test.:5432",
            "db.example.test:5432",
            "[2001:0db8::1]:5432",
            "BÜCHER.Example:5432",
        )
    ) == (
        "[2001:db8::1]:5432",
        "db.example.test:5432",
        "xn--bcher-kva.example:5432",
    )


@pytest.mark.parametrize(
    "target",
    (
        "",
        "db.example.test",
        "db.example.test:",
        "db.example.test:0",
        "db.example.test:65536",
        "*.example.test:5432",
        "https://db.example.test:5432",
        "2001:db8::1:5432",
        "[2001:db8::1]5432",
    ),
)
def test_tcp_targets_reject_ambiguous_or_malformed_values(target: str) -> None:
    with pytest.raises(ConfigurationError):
        normalize_allowed_tcp_targets((target,))


def test_tcp_target_policy_requires_exact_host_and_port_before_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def resolve(
        _admission: object,
        hostname: str,
        port: int,
        _cancelled: Event,
    ) -> set[str]:
        calls.append((hostname, port))
        return {"8.8.8.8"}

    monkeypatch.setattr(url_policy, "_resolve_isolated_sync", resolve)
    policy = TcpTargetPolicy(
        allowed_targets=("db.example.test:5432",),
        egress_control_enforced=True,
    )

    assert policy.validate("DB.Example.Test.", DB_PORT) == "db.example.test:5432"
    assert policy.validate("db.example.test", DB_PORT) == "db.example.test:5432"
    assert calls == [
        ("db.example.test", DB_PORT),
        ("db.example.test", DB_PORT),
    ]

    with pytest.raises(ConfigurationError, match="not allowlisted"):
        policy.validate("db.example.test", OTHER_PORT)
    assert len(calls) == EXPECTED_DNS_CALLS


def test_tcp_dns_receives_shared_admission_and_cancellation() -> None:
    admission = RecordingAdmission()
    cancelled = Event()
    policy = TcpTargetPolicy(
        allowed_targets=("db.example.test:5432",),
        egress_control_enforced=True,
    )

    assert (
        policy.validate(
            "db.example.test",
            DB_PORT,
            admission=admission,
            cancelled=cancelled,
        )
        == "db.example.test:5432"
    )
    assert admission.cancelled is cancelled


def test_tcp_dns_timeout_does_not_expose_child_payload() -> None:
    class FailingAdmission:
        def run_process_sync(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> set[str]:
            raise ProcessWorkerTimeoutError(
                "sensitive child payload",
                failure_type="Timeout",
            )

    with pytest.raises(ConfigurationError, match="safety deadline") as captured:
        url_policy._resolve_isolated_sync(
            FailingAdmission(),
            "db.example.test",
            DB_PORT,
            Event(),
        )

    assert "sensitive child payload" not in str(captured.value)


def test_tcp_target_policy_is_fail_closed_without_targets() -> None:
    with pytest.raises(ConfigurationError, match="not allowlisted"):
        TcpTargetPolicy().validate("8.8.8.8", DB_PORT)


def test_private_tcp_target_requires_separate_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_policy,
        "_resolve_isolated_sync",
        lambda _admission, _host, _port, _cancelled: {"10.0.0.8"},
    )
    target = ("db.internal.test:5432",)

    with pytest.raises(ConfigurationError, match="unsafe address"):
        TcpTargetPolicy(
            allowed_targets=target,
            egress_control_enforced=True,
        ).validate("db.internal.test", DB_PORT)

    policy = TcpTargetPolicy(
        allowed_targets=target,
        allow_private_networks=True,
        egress_control_enforced=True,
    )
    assert policy.validate("db.internal.test", DB_PORT) == target[0]


def test_public_policy_rejects_mixed_public_and_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_policy,
        "_resolve_isolated_sync",
        lambda _admission, _host, _port, _cancelled: {"8.8.8.8", "10.0.0.8"},
    )
    policy = TcpTargetPolicy(
        allowed_targets=("db.example.test:5432",),
        egress_control_enforced=True,
    )

    with pytest.raises(ConfigurationError, match="unsafe address"):
        policy.validate("db.example.test", DB_PORT)


def test_non_local_tcp_requires_deployment_egress_control() -> None:
    target = ("8.8.8.8:5432",)

    with pytest.raises(ConfigurationError, match="deployment egress"):
        TcpTargetPolicy(allowed_targets=target).validate("8.8.8.8", DB_PORT)

    policy = TcpTargetPolicy(
        allowed_targets=target,
        egress_control_enforced=True,
    )
    assert policy.validate("8.8.8.8", DB_PORT) == target[0]


def test_tcp_target_collection_is_bounded() -> None:
    targets = tuple(f"db-{index}.example.test:5432" for index in range(MAX_ALLOWED_TCP_TARGETS + 1))

    with pytest.raises(ConfigurationError, match="exceed"):
        normalize_allowed_tcp_targets(targets)
    assert len(targets) == MAX_ALLOWED_TCP_TARGETS + 1
