"""Shared process contract for Plantain's local dashboard runtime."""

from __future__ import annotations

from collections.abc import Mapping

DASHBOARD_PORT_ENV = "PLANTAIN_DASHBOARD_PORT"
DEFAULT_DASHBOARD_PORT = 3_000
MAX_DASHBOARD_PORT = 65_535
MIN_DASHBOARD_PORT = 1
PROJECT_ROOT_ENV = "PLANTAIN_DASHBOARD_PROJECT_ROOT"


def validate_dashboard_port(value: object) -> int:
    """Return one valid TCP port without reflecting invalid input."""

    if isinstance(value, bool):
        raise TypeError("Dashboard port must be an integer between 1 and 65535")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str):
        try:
            port = int(value.strip())
        except ValueError as exc:
            raise ValueError("Dashboard port must be an integer between 1 and 65535") from exc
    else:
        raise TypeError("Dashboard port must be an integer between 1 and 65535")
    if port < MIN_DASHBOARD_PORT or port > MAX_DASHBOARD_PORT:
        raise ValueError("Dashboard port must be an integer between 1 and 65535")
    return port


def configured_dashboard_port(environment: Mapping[str, str]) -> int | None:
    """Load the launcher-selected port, preserving source-development defaults."""

    raw_port = environment.get(DASHBOARD_PORT_ENV)
    if raw_port is None or not raw_port.strip():
        return None
    return validate_dashboard_port(raw_port)


__all__ = [
    "DASHBOARD_PORT_ENV",
    "DEFAULT_DASHBOARD_PORT",
    "MAX_DASHBOARD_PORT",
    "MIN_DASHBOARD_PORT",
    "PROJECT_ROOT_ENV",
    "configured_dashboard_port",
    "validate_dashboard_port",
]
