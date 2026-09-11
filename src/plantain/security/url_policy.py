"""URL validation and SSRF-resistant network policy."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from functools import cache
from threading import Event
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from plantain.errors import ConfigurationError
from plantain.security.redaction import redact_url

if TYPE_CHECKING:
    from plantain.engine.admission import ResourceAdmission

MAX_URL_LENGTH = 8_192
MAX_DNS_CACHE_ENTRIES = 1_024
MAX_DNS_ADDRESSES = 64
MAX_ALLOWED_HOSTS = 256
MAX_ALLOWED_TCP_TARGETS = 256
MAX_HOST_LENGTH = 253
MAX_HOST_LABEL_LENGTH = 63
MAX_TCP_PORT = 65_535
IPV6_VERSION = 6
DNS_CACHE_TTL_SECONDS = 2.0
DNS_RESOLUTION_WALL_TIMEOUT_SECONDS = 10.0
DNS_RESOLUTION_CPU_TIMEOUT_SECONDS = 2
DNS_RESOLUTION_MEMORY_BYTES = 512 * 1_024 * 1_024
HTTP_PORT = 80
HTTPS_PORT = 443
WS_PORT = 80
WSS_PORT = 443
NETWORK_MODE_STANDARD = "standard"
NETWORK_MODE_RESTRICTED = "restricted"
NETWORK_MODES = frozenset({NETWORK_MODE_STANDARD, NETWORK_MODE_RESTRICTED})
_CONTROL_CHARACTER = re.compile(r"[\x00-\x20\x7f]")


def normalize_network_mode(value: str) -> str:
    """Normalize the operator-selected public or allowlist-only network mode."""

    normalized = value.strip().casefold()
    if normalized not in NETWORK_MODES:
        raise ConfigurationError("Network mode must be 'standard' or 'restricted'")
    return normalized


def normalize_hostname(value: str) -> str:
    """Normalize one IP literal or IDNA hostname without accepting URL syntax."""

    candidate = value
    if not candidate or candidate != candidate.strip() or _CONTROL_CHARACTER.search(candidate):
        raise ConfigurationError("Target host is malformed")
    if candidate.startswith("[") or candidate.endswith("]"):
        if not (candidate.startswith("[") and candidate.endswith("]")):
            raise ConfigurationError("Target host is malformed")
        candidate = candidate[1:-1]
    candidate = candidate.removesuffix(".")
    if not candidate or "%" in candidate or "*" in candidate:
        raise ConfigurationError("Target host is malformed")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        try:
            normalized = candidate.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise ConfigurationError("Target host is malformed") from exc
    labels = normalized.split(".")
    if (
        len(normalized) > MAX_HOST_LENGTH
        or any(not label or len(label) > MAX_HOST_LABEL_LENGTH for label in labels)
        or any(
            not label[0].isalnum()
            or not label[-1].isalnum()
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        )
    ):
        raise ConfigurationError("Target host is malformed")
    return normalized


def _normalize_host_rules(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """Normalize exact hosts and explicit ``*.subdomain`` rules."""

    if len(values) > MAX_ALLOWED_HOSTS:
        raise ConfigurationError(f"{label} hosts exceed the limit of {MAX_ALLOWED_HOSTS}")
    normalized: set[str] = set()
    for value in values:
        if value != value.strip():
            raise ConfigurationError(f"{label} host entries contain surrounding whitespace")
        wildcard = value.startswith("*.")
        host_value = value[2:] if wildcard else value
        if "*" in host_value:
            raise ConfigurationError(f"{label} host wildcards must use '*.example.com'")
        host = normalize_hostname(host_value)
        if wildcard:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                host = f"*.{host}"
            else:
                raise ConfigurationError(f"{label} IP address rules cannot use wildcards")
        normalized.add(host)
    return tuple(sorted(normalized))


def normalize_allowed_hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize allow rules."""

    return _normalize_host_rules(values, label="Allowed")


def normalize_blocked_hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize deny rules."""

    return _normalize_host_rules(values, label="Blocked")


def normalize_allowed_tcp_targets(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize exact ``host:port`` targets; wildcards are never accepted."""

    if len(values) > MAX_ALLOWED_TCP_TARGETS:
        raise ConfigurationError(
            f"Allowed TCP targets exceed the limit of {MAX_ALLOWED_TCP_TARGETS}"
        )
    return tuple(sorted({_canonical_tcp_target(*_parse_tcp_target(value)) for value in values}))


def _parse_tcp_target(value: str) -> tuple[str, int]:
    if not value or value != value.strip() or _CONTROL_CHARACTER.search(value):
        raise ConfigurationError("Allowed TCP target is malformed")
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or value[closing + 1 : closing + 2] != ":":
            raise ConfigurationError("Allowed TCP target is malformed")
        host_value = value[1:closing]
        raw_port = value[closing + 2 :]
    else:
        host_value, separator, raw_port = value.rpartition(":")
        if not separator or ":" in host_value:
            raise ConfigurationError("IPv6 TCP targets must use the '[address]:port' form")
    if not raw_port.isascii() or not raw_port.isdigit():
        raise ConfigurationError("Allowed TCP target port must be an integer")
    port = int(raw_port)
    if port < 1 or port > MAX_TCP_PORT:
        raise ConfigurationError(f"Allowed TCP target port must be between 1 and {MAX_TCP_PORT}")
    return normalize_hostname(host_value), port


def _canonical_tcp_target(hostname: str, port: int) -> str:
    try:
        is_ipv6 = ipaddress.ip_address(hostname).version == IPV6_VERSION
    except ValueError:
        is_ipv6 = False
    rendered_host = f"[{hostname}]" if is_ipv6 else hostname
    return f"{rendered_host}:{port}"


def _require_deployment_egress_control(
    *,
    environment: str,
    enforced: bool,
) -> None:
    if environment.casefold() != "local" and not enforced:
        raise ConfigurationError(
            "Non-local outbound access requires an enforced deployment egress firewall or proxy"
        )


def _host_is_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(
        (
            hostname != rule[2:] and hostname.endswith(f".{rule[2:]}")
            if rule.startswith("*.")
            else hostname == rule
        )
        for rule in allowed_hosts
    )


def _validate_addresses(
    addresses: set[str],
    *,
    allow_private: bool,
    require_loopback: bool = False,
) -> None:
    if not addresses:
        raise ConfigurationError("Target host did not resolve to an address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        allowed = (
            ip.is_loopback
            if require_loopback
            else not (ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local)
            if allow_private
            else ip.is_global
        )
        if not allowed:
            raise ConfigurationError(
                "Target resolves to a private, local, reserved, or otherwise unsafe address"
            )


def _resolve(hostname: str, port: int) -> set[str]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ConfigurationError("Unable to resolve target host") from exc
    addresses: set[str] = set()
    for record in records:
        try:
            address = str(ipaddress.ip_address(record[4][0]))
        except ValueError as exc:
            raise ConfigurationError("Target DNS returned an invalid address") from exc
        addresses.add(address)
        if len(addresses) > MAX_DNS_ADDRESSES:
            raise ConfigurationError(
                f"Target DNS exceeds the limit of {MAX_DNS_ADDRESSES} addresses"
            )
    return addresses


@cache
def _default_dns_admission() -> ResourceAdmission:
    from plantain.engine.admission import ResourceAdmission  # noqa: PLC0415

    return ResourceAdmission(object())


async def _resolve_isolated(
    admission: ResourceAdmission,
    hostname: str,
    port: int,
) -> set[str]:
    from plantain.engine.process_worker import (  # noqa: PLC0415
        ProcessLimits,
        ProcessWorkerError,
        ProcessWorkerTimeoutError,
    )

    limits = ProcessLimits(
        wall_timeout_seconds=DNS_RESOLUTION_WALL_TIMEOUT_SECONDS,
        cpu_timeout_seconds=DNS_RESOLUTION_CPU_TIMEOUT_SECONDS,
        memory_bytes=DNS_RESOLUTION_MEMORY_BYTES,
    )
    try:
        return await admission.run_process(_resolve, hostname, port, limits=limits)
    except ProcessWorkerTimeoutError as exc:
        raise ConfigurationError("Target DNS resolution exceeded its safety deadline") from exc
    except ProcessWorkerError as exc:
        raise ConfigurationError("Target DNS resolution failed safely") from exc


def _resolve_isolated_sync(
    admission: ResourceAdmission,
    hostname: str,
    port: int,
    cancelled: Event,
) -> set[str]:
    from plantain.engine.process_worker import (  # noqa: PLC0415
        ProcessLimits,
        ProcessWorkerError,
        ProcessWorkerTimeoutError,
    )

    limits = ProcessLimits(
        wall_timeout_seconds=DNS_RESOLUTION_WALL_TIMEOUT_SECONDS,
        cpu_timeout_seconds=DNS_RESOLUTION_CPU_TIMEOUT_SECONDS,
        memory_bytes=DNS_RESOLUTION_MEMORY_BYTES,
    )
    try:
        return admission.run_process_sync(
            _resolve,
            (hostname, port),
            {},
            limits=limits,
            cancelled=cancelled,
        )
    except ProcessWorkerTimeoutError as exc:
        raise ConfigurationError("Target DNS resolution exceeded its safety deadline") from exc
    except ProcessWorkerError as exc:
        raise ConfigurationError("Target DNS resolution failed safely") from exc


@dataclass(slots=True)
class UrlPolicy:
    """Validates outbound HTTP(S) targets before browser or API access."""

    environment: str = "production"
    network_mode: str = NETWORK_MODE_STANDARD
    allow_private_networks: bool = False
    allowed_hosts: tuple[str, ...] = ()
    blocked_hosts: tuple[str, ...] = ()
    allow_insecure_local_http: bool = False
    egress_control_enforced: bool = False
    admission: ResourceAdmission | None = field(default=None, repr=False)
    _dns_cache: dict[tuple[str, int], tuple[float, set[str]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _dns_inflight: dict[tuple[str, int], asyncio.Task[set[str]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _dns_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.network_mode = normalize_network_mode(self.network_mode)
        self.allowed_hosts = normalize_allowed_hosts(self.allowed_hosts)
        self.blocked_hosts = normalize_blocked_hosts(self.blocked_hosts)

    async def validate(self, url: str) -> str:
        return await self._validate_target(
            url,
            allowed_schemes={"http", "https"},
            default_ports={"http": HTTP_PORT, "https": HTTPS_PORT},
            scheme_error="Only http and https targets are supported",
        )

    async def validate_websocket(self, url: str) -> str:
        """Validate an outbound browser WebSocket using the HTTP egress policy."""

        return await self._validate_target(
            url,
            allowed_schemes={"ws", "wss"},
            default_ports={"ws": WS_PORT, "wss": WSS_PORT},
            scheme_error="Only ws and wss WebSocket targets are supported",
        )

    async def _validate_target(
        self,
        url: str,
        *,
        allowed_schemes: set[str],
        default_ports: dict[str, int],
        scheme_error: str,
    ) -> str:
        if len(url) > MAX_URL_LENGTH:
            raise ConfigurationError("Target URL exceeds the 8192-character safety limit")
        if url != url.strip() or _CONTROL_CHARACTER.search(url):
            raise ConfigurationError("Target URL is malformed")
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ConfigurationError("Target URL is malformed") from exc
        scheme = parsed.scheme.lower()
        if scheme not in allowed_schemes:
            raise ConfigurationError(scheme_error)
        if not parsed.hostname:
            raise ConfigurationError("Target URL must include a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError("Credentials must not be embedded in target URLs")

        try:
            port = parsed.port or default_ports[scheme]
        except ValueError as exc:
            raise ConfigurationError("Target URL contains an invalid port") from exc

        hostname = normalize_hostname(parsed.hostname)
        insecure = scheme in {"http", "ws"}
        if insecure and (
            self.environment.casefold() != "local"
            or not self.allow_insecure_local_http
            or not self.allow_private_networks
            or not _is_loopback_hostname(hostname)
        ):
            raise ConfigurationError(
                "Plain HTTP/WS is permitted only for explicit loopback local development"
            )

        if _host_is_allowed(hostname, self.blocked_hosts):
            raise ConfigurationError(f"Target host is blocked by policy: {hostname}")
        host_allowlisted = _host_is_allowed(hostname, self.allowed_hosts)
        if self.network_mode == NETWORK_MODE_RESTRICTED and not host_allowlisted:
            raise ConfigurationError(f"Target host is not allowlisted: {hostname}")
        if self.network_mode == NETWORK_MODE_RESTRICTED:
            _require_deployment_egress_control(
                environment=self.environment,
                enforced=self.egress_control_enforced,
            )

        try:
            literal = ipaddress.ip_address(hostname)
            addresses = {str(literal)}
        except ValueError:
            addresses = await self._resolved_addresses(hostname, port)
        _validate_addresses(
            addresses,
            allow_private=self.allow_private_networks and host_allowlisted,
            require_loopback=insecure,
        )
        return url

    async def _resolved_addresses(self, hostname: str, port: int) -> set[str]:
        key = (hostname, port)
        now = time.monotonic()
        async with self._dns_lock:
            cached = self._dns_cache.get(key)
            if cached is not None and cached[0] > now:
                return set(cached[1])
            task = self._dns_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    _resolve_isolated(
                        self.admission or _default_dns_admission(),
                        hostname,
                        port,
                    )
                )
                self._dns_inflight[key] = task
        try:
            addresses = await task
        finally:
            async with self._dns_lock:
                self._dns_inflight.pop(key, None)
        async with self._dns_lock:
            if len(self._dns_cache) >= MAX_DNS_CACHE_ENTRIES:
                self._dns_cache.clear()
            # This short lifetime collapses parallel subresource lookups while
            # still rechecking hosts frequently against DNS rebinding.
            self._dns_cache[key] = (
                time.monotonic() + DNS_CACHE_TTL_SECONDS,
                set(addresses),
            )
        return addresses

    @staticmethod
    def for_log(url: str) -> str:
        return redact_url(url)


@dataclass(slots=True)
class TcpTargetPolicy:
    """Exact host/port authorization with fresh DNS validation per acquisition."""

    allowed_targets: tuple[str, ...] = ()
    allow_private_networks: bool = False
    environment: str = "production"
    egress_control_enforced: bool = False

    def __post_init__(self) -> None:
        self.allowed_targets = normalize_allowed_tcp_targets(self.allowed_targets)

    def validate(
        self,
        hostname: str,
        port: int,
        *,
        admission: ResourceAdmission | None = None,
        cancelled: Event | None = None,
    ) -> str:
        normalized_host = normalize_hostname(hostname)
        if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > MAX_TCP_PORT:
            raise ConfigurationError("Outbound TCP target port is invalid")
        target = _canonical_tcp_target(normalized_host, port)
        if target not in self.allowed_targets:
            raise ConfigurationError("Outbound TCP target is not allowlisted")
        _require_deployment_egress_control(
            environment=self.environment,
            enforced=self.egress_control_enforced,
        )
        try:
            literal = ipaddress.ip_address(normalized_host)
            addresses = {str(literal)}
        except ValueError:
            addresses = _resolve_isolated_sync(
                admission or _default_dns_admission(),
                normalized_host,
                port,
                cancelled or Event(),
            )
        _validate_addresses(addresses, allow_private=self.allow_private_networks)
        return target


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


__all__ = [
    "NETWORK_MODE_RESTRICTED",
    "NETWORK_MODE_STANDARD",
    "TcpTargetPolicy",
    "UrlPolicy",
    "normalize_allowed_hosts",
    "normalize_allowed_tcp_targets",
    "normalize_blocked_hosts",
    "normalize_hostname",
    "normalize_network_mode",
]
