"""Reflex state for immutable local run history."""

from __future__ import annotations

import asyncio
from typing import TypedDict

import reflex as rx

from plantain.dashboard.run_catalog import (
    MAX_RUN_SEARCH_LENGTH,
    RunCatalogError,
    RunCatalogItem,
    RunCatalogPage,
    RunDetail,
    is_valid_report_run_id,
    load_run_catalog,
    load_run_detail,
)
from plantain.dashboard.run_evidence import RunEvidenceError
from plantain.dashboard.run_evidence_view import (
    DEFAULT_RUN_EVIDENCE_TAB,
    RUN_EVIDENCE_TABS,
    RunEvidenceView,
    load_run_evidence_view,
)
from plantain.dashboard.state import DashboardState, dashboard_project_root

_RUN_STATUS_FILTERS = frozenset({"all", "passed", "failed", "cancelled"})


class RunSearchFormData(TypedDict):
    """Typed fields submitted by the run-history search form."""

    query: str


class RunCatalogState(DashboardState):
    """Browser-safe state for immutable local result navigation."""

    history_loading: bool = False
    history_detail_loading: bool = False
    history_items: tuple[dict[str, str], ...] = ()
    history_total: int = 0
    history_total_limited: bool = False
    history_page: int = 1
    history_page_count: int = 0
    history_has_previous: bool = False
    history_has_next: bool = False
    history_status_filter: str = "all"
    history_query: str = ""
    history_notice: str = ""
    history_error: str = ""
    history_selected: bool = False
    history_selected_run_id: str = ""
    history_selected_scenario: str = ""
    history_selected_status: str = ""
    history_selected_status_label: str = ""
    history_selected_source: str = ""
    history_selected_started: str = ""
    history_selected_duration: str = ""
    history_selected_steps: str = ""
    history_selected_tags: str = ""
    history_selected_failure: str = ""
    history_selected_failure_type: str = ""
    history_selected_jira: str = ""
    history_selected_test_case: str = ""
    history_selected_test_run: str = ""
    history_selected_integrations: tuple[dict[str, str], ...] = ()
    history_selected_integration_count: int = 0
    history_selected_additional_integrations: int = 0
    history_evidence_open: bool = False
    history_evidence_loading: bool = False
    history_evidence_loaded: bool = False
    history_evidence_tab: str = DEFAULT_RUN_EVIDENCE_TAB
    history_evidence_items: tuple[dict[str, str], ...] = ()
    history_evidence_total: int = 0
    history_evidence_total_limited: bool = False
    history_evidence_page: int = 1
    history_evidence_page_count: int = 0
    history_evidence_has_previous: bool = False
    history_evidence_has_next: bool = False
    history_evidence_notice: str = ""
    history_evidence_error: str = ""
    history_evidence_failure_present: bool = False
    history_evidence_failure_activity: str = ""
    history_evidence_failure_step_id: str = ""
    history_evidence_failure_type: str = ""
    history_evidence_failure_message: str = ""
    history_evidence_failure_message_limited: bool = False
    history_evidence_failure_details: tuple[dict[str, str], ...] = ()
    history_evidence_failure_details_limited: bool = False

    @rx.event
    async def refresh_run_history(self) -> None:
        """Load the first current run-history page."""

        await self._load_history_page(1)

    @rx.event
    async def previous_run_page(self) -> None:
        """Load the previous result page when available."""

        if self.history_loading or not self.history_has_previous:
            return
        await self._load_history_page(self.history_page - 1)

    @rx.event
    async def next_run_page(self) -> None:
        """Load the next result page when available."""

        if self.history_loading or not self.history_has_next:
            return
        await self._load_history_page(self.history_page + 1)

    @rx.event
    async def filter_run_history(self, status: str) -> None:
        """Apply one supported status filter and return to the first page."""

        normalized = status.casefold() if isinstance(status, str) else ""
        if normalized not in _RUN_STATUS_FILTERS:
            self.history_error = "That run filter is unavailable."
            return
        if normalized == self.history_status_filter and self.history_page == 1:
            return
        self.history_status_filter = normalized
        await self._load_history_page(1)

    @rx.event
    async def search_run_history(self, form_data: RunSearchFormData) -> None:
        """Search safe run metadata without loading evidence payloads."""

        query = form_data.get("query", "")
        if not isinstance(query, str) or len(query) > MAX_RUN_SEARCH_LENGTH:
            self.history_error = (
                f"Search terms must be {MAX_RUN_SEARCH_LENGTH} characters or fewer."
            )
            return
        self.history_query = " ".join(query.split())
        await self._load_history_page(1)

    @rx.event
    async def clear_run_search(self) -> None:
        """Clear the current search and return to the first page."""

        if not self.history_query:
            return
        self.history_query = ""
        await self._load_history_page(1)

    @rx.event
    async def select_run(self, run_id: str) -> None:
        """Load bounded metadata for one opaque immutable run."""

        if self.history_detail_loading:
            return
        if not is_valid_report_run_id(run_id):
            self._clear_selected_run()
            self.history_error = "That run is no longer available."
            return
        if run_id != self.history_selected_run_id:
            self._clear_run_evidence()
        self.history_detail_loading = True
        self.history_error = ""
        try:
            detail = await asyncio.to_thread(
                load_run_detail,
                dashboard_project_root(),
                run_id,
            )
        except RunCatalogError:
            self._clear_selected_run()
            self.history_error = "Plantain could not safely load the selected run."
        else:
            self._apply_run_detail(detail)
        finally:
            self.history_detail_loading = False

    @rx.event
    async def open_run_evidence(self) -> None:
        """Open the inspector and load its default tab on demand."""

        if not self.history_selected_run_id:
            self.history_error = "Select a run before opening its evidence."
            return
        if self.history_evidence_open and self.history_evidence_loaded:
            return
        self.history_evidence_open = True
        await self._load_run_evidence_page(self.history_evidence_tab, 1)

    @rx.event
    def close_run_evidence(self) -> None:
        """Close the inspector and release its browser-side projection."""

        self._clear_run_evidence()

    @rx.event
    def change_run_evidence_open(self, opened: bool) -> None:
        """Release browser evidence when the drawer is dismissed."""

        if not opened:
            self._clear_run_evidence()

    @rx.event
    async def show_run_evidence_tab(self, tab: str) -> None:
        """Load one supported evidence category after explicit selection."""

        if tab not in RUN_EVIDENCE_TABS:
            self.history_evidence_error = "That evidence category is unavailable."
            return
        if not self.history_evidence_open or not self.history_selected_run_id:
            self.history_evidence_error = "Select a run before opening its evidence."
            return
        if tab == self.history_evidence_tab and self.history_evidence_loaded:
            return
        self.history_evidence_tab = tab
        await self._load_run_evidence_page(tab, 1)

    @rx.event
    async def previous_run_evidence_page(self) -> None:
        """Load the previous page in the active evidence category."""

        if self.history_evidence_loading or not self.history_evidence_has_previous:
            return
        await self._load_run_evidence_page(
            self.history_evidence_tab,
            self.history_evidence_page - 1,
        )

    @rx.event
    async def next_run_evidence_page(self) -> None:
        """Load the next page in the active evidence category."""

        if self.history_evidence_loading or not self.history_evidence_has_next:
            return
        await self._load_run_evidence_page(
            self.history_evidence_tab,
            self.history_evidence_page + 1,
        )

    async def _load_run_evidence_page(self, tab: str, page_number: int) -> None:
        if self.history_evidence_loading:
            return
        run_id = self.history_selected_run_id
        if not is_valid_report_run_id(run_id):
            self._clear_run_evidence()
            self.history_error = "That run is no longer available."
            return
        self._clear_run_evidence_content()
        self.history_evidence_error = ""
        self.history_evidence_loading = True
        try:
            view = await asyncio.to_thread(
                load_run_evidence_view,
                dashboard_project_root(),
                run_id,
                tab,
                page=page_number,
            )
        except RunEvidenceError:
            self._clear_run_evidence_content()
            self.history_evidence_error = (
                "Plantain could not safely load the selected run evidence."
            )
        else:
            if self.history_selected_run_id == run_id and self.history_evidence_open:
                self._apply_run_evidence_view(view)
        finally:
            self.history_evidence_loading = False

    async def _load_history_page(self, page_number: int) -> None:
        self.history_loading = True
        self.history_error = ""
        self.history_notice = ""
        try:
            page = await asyncio.to_thread(
                load_run_catalog,
                dashboard_project_root(),
                page=page_number,
                status=self.history_status_filter,
                query=self.history_query,
            )
        except RunCatalogError:
            self._clear_run_history()
            self.history_error = "Plantain could not safely load local run history."
        else:
            self._apply_run_history(page)
        finally:
            self.history_loading = False

    def _apply_run_history(self, page: RunCatalogPage) -> None:
        self.history_items = tuple(_run_catalog_row(item) for item in page.items)
        self.history_total = page.total_count
        self.history_total_limited = page.total_count_limited
        self.history_page = page.page
        self.history_page_count = page.page_count
        self.history_has_previous = page.has_previous
        self.history_has_next = page.has_next
        self.history_notice = page.notice
        visible_ids = {item.run_id for item in page.items}
        if self.history_selected_run_id not in visible_ids:
            self._clear_selected_run()

    def _apply_run_detail(self, detail: RunDetail) -> None:
        summary, integrations = _run_detail_projection(detail)
        self.history_selected = True
        self.history_selected_run_id = summary["run_id"]
        self.history_selected_scenario = summary["scenario"]
        self.history_selected_status = summary["status"]
        self.history_selected_status_label = summary["status_label"]
        self.history_selected_source = summary["source"]
        self.history_selected_started = summary["started"]
        self.history_selected_duration = summary["duration"]
        self.history_selected_steps = summary["steps"]
        self.history_selected_tags = summary["tags"]
        self.history_selected_failure = summary["failure"]
        self.history_selected_failure_type = summary["failure_type"]
        self.history_selected_jira = summary["jira_ticket"]
        self.history_selected_test_case = summary["test_case_key"]
        self.history_selected_test_run = summary["test_run_key"]
        self.history_selected_integrations = integrations
        self.history_selected_integration_count = detail.summary.integration_count
        self.history_selected_additional_integrations = detail.additional_integration_count

    def _apply_run_evidence_view(self, view: RunEvidenceView) -> None:
        self.history_evidence_loaded = True
        self.history_evidence_tab = view.tab
        self.history_evidence_items = view.items
        self.history_evidence_total = view.total_count
        self.history_evidence_total_limited = view.total_count_limited
        self.history_evidence_page = view.page
        self.history_evidence_page_count = view.page_count
        self.history_evidence_has_previous = view.has_previous
        self.history_evidence_has_next = view.has_next
        self.history_evidence_notice = view.notice
        failure = view.failure
        if failure is None:
            self._clear_run_failure_evidence()
            return
        self.history_evidence_failure_present = True
        self.history_evidence_failure_activity = failure.activity
        self.history_evidence_failure_step_id = failure.step_id
        self.history_evidence_failure_type = failure.error_type
        self.history_evidence_failure_message = failure.message
        self.history_evidence_failure_message_limited = failure.message_limited
        self.history_evidence_failure_details = failure.details
        self.history_evidence_failure_details_limited = failure.details_limited

    def _clear_run_history(self) -> None:
        self.history_items = ()
        self.history_total = 0
        self.history_total_limited = False
        self.history_page = 1
        self.history_page_count = 0
        self.history_has_previous = False
        self.history_has_next = False
        self.history_notice = ""
        self._clear_selected_run()

    def _clear_selected_run(self) -> None:
        self.history_selected = False
        self.history_selected_run_id = ""
        self.history_selected_scenario = ""
        self.history_selected_status = ""
        self.history_selected_status_label = ""
        self.history_selected_source = ""
        self.history_selected_started = ""
        self.history_selected_duration = ""
        self.history_selected_steps = ""
        self.history_selected_tags = ""
        self.history_selected_failure = ""
        self.history_selected_failure_type = ""
        self.history_selected_jira = ""
        self.history_selected_test_case = ""
        self.history_selected_test_run = ""
        self.history_selected_integrations = ()
        self.history_selected_integration_count = 0
        self.history_selected_additional_integrations = 0
        self._clear_run_evidence()

    def _clear_run_evidence(self) -> None:
        self.history_evidence_open = False
        self.history_evidence_loading = False
        self.history_evidence_tab = DEFAULT_RUN_EVIDENCE_TAB
        self.history_evidence_error = ""
        self._clear_run_evidence_content()

    def _clear_run_evidence_content(self) -> None:
        self.history_evidence_loaded = False
        self.history_evidence_items = ()
        self.history_evidence_total = 0
        self.history_evidence_total_limited = False
        self.history_evidence_page = 1
        self.history_evidence_page_count = 0
        self.history_evidence_has_previous = False
        self.history_evidence_has_next = False
        self.history_evidence_notice = ""
        self._clear_run_failure_evidence()

    def _clear_run_failure_evidence(self) -> None:
        self.history_evidence_failure_present = False
        self.history_evidence_failure_activity = ""
        self.history_evidence_failure_step_id = ""
        self.history_evidence_failure_type = ""
        self.history_evidence_failure_message = ""
        self.history_evidence_failure_message_limited = False
        self.history_evidence_failure_details = ()
        self.history_evidence_failure_details_limited = False


def _run_catalog_row(item: RunCatalogItem) -> dict[str, str]:
    failure = item.failure
    tags = " · ".join(item.tags)
    if item.additional_tag_count:
        suffix = f"+{item.additional_tag_count} more"
        tags = f"{tags} · {suffix}" if tags else suffix
    return {
        "run_id": item.run_id,
        "short_id": item.run_id[:8],
        "scenario": item.scenario,
        "status": item.status,
        "status_label": _run_status_label(item.status),
        "source": item.source,
        "started": item.started,
        "duration": item.duration,
        "steps": (
            f"{item.passed_step_count}/{item.step_count} steps passed"
            if item.step_count != 1
            else f"{item.passed_step_count}/1 step passed"
        ),
        "tags": tags or "No tags",
        "failure": (f"{failure.activity} · {failure.step_id}" if failure is not None else ""),
        "failure_type": failure.error_type if failure is not None else "",
        "integrations": (
            f"{item.integration_count} "
            f"{'integration' if item.integration_count == 1 else 'integrations'}"
        ),
    }


def _run_detail_projection(
    detail: RunDetail,
) -> tuple[dict[str, str], tuple[dict[str, str], ...]]:
    summary = _run_catalog_row(detail.summary)
    summary.update(
        {
            "jira_ticket": detail.jira_ticket,
            "test_case_key": detail.test_case_key,
            "test_run_key": detail.test_run_key,
        }
    )
    integrations = tuple(
        {"provider": item.provider, "status": item.status} for item in detail.integrations
    )
    return summary, integrations


def _run_status_label(status: str) -> str:
    return {
        "passed": "Passed",
        "failed": "Failed",
        "cancelled": "Stopped",
    }.get(status, "Unavailable")


__all__ = ["RunCatalogState"]
