# Dashboard reference

The dashboard organizes the complete user workflow into five pages. Each page has
one primary purpose; detailed context opens in drawers or adjacent panels only when
requested.

## Page map

| Page | Route | Primary purpose |
| --- | --- | --- |
| Overview | `/` | Understand workspace health and recent activity |
| Create | `/create` | Describe intent and work with the agent |
| Tests | `/tests` | Find, inspect, validate, select, and run tests |
| Runs and Results | `/runs` | Review immutable executions and evidence |
| Settings | `/settings` | Configure the agent, results, and appearance |

## Shared navigation

The top bar shows the Plantain brand, current page controls, and appearance action.

On desktop:

- the navigation rail remains full-height;
- it can collapse to icons to create more workspace room;
- the active page remains visually identified.

On mobile and tablet:

- the desktop rail is hidden;
- the menu button opens one full-width navigation drawer;
- closing or choosing a destination returns focus to the active page;
- page content should not require horizontal scrolling.

## Overview

Use **Overview** as a decision surface, not a result archive.

It summarizes:

- test catalog health;
- recent execution activity;
- result trends;
- quality indicators;
- bounded agent-call and usage information.

Cards and charts derive from the current local workspace projection. Open **Tests**
or **Runs and Results** when you need record-level detail.

## Create

**Create** behaves like an agent workbench:

- live planning, discovery, creation, and execution occupy the primary area;
- the intent composer stays at the bottom;
- **Add context** opens optional context without permanently reducing workspace
  room;
- plan and draft review appear when a user decision is needed;
- accepted creation proceeds to validated persistence and execution.

The page preserves workflow identity across clarifications and grounded
continuations. Starting a different goal creates a different workflow.

## Tests

**Tests** behaves like a modern data table:

- search and tags define the current matching scope;
- row metadata summarizes readiness, type, step count, and tags;
- **Run test** is the primary single-row action;
- **View steps** opens a bounded detail drawer;
- **Select tests** reveals batch-selection controls only when needed;
- selected rows use a circular control and yellow outline;
- validation and execution actions follow matching or selected scope.

See [Manage tests](../testing/manage-tests.md) for the complete interaction model.

## Runs and Results

The result page separates three levels:

1. a searchable, paginated history;
2. a selected result summary;
3. an on-demand evidence drawer.

The summary loads bounded metadata. **Inspect evidence** separately opens
**Operations**, **Artifacts**, and **Failure / info** so large evidence does not
shift or crowd the main result layout.

## Settings

**Settings** contains:

- agent provider, model, endpoint, and credential source;
- default local result behavior;
- optional Allure and Zephyr result subsections;
- appearance control.

Network policy and application environment values are not general dashboard
settings. Configure them in the launch environment; supported agent and integration
credentials may use process-lifetime session profiles.

## Loading, empty, and error states

Dashboard collections distinguish:

- loading;
- no local data yet;
- no matches for the current filter;
- a safe recoverable error;
- a bounded-display notice.

Refresh actions reload the current projection. They do not mutate test artifacts
or existing results.

## Drawers and progressive detail

Plantain uses drawers for information that supports a decision but should not
occupy the page continuously:

- optional creation context;
- generated test steps;
- run evidence;
- focused operation details.

Closing a drawer preserves the underlying page context. Drawer content is bounded,
sanitized, and loaded only when needed.

## Local runtime boundary

The supported launcher binds the dashboard to one loopback port and stages writable
runtime under `output/dashboard/`. The browser receives safe display models rather
than backend settings objects, credentials, or unrestricted filesystem paths.

See [Use the dashboard](../get-started/dashboard.md) for launch instructions.
