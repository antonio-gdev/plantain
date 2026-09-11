"""Zero-trust outbound URL policy tests."""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from plantain.errors import ConfigurationError
from plantain.security import url_policy
from plantain.security.redaction import REDACTED
from plantain.security.url_policy import (
    MAX_DNS_ADDRESSES,
    MAX_URL_LENGTH,
    normalize_allowed_hosts,
)
from plantain.security.url_policy import UrlPolicy as _UrlPolicy


class _InlineAdmission:
    async def run_process(
        self,
        function: Any,
        /,
        *args: Any,
        limits: Any,
        **kwargs: Any,
    ) -> Any:
        del limits
        return function(*args, **kwargs)


def _policy(**options: Any) -> _UrlPolicy:
    options.setdefault("admission", _InlineAdmission())
    options.setdefault("network_mode", "restricted")
    options.setdefault("egress_control_enforced", True)
    return _UrlPolicy(**options)


def test_websocket_policy_accepts_public_literal_addresses() -> None:
    policy = _policy(allowed_hosts=("1.1.1.1",))

    assert asyncio.run(policy.validate_websocket("wss://1.1.1.1/events")) == (
        "wss://1.1.1.1/events"
    )
    with pytest.raises(ConfigurationError, match="Plain HTTP/WS"):
        asyncio.run(policy.validate_websocket("ws://1.1.1.1/socket"))


@pytest.mark.parametrize(
    "url",
    [
        "wss://127.0.0.1/socket",
        "wss://10.20.30.40/events",
        "wss://[::1]/socket",
    ],
)
def test_websocket_policy_rejects_non_public_addresses(url: str) -> None:
    hostname = urlsplit(url).hostname
    assert hostname is not None
    with pytest.raises(ConfigurationError, match="unsafe address"):
        asyncio.run(_policy(allowed_hosts=(hostname,)).validate_websocket(url))


def test_http_and_websocket_schemes_remain_separate() -> None:
    policy = _policy(
        allow_private_networks=True,
        allowed_hosts=("example.test",),
    )

    with pytest.raises(ConfigurationError, match="Only http and https"):
        asyncio.run(policy.validate("wss://example.test/socket"))
    with pytest.raises(ConfigurationError, match="Only ws and wss"):
        asyncio.run(policy.validate_websocket("https://example.test/socket"))


def test_websocket_policy_requires_explicit_subdomain_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(url_policy, "_resolve", lambda _host, _port: {"8.8.8.8"})
    policy = _policy(
        allowed_hosts=("*.example.test",),
    )

    assert asyncio.run(policy.validate_websocket("wss://events.example.test/socket")) == (
        "wss://events.example.test/socket"
    )
    with pytest.raises(ConfigurationError, match="not allowlisted"):
        asyncio.run(policy.validate_websocket("wss://example.test/socket"))
    with pytest.raises(ConfigurationError, match="not allowlisted"):
        asyncio.run(policy.validate_websocket("wss://untrusted.test/socket"))


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("x" * (MAX_URL_LENGTH + 1), "safety limit"),
        ("ftp://example.test/file", "Only http and https"),
        ("https://", "must include a hostname"),
        ("https://user:pass@example.test", "must not be embedded"),
        ("https://example.test:not-a-port", "invalid port"),
        ("https://[broken", "malformed"),
        (" https://example.test", "malformed"),
        ("https://example.test/\nnext", "malformed"),
    ],
)
def test_http_policy_rejects_malformed_or_unsafe_url_shapes(
    target: str,
    message: str,
) -> None:
    policy = _policy(
        allow_private_networks=True,
        allowed_hosts=("example.test",),
    )
    with pytest.raises(ConfigurationError, match=message):
        asyncio.run(policy.validate(target))


def test_http_policy_distinguishes_exact_and_explicit_subdomain_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(url_policy, "_resolve", lambda _host, _port: {"8.8.8.8"})
    policy = _policy(
        allowed_hosts=("example.test", "*.services.example.test"),
    )

    assert asyncio.run(policy.validate("https://example.test/path")) == (
        "https://example.test/path"
    )
    assert asyncio.run(policy.validate("https://Example.Test./path")) == (
        "https://Example.Test./path"
    )
    assert asyncio.run(policy.validate("https://api.services.example.test/path")) == (
        "https://api.services.example.test/path"
    )
    with pytest.raises(ConfigurationError, match="not allowlisted"):
        asyncio.run(policy.validate("https://api.example.test/path"))
    with pytest.raises(ConfigurationError, match="not allowlisted"):
        asyncio.run(policy.validate("https://services.example.test/path"))
    with pytest.raises(ConfigurationError, match="not allowlisted"):
        asyncio.run(policy.validate("https://example.test.evil.test/path"))


def test_http_policy_resolves_public_hosts_once_within_cache_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def resolve(hostname: str, port: int) -> set[str]:
        calls.append((hostname, port))
        return {"8.8.8.8"}

    monkeypatch.setattr(url_policy, "_resolve", resolve)
    policy = _policy(allowed_hosts=("*.example.test",))
    target = "https://api.example.test:8443/resource"

    assert asyncio.run(policy.validate(target)) == target
    assert asyncio.run(policy.validate(target)) == target
    assert calls == [("api.example.test", 8443)]


@pytest.mark.parametrize(
    ("addresses", "message"),
    [
        (set(), "did not resolve"),
        ({"8.8.8.8", "127.0.0.1"}, "unsafe address"),
    ],
)
def test_http_policy_rejects_empty_or_mixed_unsafe_dns_results(
    addresses: set[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(url_policy, "_resolve", lambda _host, _port: addresses)

    policy = _policy(allowed_hosts=("*.example.test",))
    with pytest.raises(ConfigurationError, match=message):
        asyncio.run(policy.validate("https://public.example.test/path"))


def test_dns_cache_is_cleared_at_its_hard_entry_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def resolve(hostname: str, _port: int) -> set[str]:
        calls.append(hostname)
        return {"8.8.8.8"}

    monkeypatch.setattr(url_policy, "MAX_DNS_CACHE_ENTRIES", 1)
    monkeypatch.setattr(url_policy, "_resolve", resolve)
    policy = _policy(allowed_hosts=("*.example.test",))

    asyncio.run(policy.validate("https://first.example.test"))
    asyncio.run(policy.validate("https://second.example.test"))

    assert calls == ["first.example.test", "second.example.test"]
    assert len(policy._dns_cache) == 1


def test_resolver_translates_dns_failures_without_socket_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror("synthetic resolver detail")

    monkeypatch.setattr(url_policy.socket, "getaddrinfo", reject)

    with pytest.raises(ConfigurationError, match="Unable to resolve target host") as raised:
        url_policy._resolve("missing.example.test", 443)

    assert "synthetic resolver detail" not in str(raised.value)


def test_standard_mode_allows_public_hosts_without_allowlist_or_attestation() -> None:
    target = "https://8.8.8.8/resource"

    policy = _policy(
        network_mode="standard",
        egress_control_enforced=False,
    )
    assert asyncio.run(policy.validate(target)) == target


def test_restricted_mode_requires_an_allowlist() -> None:
    target = "https://8.8.8.8/resource"

    with pytest.raises(ConfigurationError, match="not allowlisted"):
        asyncio.run(_policy().validate(target))


def test_standard_mode_never_authorizes_unlisted_private_addresses() -> None:
    policy = _policy(
        network_mode="standard",
        allow_private_networks=True,
    )

    with pytest.raises(ConfigurationError, match="unsafe address"):
        asyncio.run(policy.validate("https://127.0.0.1/resource"))


def test_blocked_host_wins_in_standard_and_restricted_modes() -> None:
    target = "https://8.8.8.8/resource"

    for mode in ("standard", "restricted"):
        policy = _policy(
            network_mode=mode,
            allowed_hosts=("8.8.8.8",),
            blocked_hosts=("8.8.8.8",),
        )
        with pytest.raises(ConfigurationError, match="blocked by policy"):
            asyncio.run(policy.validate(target))


def test_allowlisted_private_address_requires_private_network_opt_in() -> None:
    target = "https://127.0.0.1/resource"

    with pytest.raises(ConfigurationError, match="unsafe address"):
        asyncio.run(_policy(allowed_hosts=("127.0.0.1",)).validate(target))

    policy = _policy(
        allow_private_networks=True,
        allowed_hosts=("127.0.0.1",),
    )
    assert asyncio.run(policy.validate(target)) == target


@pytest.mark.parametrize(
    "options",
    [
        {
            "allow_private_networks": True,
            "allow_insecure_local_http": True,
        },
        {
            "environment": "local",
            "allow_insecure_local_http": True,
        },
        {
            "environment": "local",
            "allow_private_networks": True,
        },
    ],
)
def test_plaintext_requires_every_local_loopback_opt_in(
    options: dict[str, object],
) -> None:
    policy = _policy(allowed_hosts=("localhost",), **options)

    with pytest.raises(ConfigurationError, match="Plain HTTP/WS"):
        asyncio.run(policy.validate("http://localhost/resource"))


def test_plaintext_local_loopback_policy_validates_resolved_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_policy,
        "_resolve",
        lambda _host, _port: {"127.0.0.1", "::1"},
    )
    policy = _policy(
        environment="local",
        allow_private_networks=True,
        allowed_hosts=("localhost",),
        allow_insecure_local_http=True,
    )

    assert asyncio.run(policy.validate("http://localhost/resource")) == (
        "http://localhost/resource"
    )
    assert asyncio.run(policy.validate_websocket("ws://localhost/socket")) == (
        "ws://localhost/socket"
    )


def test_plaintext_policy_rejects_non_loopback_hostname_even_if_dns_is_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(url_policy, "_resolve", lambda _host, _port: {"127.0.0.1"})
    policy = _policy(
        environment="local",
        allow_private_networks=True,
        allowed_hosts=("dev.example.test",),
        allow_insecure_local_http=True,
    )

    with pytest.raises(ConfigurationError, match="Plain HTTP/WS"):
        asyncio.run(policy.validate("http://dev.example.test/resource"))


def test_non_local_access_requires_deployment_egress_control() -> None:
    target = "https://8.8.8.8/resource"

    with pytest.raises(ConfigurationError, match="deployment egress"):
        asyncio.run(
            _UrlPolicy(
                network_mode="restricted",
                allowed_hosts=("8.8.8.8",),
            ).validate(target)
        )

    assert asyncio.run(_policy(allowed_hosts=("8.8.8.8",)).validate(target)) == target


@pytest.mark.parametrize(
    "rule",
    [
        "",
        " example.test",
        "example.*",
        "*.127.0.0.1",
        "https://example.test",
        "example..test",
    ],
)
def test_allowed_host_rules_reject_ambiguous_or_malformed_values(rule: str) -> None:
    with pytest.raises(ConfigurationError):
        normalize_allowed_hosts((rule,))


def test_allowed_host_rules_normalize_idna_and_remove_duplicates() -> None:
    assert normalize_allowed_hosts(("BÜCHER.Example.", "xn--bcher-kva.example")) == (
        "xn--bcher-kva.example",
    )


def test_resolver_rejects_excessive_distinct_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"8.8.8.{index}", 443))
        for index in range(MAX_DNS_ADDRESSES + 1)
    ]
    monkeypatch.setattr(
        url_policy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: records,
    )

    with pytest.raises(ConfigurationError, match="exceeds the limit"):
        url_policy._resolve("many.example.test", 443)


def test_url_policy_log_rendering_masks_sensitive_query_values() -> None:
    rendered = _UrlPolicy.for_log(
        "https://example.test/items?token=synthetic-sensitive-value&view=summary"
    )

    assert "synthetic-sensitive-value" not in rendered
    assert parse_qs(urlsplit(rendered).query) == {
        "token": [REDACTED],
        "view": ["summary"],
    }
    # End of URL policy tests.
    #
