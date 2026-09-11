# `capturePageSnapshot` authoring contract

`capturePageSnapshot` is the only built-in UI activity. It navigates, acts, verifies, and captures
while delegating browser, locator, action, assertion, extraction, and registry work to isolated
internal services.

## Step parameters

Every scenario step requires a unique literal `id`. The activity accepts only:

- `id`: identifier used by `${step...}` chaining;
- `activity`: requested verified snapshot-registry alias, defaulting to the step ID;
- `url`: optional initial HTTP(S) destination, usually an `env:` reference;
- `actions`: ordered browser instructions;
- `verify`: hard assertions evaluated after the final capture is registered;
- `snapshot`: capture options;
- `timeoutMs`: 100 through 300000 milliseconds;
- `waitUntil`: `commit`, `domcontentloaded`, `load`, or `networkidle`.

At least one of `url`, `actions`, or `verify` is required. Only the first step that begins a browser
session normally needs `url`; later steps reuse the same scenario-isolated active page.

## Browser preparation

Do not ask the tester to install a browser before the first UI run. The runtime lazily checks the
configured Playwright binary and installs only that pinned browser when it is absent. Preparation
runs once per scenario before per-step time limits and is lock-protected across concurrent runs.
API and database scenarios do not trigger it.

If automatic provisioning fails because the host lacks operating-system libraries or cannot reach
its approved artifact source, stop with the exact safe framework error and ask a human to review
the documented installation boundary. Never attempt sudo, `install-deps`, or an OS package manager
from the skill.

## Locators

A target requires exactly one primary strategy:

- `role`, optionally with `name`;
- `label`;
- `placeholder`;
- `text`;
- `altText`;
- `title`;
- `testId`;
- `css`;
- `xpath`.

`name` is valid only with `role`. Optional refinements are `exact` (default `true`), zero-based
`nth`, `visible` (default `true`), and `frames`. Prefer role/name, label, placeholder, test ID,
stable visible text/title, CSS, and finally XPath.

Never invent a locator. Use a selector recorded in the current verified canonical snapshot. For
the immediate failed iteration only, an exact diagnostic named by the safe failure may ground a
repair when no verified state supports the observed page. Use `nth` only when the intended repeated
occurrence is explicit and stable.

For nested frames, add an ordered `frames` chain to the target. Each frame entry requires exactly
one of `name`, exact `url`, or `css`:

```yaml
target:
  role: button
  name: Continue
  frames:
    - url: "https://app.example.test/embedded/form"
```

Do not guess a frame identity or rely on traversal order.

## Actions

Use ordered one-key action mappings:

```yaml
actions:
  - fill:
      target:
        label: Username
      value: "env:APP_USERNAME"
  - click:
      target:
        role: button
        name: Sign in
  - waitForUrl:
      value: "**/dashboard"
```

Supported actions are:

- Element actions: `click`, `fill`, `clear`, `type`, `press`, `select`, `check`, `uncheck`,
  `hover`, `focus`, `scrollIntoView`, and `waitFor`.
- Page actions: `waitForUrl`, `reload`, `goBack`, and `goForward`.

Element actions require `target`. `fill` and `type` require `value`. `press` requires `key`
(legacy `value` is also accepted). `waitForUrl` requires `value`. A `select` requires one or more
`choices`, and each choice has exactly one of `value`, `label`, or zero-based `index`:

```yaml
- select:
    target:
      label: Sort products
    choices:
      - label: "Price (low to high)"
```

Common optional fields are `timeoutMs`, `delayMs`, and `force`. `waitFor` also accepts `state`:
`attached`, `detached`, `visible`, or `hidden`.

When a click is expected to open a new window or tab, set `expectPopup: true` on that click.
Subsequent actions automatically use the new active page. `expectPopup` is invalid on every other
action.

Any action may optionally prove that it caused a known response:

```yaml
- click:
    target:
      role: button
      name: Submit
    expectResponse:
      url: "**/api/orders"
      method: POST
      status: [200, 201]
      timeoutMs: 15000
```

`expectResponse` requires a case-sensitive full-URL glob, method, and one status or non-empty status
list; its timeout defaults to the action or activity timeout. Use it only when user intent or
grounded application/API evidence establishes the causal response—never infer it merely because
passive traffic exists. The runtime arms the waiter before the action, removes it on every outcome,
and retains only bounded sanitized URL, method, and status evidence, never bodies.

## Verifications

Supported assertions are `visible`, `hidden`, `enabled`, `disabled`, `editable`, `checked`,
`unchecked`, `text`, `value`, `count`, `attribute`, `url`, and `title`.

All element assertions require `target`. `url` and `title` are page assertions and do not accept a
target. `text` and `value` require `equals` or `contains`; `count` requires an integer `equals`;
`attribute` requires both `attribute` and `equals`; `url` and `title` require `equals` or
`contains`.

```yaml
verify:
  - url:
      contains: "inventory.html"
  - visible:
      target:
        role: heading
        name: Products
  - count:
      target:
        css: ".inventory_item"
      equals: 6
```

Each assertion optionally accepts `timeoutMs`. The runtime registers the final state as diagnostic
before assertions run. Passing every assertion atomically promotes it to verified; failure retains
the exact diagnostic without assigning the requested activity alias.

## Snapshot behavior and outputs

Snapshots default to enabled:

```yaml
snapshot:
  enabled: true
  captureAfterEachAction: false
```

Use `captureAfterEachAction: true` only when intermediate action states materially matter. The
runtime then registers verified intermediate evidence after every successful action except the
last. A final capture with assertions remains diagnostic until promotion; a failed action attempts
one final diagnostic capture before returning the original safe action error. Separate state steps
are normally clearer.

Capture preserves the complete discoverable semantic layout, including nested frames, in bounded
content-addressed chunks without a total element, frame, or network-event count cap. Snapshot
artifacts remain local and are ignored by Git.

The step result supports chaining through:

- `success`;
- `url`;
- `pageTitle`;
- `actionsExecuted`;
- `verificationsPassed`;
- `snapshots[].activity`, `canonicalFile`, `status`, `evidenceState`, and `failureStage`.

Example:

```yaml
outputs:
  inventoryUrl: "${capture_inventory.url}"
  canonicalSnapshot: "${capture_inventory.snapshots[0].canonicalFile}"
```

Registry v4 stores structural states in a top-level `entries` array. Each entry stores verified
aliases under `activities` and failed intent under `diagnostics`. Diagnostics resolve only through
their exact filename. Before using locators, require registry schema 4.0, complete manifest schema
3.0, matching entry metadata, contained non-symlink paths, exact byte counts, and matching SHA-256
digests. Safe failures may expose `diagnostic_snapshot`, `evidence_state`, or a bounded
`diagnostic_status` when capture or registration was unavailable.

The runtime—not the skill—owns staging, canonical naming, deduplication, history, deprecated
redirects, atomic commits, and promotion. Never hand-edit, manually promote, rename, or delete
registry or snapshot artifacts.
