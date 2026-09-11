# YAML Contract Specialist

<!--
AGENT METADATA
- Role: Pydantic and User-Facing YAML Contract Specialist
- Last Updated: 2026-09-02
-->

Read the root instructions first.

## Non-obvious contracts

- YAML is the maintained public interface. Models reject unknown keys and ambiguous shorthand so a
  typo cannot silently change behavior.
- Keep authored lower-camel aliases and serialized output names stable. Internal snake_case is not
  a reason to break existing YAML.
- `from_yaml` methods normalize supported concise forms into one typed representation. Add syntax
  only when it reduces user burden without creating two meanings for the same mapping.
- Environment/context expressions remain strings during static loading. Validators must still
  reject malformed structure while deferring only checks that genuinely need the resolved value.
- Do not add BDD descriptions, screen names, flow names, DOM-specific classes, API builders, or
  database query builders to models.
- UI actions and assertions are separate discriminated contracts. A step may navigate, act,
  verify, or combine them, but must contain meaningful work.
- Do not duplicate arbitrary count caps on UI actions, assertions, or recorded frame paths; YAML
  document budgets and the scenario deadline own those resource boundaries.
- Scenario models retain at most 1,000 steps, 1,000 top-level parameters per step, and 100 metadata
  entries. These practical ceilings protect shared runs without adding author-facing settings.
- Database discovery phases constrain which fields are legal. Query result shape is explicitly
  scalar, row, or rows; preserve that distinction in results and verification.
- API expected statuses accept the documented scalar/range forms. Request and response bodies
  remain JSON-compatible values and are not implicitly coerced into generated models.
- Scenario metadata remains optional unless enabled reporting imposes runtime validation. Preserve
  user-provided `JiraTicket`, `testCaseKey`, and `testRunKey`; never fabricate identifiers.

## Fast feedback

```bash
uv run --locked --extra dev ruff check \
  src/plantain/models \
  tests/unit/test_ui_models.py \
  tests/unit/test_scenario_selection.py \
  tests/unit/activities/test_api_activities.py \
  tests/unit/activities/test_database_activities.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/test_ui_models.py \
  tests/unit/test_scenario_selection.py \
  tests/unit/activities/test_api_activities.py \
  tests/unit/activities/test_database_activities.py
```

When a contract changes, update its activity YAML and at least one runnable scenario in the same
change.
