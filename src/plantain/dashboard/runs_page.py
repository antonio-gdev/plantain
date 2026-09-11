"""Responsive immutable run-history route."""

from __future__ import annotations

import reflex as rx

from plantain.dashboard.run_detail_panel import run_detail_panel
from plantain.dashboard.run_evidence_panel import run_evidence_drawer
from plantain.dashboard.run_history_panel import (
    history_controls,
    history_feedback,
    history_panel,
)
from plantain.dashboard.run_state import RunCatalogState
from plantain.dashboard.shell import component, page_shell


def runs_page() -> rx.Component:
    """Render searchable immutable local history and selected metadata."""

    content = rx.vstack(
        rx.hstack(
            rx.heading("RUNS AND RESULTS", size="4", letter_spacing="-0.025em"),
            rx.badge(
                RunCatalogState.history_total,
                rx.cond(RunCatalogState.history_total_limited, "+", ""),
                color_scheme="blue",
                size="2",
                variant="soft",
            ),
            align="center",
            spacing="2",
        ),
        history_feedback(),
        history_controls(),
        rx.grid(
            history_panel(),
            run_detail_panel(),
            columns={
                "initial": "minmax(0, 1fr)",
                "lg": "minmax(0, 1.15fr) minmax(320px, 0.85fr)",
            },
            gap="4",
            width="100%",
        ),
        run_evidence_drawer(),
        align="stretch",
        max_width="1240px",
        spacing="5",
        width="100%",
    )
    return page_shell(component(content), active="runs", title="Runs and Results")


__all__ = ["runs_page"]
