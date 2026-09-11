# UI testing

Plantain creates UI tests from the behavior you describe and the interface it can
actually observe. You focus on the user journey and expected outcome; Plantain
handles browser discovery, grounded interaction, verification, and evidence.

## What you provide

Tell Plantain:

- the journey or behavior that matters;
- the approved starting URL or its environment reference;
- the user role or starting state when relevant;
- the visible outcome that proves success;
- the names of any environment references needed at runtime.

For example:

> Using `SAUCE_DEMO_URL` and the standard-user environment references, confirm the
> customer can sign in, add the backpack to the cart, complete checkout, and see
> the order confirmation.

Do not supply CSS selectors, XPath expressions, browser scripts, or copied page
source. Plantain must ground interactions in current semantic evidence.

## What Plantain does

### 1. Opens the approved page

For a new UI workflow, Plantain begins with discovery. It opens the approved URL
without assuming that a particular button, field, or flow exists.

### 2. Observes the semantic interface

Plantain records a complete semantic representation of the page and reachable
frames. This evidence captures meaningful roles, labels, names, states, and page
structure rather than relying only on visual position.

### 3. Plans a grounded journey

The agent uses observed controls and your intended outcome to propose the smallest
useful path. It does not invent controls that are absent from the evidence.

### 4. Creates and validates the test

Plantain translates the approved plan into a reviewable scenario, checks it against
the UI activity contract, and preserves environment references for protected
values.

### 5. Runs and verifies the result

The browser journey runs in an isolated context. Plantain performs the supported
interactions, checks the requested state, and retains sanitized semantic evidence
for the result.

## What you see

In **Create**, the live agent workspace shows whether Plantain is clarifying,
planning, discovering, creating, validating, or running.

After execution, **Runs and Results** shows:

- whether the journey passed or failed;
- the completed step count and duration;
- the last safe execution message;
- step-level detail;
- available semantic evidence;
- a focused repair path when a grounded locator fails.

Open evidence only when you need to investigate. The default result stays concise.

## What to review

Before accepting the generated test, confirm:

- it starts at the intended page and user state;
- the main actions match how a user completes the task;
- the final verification proves the outcome;
- it does not include unrelated browsing;
- account values are environment references;
- the proposed name clearly distinguishes the behavior in the test catalog.

You are reviewing user meaning. Plantain is responsible for the underlying
automation details.

## Continuing an existing journey

You can extend a previously verified UI path:

> Preserve the approved sign-in journey, then add the checkout steps and verify
> the completion message.

Plantain binds the continuation to the exact prior workflow and evidence. It keeps
the verified action prefix and appends only behavior grounded in the next observed
state.

## Focused repair

When a run fails because a grounded UI target changed, Plantain can use diagnostic
evidence from that exact run to propose a locator repair.

The repair is deliberately narrow: it may update the failed target, but it does not
silently change the surrounding action, expected behavior, or unrelated steps.
Review and rerun the repaired test as a new execution.

## Browser availability

Chromium is the default. If supported auto-installation is enabled, Plantain can
provision only the missing selected browser at first use. You can also install it
beforehand:

```bash
uv run --locked python -m playwright install chromium
```

Firefox and WebKit can be selected through the documented runtime environment.
Plantain does not install privileged operating-system packages automatically.

## When Plantain pauses

Plantain stops for clarification or correction when:

- no approved starting URL is available;
- the target is blocked by URL, DNS, or network policy;
- a requested control is not supported by current evidence;
- an interaction is ambiguous;
- a secret value was supplied as literal text;
- the page or evidence changed before continuation;
- a popup, response expectation, frame, or download cannot be handled safely.

The safe response is to correct the intent, environment, or evidence—not to ask the
agent to guess.

## UI evidence and privacy

Semantic snapshots are private AI evidence. Runtime transactions own their
registry and artifact updates, and browser tracing stays disabled by default
because trace archives can retain sensitive page data.

Only bounded, sanitized context selected for the active task is available to an
external agent provider. Review [Security and privacy](../operations/security.md)
before using sensitive environments.

Next, learn how to [manage generated tests](manage-tests.md) or
[review results](results.md).
