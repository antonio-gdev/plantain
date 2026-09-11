"""Hardened Reflex configuration for Plantain's local dashboard."""

from __future__ import annotations

import os

import reflex as rx
from reflex_base.plugins.sitemap import SitemapPlugin

from plantain.dashboard.runtime_contract import (
    DEFAULT_DASHBOARD_PORT,
    configured_dashboard_port,
)

DEFAULT_DASHBOARD_BACKEND_PORT = 8_000
_configured_port = configured_dashboard_port(os.environ)
_frontend_port = _configured_port or DEFAULT_DASHBOARD_PORT
_backend_port = _configured_port or DEFAULT_DASHBOARD_BACKEND_PORT

LOCAL_DASHBOARD_ORIGINS = (
    f"http://127.0.0.1:{_frontend_port}",
    f"http://localhost:{_frontend_port}",
)

config = rx.Config(
    app_name="plantain_dashboard",
    app_module_import="plantain.dashboard.app",
    api_url=f"http://127.0.0.1:{_backend_port}",
    deploy_url=f"http://127.0.0.1:{_frontend_port}",
    backend_host="127.0.0.1",
    cors_allowed_origins=LOCAL_DASHBOARD_ORIGINS,
    plugins=[
        rx.plugins.RadixThemesPlugin(
            theme=rx.theme(
                accent_color="blue",
                gray_color="slate",
                radius="large",
            )
        )
    ],
    disable_plugins=[SitemapPlugin],
    env_file=None,
    react_strict_mode=True,
    telemetry_enabled=False,
)

__all__ = ["config"]
