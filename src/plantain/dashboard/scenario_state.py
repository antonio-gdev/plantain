"""Reflex state for the bounded dashboard Tests catalog."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from typing import Literal

import reflex as rx

from plantain.dashboard.agent.runtime import (
    DashboardAgentError,
    author_dashboard_database_continuation,
    author_dashboard_ui_continuation,
)
from plantain.dashboard.database_workflow_store import (
    is_valid_database_workflow_id,
)
from plantain.dashboard.draft_store import DashboardDraftError
from plantain.dashboard.run_progress import DashboardRunProgress
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogError,
    ScenarioCatalogFilter,
    ScenarioCatalogItem,
    ScenarioCatalogPage,
    ScenarioCatalogSelection,
    is_valid_scenario_id,
    load_scenario_catalog,
    load_scenario_selection,
)
from plantain.dashboard.scenario_detail import (
    ScenarioDetailError,
    ScenarioDetailPage,
    ScenarioStepDetail,
    load_scenario_detail,
)
from plantain.dashboard.scenario_execution import (
    DashboardRunError,
    DashboardRunOutcome,
    cancel_dashboard_run,
    dashboard_run_capacity,
    is_valid_dashboard_job_id,
    monitor_dashboard_run,
    start_dashboard_run,
)
from plantain.dashboard.state import (
    DashboardState,
    background_event,
    dashboard_project_root,
    store_database_authoring_draft,
    store_ui_authoring_draft,
)
from plantain.dashboard.ui_evidence import UiEvidenceError, load_ui_run_evidence
from plantain.dashboard.ui_workflow_store import is_valid_ui_workflow_id

MILLISECONDS_PER_SECOND = 1_000
BATCH_ID_BYTES = 16
MAX_SESSION_RUN_RESULTS = 8
MAX_PRESENTATION_RUN_NAME_LENGTH = 160
MAX_TEST_FILTER_INPUT_LENGTH = 4_096
_TestBatchRequest = tuple[
    str,
    ScenarioCatalogFilter,
    tuple[str, ...],
    frozenset[str],
]
_PreparedTestBatch = tuple[tuple[ScenarioCatalogItem, ...], int, str]


class ScenarioCatalogState(DashboardState):
    """Browser-safe state for deterministic test-library navigation."""

    test_catalog_loading: bool = False
    test_catalog_items: tuple[dict[str, str], ...] = ()
    test_catalog_total: int = 0
    test_catalog_page: int = 1
    test_catalog_page_count: int = 0
    test_catalog_has_previous: bool = False
    test_catalog_has_next: bool = False
    test_catalog_notice: str = ""
    test_catalog_error: str = ""
    test_detail_open: bool = False
    test_detail_loading: bool = False
    test_detail_scenario_id: str = ""
    test_detail_name: str = ""
    test_detail_source: str = ""
    test_detail_steps: tuple[dict[str, str], ...] = ()
    test_detail_total_steps: int = 0
    test_detail_page: int = 1
    test_detail_page_count: int = 0
    test_detail_has_previous: bool = False
    test_detail_has_next: bool = False
    test_detail_notice: str = ""
    test_detail_error: str = ""
    test_filter_query: str = ""
    test_filter_required_all: str = ""
    test_filter_required_any: str = ""
    test_filter_excluded: str = ""
    test_filters_active: bool = False
    test_selection_mode: bool = False
    test_selected_ids: tuple[str, ...] = ()
    test_selected_count: int = 0
    test_validation_loading: bool = False
    test_validation_notice: str = ""
    test_batch_id: str = ""
    test_batch_active: bool = False
    test_batch_total: int = 0
    test_batch_completed: int = 0
    test_batch_stop_requested: bool = False
    test_active_run_ids: tuple[str, ...] = ()
    test_active_run_count: int = 0
    test_active_runs: tuple[dict[str, str], ...] = ()
    test_run_results: tuple[dict[str, str], ...] = ()
    test_run_result_count: int = 0
    test_run_notice: str = ""

    @rx.event
    async def refresh_tests(self) -> None:
        """Load the first current catalog page without blocking Reflex."""

        await self._load_test_page(1)

    @rx.event
    def change_test_filter_query(self, value: str) -> None:
        if len(value) <= MAX_TEST_FILTER_INPUT_LENGTH:
            self.test_filter_query = value

    @rx.event
    def change_test_filter_required_all(self, value: str) -> None:
        if len(value) <= MAX_TEST_FILTER_INPUT_LENGTH:
            self.test_filter_required_all = value

    @rx.event
    def change_test_filter_required_any(self, value: str) -> None:
        if len(value) <= MAX_TEST_FILTER_INPUT_LENGTH:
            self.test_filter_required_any = value

    @rx.event
    def change_test_filter_excluded(self, value: str) -> None:
        if len(value) <= MAX_TEST_FILTER_INPUT_LENGTH:
            self.test_filter_excluded = value

    @rx.event
    def begin_test_selection(self) -> None:
        """Reveal explicit row selection without changing the active scope."""

        if self.test_batch_active:
            return
        self.test_selection_mode = True
        self._set_test_selection(())
        self.test_validation_notice = ""

    @rx.event
    def end_test_selection(self) -> None:
        """Leave selection mode and restore matching-scope actions."""

        if self.test_batch_active:
            return
        self.test_selection_mode = False
        self._set_test_selection(())
        self.test_validation_notice = ""

    @rx.event
    def toggle_test_selection(self, scenario_id: str) -> None:
        """Select or clear one test from the currently visible page."""

        if not self.test_selection_mode or self.test_batch_active:
            return
        visible_ids = {item["scenario_id"] for item in self.test_catalog_items}
        if not is_valid_scenario_id(scenario_id) or scenario_id not in visible_ids:
            self.test_validation_notice = "That test is no longer visible."
            return
        self._set_test_selection(_toggle_test_selection(self.test_selected_ids, scenario_id))

    @rx.event
    def select_test_page(self) -> None:
        """Add the current page to the browser-safe selection."""

        if not self.test_selection_mode or self.test_batch_active:
            return
        visible_ids = tuple(item["scenario_id"] for item in self.test_catalog_items)
        self._set_test_selection(_merge_test_selection(self.test_selected_ids, visible_ids))

    @rx.event
    def clear_test_selection(self) -> None:
        """Clear selected tests without changing the active filters."""

        self._set_test_selection(())
        self.test_validation_notice = ""

    @background_event
    async def validate_test_selection(self) -> None:
        """Validate selected tests, or every current match when none are selected."""

        async with self:
            if self.test_validation_loading:
                return
            try:
                catalog_filter = self._current_test_catalog_filter()
            except ScenarioCatalogError:
                self.test_validation_notice = "Review the current search and tag filters."
                return
            selected_ids = self.test_selected_ids
            self.test_validation_loading = True
            self.test_validation_notice = ""
        notice = "Plantain could not safely validate the selected tests."
        try:
            selection = await asyncio.to_thread(
                load_scenario_selection,
                dashboard_project_root(),
                catalog_filter=catalog_filter,
            )
        except ScenarioCatalogError:
            pass
        else:
            notice = _selection_validation_notice(selection, selected_ids)
        finally:
            async with self:
                self.test_validation_loading = False
                self.test_validation_notice = notice

    @background_event
    async def run_test_selection(self) -> None:
        """Run selected tests, or every current match, with bounded concurrency."""

        async with self:
            request = self._begin_test_batch()
        if request is None:
            return
        batch_id = request[0]
        terminal_notice = ""
        try:
            items, capacity, terminal_notice = await self._prepare_test_batch(request)
            if terminal_notice:
                return
            await self._run_test_batch_workers(batch_id, items, capacity)
        except asyncio.CancelledError:
            terminal_notice = "The batch was stopped before completion."
            raise
        except (DashboardRunError, ScenarioCatalogError, RuntimeError):
            terminal_notice = "Plantain could not safely run the selected test batch."
        finally:
            async with self:
                self._finish_test_batch(batch_id, terminal_notice)

    @rx.event
    def cancel_test_batch(self, batch_id: str) -> None:
        """Stop active jobs in this browser-owned batch and drain its queue."""

        if not self.test_batch_active or batch_id != self.test_batch_id:
            self.test_run_notice = "That batch is no longer active in this session."
            return
        self.test_batch_stop_requested = True
        job_ids = tuple(
            row["job_id"]
            for row in self.test_active_runs
            if row["batch_id"] == batch_id and is_valid_dashboard_job_id(row["job_id"])
        )
        for job_id in job_ids:
            cancel_dashboard_run(job_id)
        self.test_run_notice = "Stopping active tests and clearing the remaining batch queue."

    @rx.event
    async def previous_test_page(self) -> None:
        """Load the prior catalog page when one exists."""

        if self.test_catalog_loading or not self.test_catalog_has_previous:
            return
        await self._load_test_page(self.test_catalog_page - 1)

    @rx.event
    async def next_test_page(self) -> None:
        """Load the next catalog page when one exists."""

        if self.test_catalog_loading or not self.test_catalog_has_next:
            return
        await self._load_test_page(self.test_catalog_page + 1)

    @rx.event
    async def apply_test_filters(self) -> None:
        """Apply the current search and tag predicates to the whole catalog."""

        if self.test_catalog_loading:
            return
        self._set_test_selection(())
        self.test_validation_notice = ""
        await self._load_test_page(1)

    @rx.event
    async def clear_test_filters(self) -> None:
        """Restore the complete deterministic test catalog."""

        if self.test_catalog_loading:
            return
        self.test_filter_query = ""
        self.test_filter_required_all = ""
        self.test_filter_required_any = ""
        self.test_filter_excluded = ""
        self._set_test_selection(())
        self.test_validation_notice = ""
        await self._load_test_page(1)

    @background_event
    async def open_test_detail(self, scenario_id: str) -> None:
        """Open one currently visible test through its opaque identifier."""

        async with self:
            visible_ids = {item["scenario_id"] for item in self.test_catalog_items}
            if not is_valid_scenario_id(scenario_id) or scenario_id not in visible_ids:
                self.test_detail_error = "That test is no longer visible."
                return
            self.test_detail_open = True
            self.test_detail_scenario_id = scenario_id
            self.test_detail_loading = True
            self.test_detail_error = ""
        await self._load_test_detail(scenario_id, 1)

    @rx.event
    def change_test_detail_open(self, opened: bool) -> None:
        """Clear detail projection whenever its drawer closes."""

        if not opened:
            self._clear_test_detail()

    @background_event
    async def show_test_detail_page(self, page_number: int) -> None:
        """Load another bounded page for the currently open test."""

        async with self:
            if not self.test_detail_open or self.test_detail_loading:
                return
            scenario_id = self.test_detail_scenario_id
            if not is_valid_scenario_id(scenario_id):
                self._clear_test_detail()
                return
            self.test_detail_loading = True
            self.test_detail_error = ""
        await self._load_test_detail(scenario_id, page_number)

    @background_event
    async def run_test(self, scenario_id: str, requested_name: str) -> None:
        """Run one explicitly selected, currently valid scenario."""

        await self._execute_test_run(scenario_id, requested_name)

    async def _execute_test_run(
        self,
        scenario_id: str,
        requested_name: str,
        *,
        batch_id: str = "",
    ) -> None:
        """Execute one test through the shared dashboard job lifecycle."""

        async with self:
            if not is_valid_scenario_id(scenario_id):
                self.test_run_notice = "The selected test is no longer available."
                return
            active_ids = _activate_run(self.test_active_run_ids, scenario_id)
            if active_ids == self.test_active_run_ids:
                return
            database_workflow_id = _database_workflow_for_run(
                self.database_workflow_id,
                self.database_workflow_scenario_id,
                scenario_id,
            )
            ui_workflow_id = _ui_workflow_for_run(
                self.ui_workflow_id,
                self.ui_workflow_scenario_id,
                scenario_id,
            )
            run_name = _catalog_run_name(
                self.test_catalog_items,
                scenario_id,
                requested_name,
            )
            self.test_active_run_ids = active_ids
            self.test_active_run_count = len(self.test_active_run_ids)
            self.test_active_runs = (
                *self.test_active_runs,
                _active_run_row(
                    scenario_id,
                    run_name,
                    database_workflow_id,
                    ui_workflow_id,
                    batch_id,
                ),
            )
            self.test_run_notice = ""

        result_row: dict[str, str] | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            handle = start_dashboard_run(
                dashboard_project_root(),
                scenario_id,
            )
            async with self:
                self.test_active_runs = _assign_run_job(
                    self.test_active_runs,
                    scenario_id,
                    handle.job_id,
                )
                should_cancel = (
                    bool(batch_id)
                    and self.test_batch_id == batch_id
                    and self.test_batch_stop_requested
                )
            if should_cancel:
                cancel_dashboard_run(handle.job_id)

            async def update_progress(progress: DashboardRunProgress) -> None:
                async with self:
                    self.test_active_runs = _apply_run_progress(
                        self.test_active_runs,
                        scenario_id,
                        progress,
                    )

            outcome = await monitor_dashboard_run(handle, update_progress)
        except asyncio.CancelledError as exc:
            result_row = _run_terminal_row(
                scenario_id,
                name=run_name,
                status="cancelled",
                message="The test run was stopped before completion.",
            )
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                cancellation = exc
        except DashboardRunError:
            result_row = _run_terminal_row(
                scenario_id,
                name=run_name,
                status="failed",
                message="Plantain could not safely start or complete the selected test.",
            )
        else:
            ui_workflow_id = await _verified_ui_workflow_for_outcome(
                ui_workflow_id,
                outcome,
            )
            result_row = _run_outcome_row(
                outcome,
                database_workflow_id,
                ui_workflow_id,
            )
        finally:
            async with self:
                self.test_active_run_ids = tuple(
                    active_id for active_id in self.test_active_run_ids if active_id != scenario_id
                )
                self.test_active_run_count = len(self.test_active_run_ids)
                self.test_active_runs = tuple(
                    row for row in self.test_active_runs if row["scenario_id"] != scenario_id
                )
                if result_row is not None:
                    self.test_run_results = (
                        result_row,
                        *self.test_run_results[: MAX_SESSION_RUN_RESULTS - 1],
                    )
                    self.test_run_result_count = len(self.test_run_results)
        if cancellation is not None:
            raise cancellation

    @background_event
    async def continue_database_discovery(
        self,
        workflow_id: str,
        scenario_id: str,
        run_id: str,
    ) -> None:
        """Author the next reviewed step from one verified discovery run."""

        async with self:
            if not self._begin_database_continuation(workflow_id, scenario_id, run_id):
                return
        try:
            result = await author_dashboard_database_continuation(
                dashboard_project_root(),
                workflow_id,
                run_id,
            )
            page = store_database_authoring_draft(result)
        except (DashboardAgentError, DashboardDraftError):
            async with self:
                self.agent_error_message = (
                    "Plantain could not safely continue this database discovery."
                )
        else:
            async with self:
                self._finish_database_continuation(result, page, workflow_id)
                self.test_run_notice = "The next step is ready for review in Create."
        finally:
            async with self:
                self.agent_busy = False

    @background_event
    async def continue_ui_discovery(
        self,
        workflow_id: str,
        scenario_id: str,
        run_id: str,
    ) -> None:
        """Author the next reviewed UI step or one grounded locator repair."""

        async with self:
            if not self._begin_ui_continuation(workflow_id, scenario_id, run_id):
                return
        try:
            result = await author_dashboard_ui_continuation(
                dashboard_project_root(),
                workflow_id,
                run_id,
            )
            page = store_ui_authoring_draft(result)
        except (DashboardAgentError, DashboardDraftError):
            async with self:
                self.agent_error_message = "Plantain could not safely continue this UI discovery."
        else:
            async with self:
                self._finish_ui_continuation(result, page, workflow_id)
                self.test_run_notice = "The next UI step is ready for review in Create."
        finally:
            async with self:
                self.agent_busy = False

    def _begin_database_continuation(
        self,
        workflow_id: str,
        scenario_id: str,
        run_id: str,
    ) -> bool:
        owned = _owns_database_continuation(
            self.test_run_results,
            workflow_id,
            scenario_id,
            run_id,
        )
        current = (
            workflow_id == self.database_workflow_id
            and scenario_id == self.database_workflow_scenario_id
        )
        if self.agent_busy or self.draft_saving:
            return False
        if not owned or not current:
            self.test_run_notice = "That database continuation is no longer available."
            return False
        self._apply_agent_connection()
        if not self.agent_ready:
            self.agent_error_message = "Connect an agent in Settings before continuing."
            return False
        self.agent_busy = True
        self.agent_error_message = ""
        return True

    def _begin_ui_continuation(
        self,
        workflow_id: str,
        scenario_id: str,
        run_id: str,
    ) -> bool:
        owned = _owns_ui_continuation(
            self.test_run_results,
            workflow_id,
            scenario_id,
            run_id,
        )
        current = workflow_id == self.ui_workflow_id and scenario_id == self.ui_workflow_scenario_id
        if self.agent_busy or self.draft_saving:
            return False
        if not owned or not current:
            self.test_run_notice = "That UI continuation is no longer available."
            return False
        self._apply_agent_connection()
        if not self.agent_ready:
            self.agent_error_message = "Connect an agent in Settings before continuing."
            return False
        self.agent_busy = True
        self.agent_error_message = ""
        return True

    @rx.event
    def cancel_test(self, job_id: str) -> None:
        """Stop one active run owned by this browser session."""

        if not is_valid_dashboard_job_id(job_id):
            self.test_run_notice = "That run control is no longer available."
            return
        owned = any(row["job_id"] == job_id for row in self.test_active_runs)
        if not owned:
            self.test_run_notice = "That test is no longer active in this session."
            return
        if cancel_dashboard_run(job_id):
            self.test_run_notice = "Stopping the test safely and waiting for resource cleanup."
        else:
            self.test_run_notice = "That test is already finishing."

    async def _load_test_page(self, page_number: int) -> None:
        self.test_catalog_loading = True
        self.test_catalog_error = ""
        self.test_catalog_notice = ""
        try:
            catalog_filter = _test_catalog_filter(
                self.test_filter_query,
                self.test_filter_required_all,
                self.test_filter_required_any,
                self.test_filter_excluded,
            )
        except ScenarioCatalogError:
            self.test_catalog_error = "Review the current search and tag filters."
            self.test_catalog_loading = False
            return
        try:
            page = await asyncio.to_thread(
                load_scenario_catalog,
                dashboard_project_root(),
                page=page_number,
                catalog_filter=catalog_filter,
            )
        except ScenarioCatalogError:
            self._clear_test_catalog()
            self.test_catalog_error = "Plantain could not safely load the local test collection."
        else:
            self._apply_test_catalog(page)
            self.test_filters_active = catalog_filter.active
        finally:
            self.test_catalog_loading = False

    async def _load_test_detail(
        self,
        scenario_id: str,
        page_number: int,
    ) -> None:
        detail: ScenarioDetailPage | None = None
        error = ""
        try:
            detail = await asyncio.to_thread(
                load_scenario_detail,
                dashboard_project_root(),
                scenario_id,
                page=page_number,
            )
        except ScenarioDetailError:
            error = "Plantain could not safely load this test detail."
        async with self:
            if self.test_detail_scenario_id != scenario_id:
                return
            self.test_detail_loading = False
            if detail is None:
                self.test_detail_steps = ()
                self.test_detail_error = error
                return
            self._apply_test_detail(detail)

    def _apply_test_detail(self, detail: ScenarioDetailPage) -> None:
        self.test_detail_name = detail.name
        self.test_detail_source = detail.source
        self.test_detail_steps = tuple(_scenario_step_row(step) for step in detail.steps)
        self.test_detail_total_steps = detail.total_steps
        self.test_detail_page = detail.page
        self.test_detail_page_count = detail.page_count
        self.test_detail_has_previous = detail.has_previous
        self.test_detail_has_next = detail.has_next
        self.test_detail_notice = detail.notice
        self.test_detail_error = ""

    def _clear_test_detail(self) -> None:
        self.test_detail_open = False
        self.test_detail_loading = False
        self.test_detail_scenario_id = ""
        self.test_detail_name = ""
        self.test_detail_source = ""
        self.test_detail_steps = ()
        self.test_detail_total_steps = 0
        self.test_detail_page = 1
        self.test_detail_page_count = 0
        self.test_detail_has_previous = False
        self.test_detail_has_next = False
        self.test_detail_notice = ""
        self.test_detail_error = ""

    def _apply_test_catalog(self, page: ScenarioCatalogPage) -> None:
        self.test_catalog_items = tuple(_scenario_catalog_row(item) for item in page.items)
        self.test_catalog_total = page.total_count
        self.test_catalog_page = page.page
        self.test_catalog_page_count = page.page_count
        self.test_catalog_has_previous = page.has_previous
        self.test_catalog_has_next = page.has_next
        self.test_catalog_notice = page.notice

    def _clear_test_catalog(self) -> None:
        self.test_catalog_items = ()
        self.test_catalog_total = 0
        self.test_catalog_page = 1
        self.test_catalog_page_count = 0
        self.test_catalog_has_previous = False
        self.test_catalog_has_next = False
        self.test_catalog_notice = ""

    def _set_test_selection(self, selected_ids: tuple[str, ...]) -> None:
        self.test_selected_ids = selected_ids
        self.test_selected_count = len(selected_ids)

    def _current_test_catalog_filter(self) -> ScenarioCatalogFilter:
        return _test_catalog_filter(
            self.test_filter_query,
            self.test_filter_required_all,
            self.test_filter_required_any,
            self.test_filter_excluded,
        )

    def _begin_test_batch(self) -> _TestBatchRequest | None:
        if self.test_batch_active:
            self.test_run_notice = "A test batch is already active."
            return None
        if self.test_catalog_loading or self.test_validation_loading:
            self.test_run_notice = "Wait for the current catalog operation to finish."
            return None
        try:
            catalog_filter = self._current_test_catalog_filter()
        except ScenarioCatalogError:
            self.test_run_notice = "Review the current search and tag filters."
            return None
        batch_id = secrets.token_hex(BATCH_ID_BYTES)
        self.test_batch_id = batch_id
        self.test_batch_active = True
        self.test_batch_total = 0
        self.test_batch_completed = 0
        self.test_batch_stop_requested = False
        self.test_run_notice = "Preparing the bounded test batch."
        return (
            batch_id,
            catalog_filter,
            self.test_selected_ids,
            frozenset(self.test_active_run_ids),
        )

    async def _prepare_test_batch(
        self,
        request: _TestBatchRequest,
    ) -> _PreparedTestBatch:
        batch_id, catalog_filter, selected_ids, active_ids = request
        root = dashboard_project_root()
        selection = await asyncio.to_thread(
            load_scenario_selection,
            root,
            catalog_filter=catalog_filter,
        )
        items, issue = _batch_run_items(selection, selected_ids, active_ids)
        if issue:
            return (), 0, issue
        capacity = await asyncio.to_thread(dashboard_run_capacity, root)
        async with self:
            if self.test_batch_id != batch_id or self.test_batch_stop_requested:
                return (), 0, "The batch was stopped before any tests started."
            self.test_batch_total = len(items)
            self.test_run_notice = (
                f"Running {len(items)} tests with up to {capacity} active at once."
            )
        return items, capacity, ""

    async def _run_test_batch_workers(
        self,
        batch_id: str,
        items: tuple[ScenarioCatalogItem, ...],
        capacity: int,
    ) -> None:
        pending = iter(items)

        async def worker() -> None:
            while (item := await self._next_test_batch_item(batch_id, pending)) is not None:
                await self._execute_test_run(
                    item.scenario_id,
                    item.name,
                    batch_id=batch_id,
                )
                async with self:
                    if self.test_batch_id == batch_id:
                        self.test_batch_completed += 1

        worker_count = min(capacity, len(items))
        await asyncio.gather(*(worker() for _worker in range(worker_count)))

    async def _next_test_batch_item(
        self,
        batch_id: str,
        pending: Iterator[ScenarioCatalogItem],
    ) -> ScenarioCatalogItem | None:
        async with self:
            if self.test_batch_id != batch_id or self.test_batch_stop_requested:
                return None
            return next(pending, None)

    def _finish_test_batch(self, batch_id: str, terminal_notice: str) -> None:
        if self.test_batch_id != batch_id:
            return
        if terminal_notice:
            notice = terminal_notice
        else:
            notice = _batch_completion_notice(
                self.test_batch_completed,
                self.test_batch_total,
                stopped=self.test_batch_stop_requested,
            )
        self.test_batch_id = ""
        self.test_batch_active = False
        self.test_batch_stop_requested = False
        self.test_run_notice = notice


def _toggle_test_selection(
    selected_ids: tuple[str, ...],
    scenario_id: str,
) -> tuple[str, ...]:
    if scenario_id in selected_ids:
        return tuple(item for item in selected_ids if item != scenario_id)
    return (*selected_ids, scenario_id)


def _merge_test_selection(
    selected_ids: tuple[str, ...],
    additional_ids: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*selected_ids, *additional_ids)))


def _batch_run_items(
    selection: ScenarioCatalogSelection,
    selected_ids: tuple[str, ...],
    active_ids: frozenset[str],
) -> tuple[tuple[ScenarioCatalogItem, ...], str]:
    selected = frozenset(selected_ids)
    items = tuple(item for item in selection.items if not selected or item.scenario_id in selected)
    if selected and len(items) != len(selected):
        return (), "The selection changed. Review the visible tests and select them again."
    if not items:
        return (), "No tests match the current filters."
    active_count = sum(item.scenario_id in active_ids for item in items)
    if active_count:
        return (), (
            f"{active_count} matching tests are already active. "
            "Wait for them to finish or stop them before starting the batch."
        )
    review_count = sum(item.status != "ready" for item in items)
    if review_count:
        return (), (f"{review_count} matching tests need review. No tests were started.")
    return items, ""


def _batch_completion_notice(
    completed: int,
    total: int,
    *,
    stopped: bool,
) -> str:
    if stopped:
        return f"Batch stopped after {completed} of {total} tests finished."
    return f"Batch complete: {completed} of {total} tests finished."


def _selection_validation_notice(
    selection: ScenarioCatalogSelection,
    selected_ids: tuple[str, ...],
) -> str:
    selected = frozenset(selected_ids)
    items = tuple(item for item in selection.items if not selected or item.scenario_id in selected)
    if selected and len(items) != len(selected):
        return "The selection changed. Review the visible tests and select them again."
    if not items:
        return "No tests match the current filters."
    ready_count = sum(item.status == "ready" for item in items)
    review_count = len(items) - ready_count
    scope = "selected" if selected else "matching"
    noun = "test" if len(items) == 1 else "tests"
    return (
        f"Validated {len(items)} {scope} {noun}: {ready_count} ready, {review_count} need review."
    )


def _test_catalog_filter(
    query: str,
    required_all: str,
    required_any: str,
    excluded: str,
) -> ScenarioCatalogFilter:
    values = (query, required_all, required_any, excluded)
    if any(len(value) > MAX_TEST_FILTER_INPUT_LENGTH for value in values):
        raise ScenarioCatalogError("The test filters are too long")
    return ScenarioCatalogFilter.create(
        query=query,
        required_all=_test_filter_values(required_all),
        required_any=_test_filter_values(required_any),
        excluded=_test_filter_values(excluded),
    )


def _test_filter_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace("\n", ",").split(",") if item.strip())


def _scenario_catalog_row(item: ScenarioCatalogItem) -> dict[str, str]:
    tags = " · ".join(item.tags)
    if item.additional_tag_count:
        suffix = f"+{item.additional_tag_count} more"
        tags = f"{tags} · {suffix}" if tags else suffix
    step_label = f"{item.step_count} {'step' if item.step_count == 1 else 'steps'}"
    return {
        "scenario_id": item.scenario_id,
        "name": item.name,
        "source": item.source,
        "status": item.status,
        "status_label": "Validated" if item.status == "ready" else "Needs review",
        "steps": step_label,
        "domains": " + ".join(item.domains) if item.domains else "Unclassified",
        "tags": tags or "No tags",
        "issue": item.issue,
    }


def _scenario_step_row(item: ScenarioStepDetail) -> dict[str, str]:
    return {
        "position": str(item.position),
        "activity": item.activity,
        "step_id": item.step_id,
        "yaml": item.yaml_text,
        "limited": "true" if item.content_limited else "false",
    }


def _activate_run(
    active_ids: tuple[str, ...],
    scenario_id: str,
) -> tuple[str, ...]:
    if scenario_id in active_ids:
        return active_ids
    return (*active_ids, scenario_id)


def _catalog_run_name(
    items: tuple[dict[str, str], ...],
    scenario_id: str,
    requested_name: str,
) -> str:
    for item in items:
        if item["scenario_id"] == scenario_id:
            return item["name"]
    if len(requested_name) <= MAX_PRESENTATION_RUN_NAME_LENGTH:
        rendered = " ".join(requested_name.split())
        if rendered:
            return rendered
    return "Selected test"


def _active_run_row(
    scenario_id: str,
    name: str,
    database_workflow_id: str,
    ui_workflow_id: str = "",
    batch_id: str = "",
) -> dict[str, str]:
    return {
        "scenario_id": scenario_id,
        "job_id": "",
        "batch_id": batch_id,
        "database_workflow_id": database_workflow_id,
        "ui_workflow_id": ui_workflow_id,
        "name": name,
        "status": "queued",
        "status_label": "Queued",
        "message": "Waiting for available runtime capacity.",
        "phase": "Preparing test",
        "progress_width": "0%",
        "progress_label": "Preparing step plan",
        "activity": "",
        "step_id": "",
        "latest_operation": "",
        "timeline": "• Waiting for available runtime capacity.",
        "elapsed": "0 ms",
        "correlation_id": "",
    }


def _assign_run_job(
    rows: tuple[dict[str, str], ...],
    scenario_id: str,
    job_id: str,
) -> tuple[dict[str, str], ...]:
    return tuple(
        {**row, "job_id": job_id} if row["scenario_id"] == scenario_id else row for row in rows
    )


def _apply_run_progress(
    rows: tuple[dict[str, str], ...],
    scenario_id: str,
    progress: DashboardRunProgress,
) -> tuple[dict[str, str], ...]:
    step_label = (
        f"{progress.completed_steps} of {progress.total_steps} steps"
        if progress.total_steps
        else "Preparing step plan"
    )
    message = "Plantain is executing the selected test."
    if progress.latest_operation:
        message = f"Latest evidence: {progress.latest_operation}"
    elif progress.activity:
        message = f"Executing {progress.activity}."
    updates = {
        "status": progress.status,
        "status_label": progress.status.title(),
        "message": message,
        "phase": progress.phase_label,
        "progress_percent": str(progress.progress_percent),
        "progress_width": f"{progress.progress_percent}%",
        "progress_label": step_label,
        "activity": progress.activity,
        "step_id": progress.step_id,
        "latest_operation": progress.latest_operation,
        "timeline": "\n".join(f"• {item}" for item in progress.timeline),
        "elapsed": _run_duration_label(progress.elapsed_ms),
        "correlation_id": progress.correlation_id,
    }
    return tuple({**row, **updates} if row["scenario_id"] == scenario_id else row for row in rows)


def _run_outcome_row(
    outcome: DashboardRunOutcome,
    database_workflow_id: str,
    ui_workflow_id: str = "",
) -> dict[str, str]:
    return {
        "scenario_id": outcome.scenario_id,
        "database_workflow_id": (database_workflow_id if outcome.status == "passed" else ""),
        "ui_workflow_id": ui_workflow_id,
        "name": outcome.name,
        "status": outcome.status,
        "status_label": "Passed" if outcome.status == "passed" else "Failed",
        "message": outcome.message,
        "duration": _run_duration_label(outcome.duration_ms),
        "steps": _run_step_label(outcome.completed_steps),
        "correlation_id": outcome.correlation_id,
    }


def _run_terminal_row(
    scenario_id: str,
    *,
    name: str,
    status: Literal["failed", "cancelled"],
    message: str,
) -> dict[str, str]:
    return {
        "scenario_id": scenario_id,
        "database_workflow_id": "",
        "ui_workflow_id": "",
        "name": name,
        "status": status,
        "status_label": "Failed" if status == "failed" else "Stopped",
        "message": message,
        "duration": "",
        "steps": "",
        "correlation_id": "",
    }


def _database_workflow_for_run(
    workflow_id: str,
    bound_scenario_id: str,
    scenario_id: str,
) -> str:
    if is_valid_database_workflow_id(workflow_id) and bound_scenario_id == scenario_id:
        return workflow_id
    return ""


def _ui_workflow_for_run(
    workflow_id: str,
    bound_scenario_id: str,
    scenario_id: str,
) -> str:
    if is_valid_ui_workflow_id(workflow_id) and bound_scenario_id == scenario_id:
        return workflow_id
    return ""


def _owns_database_continuation(
    rows: tuple[dict[str, str], ...],
    workflow_id: str,
    scenario_id: str,
    run_id: str,
) -> bool:
    if not is_valid_database_workflow_id(workflow_id) or not run_id:
        return False
    return any(
        row["status"] == "passed"
        and row["database_workflow_id"] == workflow_id
        and row["scenario_id"] == scenario_id
        and row["correlation_id"] == run_id
        for row in rows
    )


def _owns_ui_continuation(
    rows: tuple[dict[str, str], ...],
    workflow_id: str,
    scenario_id: str,
    run_id: str,
) -> bool:
    if not is_valid_ui_workflow_id(workflow_id) or not run_id:
        return False
    return any(
        row["status"] in {"passed", "failed"}
        and row["ui_workflow_id"] == workflow_id
        and row["scenario_id"] == scenario_id
        and row["correlation_id"] == run_id
        for row in rows
    )


async def _verified_ui_workflow_for_outcome(
    workflow_id: str,
    outcome: DashboardRunOutcome,
) -> str:
    if not is_valid_ui_workflow_id(workflow_id) or not outcome.correlation_id:
        return ""
    try:
        await asyncio.to_thread(
            load_ui_run_evidence,
            dashboard_project_root(),
            outcome.correlation_id,
        )
    except UiEvidenceError:
        return ""
    return workflow_id


def _run_duration_label(value: int) -> str:
    if value < MILLISECONDS_PER_SECOND:
        return f"{value} ms"
    return f"{value / MILLISECONDS_PER_SECOND:.2f} s"


def _run_step_label(value: int) -> str:
    return f"{value} {'step' if value == 1 else 'steps'}"


__all__ = ["ScenarioCatalogState"]
