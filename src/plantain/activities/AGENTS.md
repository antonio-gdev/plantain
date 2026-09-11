# Activity and UI Discovery Specialist

<!--
AGENT METADATA
- Role: Activity and UI Discovery Specialist
- Last Updated: 2026-08-31
-->

Read the root instructions first. Nested API and database directories add stricter overrides.

## Non-obvious contracts

- The public surface is fixed at eight framework-owned activities. UI has exactly one:
  `capturePageSnapshot`. Do not introduce screens, flows, target enums, selector modules, or
  application-named activities.
- UI YAML contains actions and assertions, not logging descriptions. Lifecycle messages are
  derived from verb, sanitized target, and bounded sanitized input/expected/actual evidence.
- One scenario reuses one isolated browser context and active page across UI steps. A popup may
  become the active page; do not create a new session per step.
- Preserve execution order: navigation, actions, captures, and one atomic registration batch.
  Assertion-bound final evidence registers as diagnostic, assertions run, and success atomically
  promotes it; an action failure may retain one diagnostic before the original error returns.
- Locator resolution must be strict and complete. Prefer recorded role/name, label, placeholder,
  test ID, text, then exact recorded CSS candidates. Use Playwright-native visibility filtering
  across the complete match set; ambiguity or no visible match fails, and locators are never
  guessed.
- The `testId` locator means Playwright's configured test-id attribute. It is not an alias for
  arbitrary attributes such as SauceDemo's `data-test`.
- Browser routes and service-worker blocking must exist before the first page. Apply HTTP policy to
  navigation, redirects, popups, and requests; apply WebSocket policy before forwarding a socket.
- Lazy browser installation is browser-specific, shell-free, lock-protected, and receives only an
  allowlisted child environment. Never run `install-deps`, sudo, or a package manager.
- Browser tracing stays disabled by default because trace archives can retain page data, tokens,
  and credentials.
- Optional per-action response expectations must arm on the isolated browser context before the
  action, match explicit URL/method/status intent, retain no bodies, and remove their listener on
  every outcome. Actions without an expectation must not pay a listener or configuration cost.
- Synchronous registry/filesystem work uses shared worker-thread admission. Snapshot cancellation
  must signal the registry transaction and wait for rollback plus private-stage cleanup before
  returning; never launch untracked `asyncio.to_thread` work.

## Semantic snapshot protocol

Semantic accessibility records and the complete discoverable layout are private AI evidence.
`snapshots/` stays ignored by Git and is never published.

1. Read `snapshots/registry.json` before relying on an existing snapshot.
2. Run a fresh capture when discovery is requested; an existing entry never replaces live
   discovery.
3. Let `capturePageSnapshot` and the registry transaction own staging, frozen-baseline comparison,
   deduplication, structural-state registration, aliases, canonical naming, history, deprecated
   redirects, rollback, digest verification, promotion, and cleanup.
   Keep its internal navigation, action/capture, registration, verification, promotion, and result
   lifecycle units focused; do not expose them as activities or reorder their policy checks.
4. Registry v4 stores structural states in its top-level `entries` array. Each entry keeps verified
   aliases in `activities` and failed-intent records in `diagnostics`; diagnostics resolve only by
   exact filename and never through an activity alias.
5. Before locator use, require registry schema 4.0 and manifest schema 3.0, verified evidence,
   matching registry/manifest metadata, contained non-symlink paths, exact byte counts, and matching
   SHA-256 digests.
6. Never hand-edit the registry, map diagnostics to aliases, rename files, or delete artifacts.
7. For generation-only work, resolve activity/deprecated aliases to the verified manifest and
   apply the same complete integrity verification before using its locators.
8. When the latest safe failure names an exact diagnostic and no verified state supports the
   intended page, verify that diagnostic and use it only to repair and rerun the failed iteration.
9. Otherwise request fresh discovery. Never treat a diagnostic as canonical or invent a locator.

Capture has no shared element limit, frame cap, or overflow-dropping behavior. DOM/accessibility
and network evidence stream through byte-bounded working batches into content-addressed,
hash-verifiable chunks. A DOM mutation or page/frame topology change aborts its atomic attempt;
the activity may retry the complete capture at most three times while rechecking browser policy
between attempts. Exhausted consistency retries, working-set or storage-envelope exhaustion, and
malformed cursors abort the operation and register nothing; never add scenario delays merely to
compensate for capture internals or accept truncation.
Complete DOM captured after an action or verification failure may be retained as diagnostic
evidence, but it becomes canonical only after assertions pass and atomic promotion commits.
Network lifecycle telemetry uses per-capture deltas and bounded unfinished-request correlation, and
never captures request or response bodies.

## Fast feedback

After an activity/UI/snapshot change, run:

```bash
uv run --locked --extra dev ruff check \
  src/plantain/activities/ui_*.py \
  src/plantain/activities/snapshot_*.py \
  tests/unit/activities/test_ui_*.py \
  tests/unit/activities/test_snapshot_*.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/activities/test_ui_*.py \
  tests/unit/activities/test_snapshot_*.py \
  tests/unit/test_ui_models.py \
  tests/unit/test_browser_configuration.py
```

Run a live browser scenario only with a human-approved target and environment references. Standard
mode safely discovers public HTTPS/WSS dependencies without a host map; restricted or private
targets require their exact allow rules. A successful unit suite does not prove host browser
libraries are installed.
