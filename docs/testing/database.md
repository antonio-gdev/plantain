# Database testing

Plantain turns a business question about stored data into a bounded, read-only
test. You describe the fact that should be true; Plantain discovers the approved
database structure, issues one safe query, and verifies the returned evidence.

Agent-driven database work can never modify data.

## What you provide

Tell Plantain:

- which approved database environment to use;
- the business fact you want to verify;
- a table, schema, or domain hint when you know it;
- the fields needed to prove the result;
- any filter values by environment-reference name;
- whether you expect one value, one row, or a bounded set of rows.

For example:

> Using the PostgreSQL source identified by `CLOTHING_DATABASE_URL`, verify that
> the most recently created UNO item exists and return only the item identifier,
> name, and creation time.

You do not need to know the database driver, system catalog, SQL dialect, or final
query.

## Supported databases

Plantain supports:

- PostgreSQL;
- SQL Server;
- MySQL;
- Oracle.

The same user workflow applies to each database. Plantain selects the supported
driver and dialect from the approved source configuration.

## What Plantain does

### 1. Validates the destination

Plantain resolves the database source at execution time and applies its separate
database `host:port` allowlist, fresh DNS validation, TLS requirements, and
connection limits.

### 2. Discovers structure without reading application rows

Plantain discovers schemas and tables, then reflects the metadata for the relevant
table. This stage learns names, columns, types, and relationships; it does not scan
business data.

### 3. Builds one bounded read-only query

Using the discovered metadata and your intent, Plantain proposes one provably
read-only `SELECT` with exact named parameters. It selects only the fields needed
for the test and applies configured row and value limits.

### 4. Verifies the evidence

Plantain can verify an expected value, a row, or a bounded row collection. The
generated test keeps the question, query, and verification together so the result
is explainable.

### 5. Returns the connection safely

Database sessions and leases are isolated per scenario. Plantain rolls back on
return and closes shared pools at their owning runtime boundary.

## What you see

During creation, **Create** distinguishes metadata discovery from the later
read-only query. You can review the discovered table context before accepting the
proposed test.

In **Tests**, select **View steps** to interpret the generated flow. A typical
database test shows:

1. a discovery step that identifies safe metadata;
2. a query step that retrieves bounded evidence;
3. a verification step that decides whether the business fact is true.

Each step is one agent-created action or check. The step list helps you audit what
Plantain will do; it is not an instruction to write SQL or edit activity fields.

After execution, **Runs and Results** shows status, duration, completed steps, and
sanitized evidence appropriate to the verification.

## What to review

Confirm that:

- the source points to the intended environment;
- the discovered table represents the business concept;
- selected fields are sufficient but not excessive;
- filters and ordering match the question;
- the result limit is appropriate;
- parameter values come from approved environment or scenario context;
- the plan contains no mutation, procedure call, or locking read.

If the proposed query exposes more data than needed, ask Plantain to narrow the
projection or result scope in plain language.

## Permanent read-only boundary

Plantain rejects agent or scenario requests containing `INSERT`, `UPDATE`,
`DELETE`, `MERGE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, procedure calls, locking
reads, or equivalent mutations.

The separate `db-mutate` CLI is a human-only reviewed boundary. It is never
available to the agent or a scenario, and it is not a workaround when a test
request requires mutation.

## Credentials and sensitive data

Database URLs, usernames, passwords, wallet values, and driver secrets belong in
the runtime environment. Generated tests retain an environment reference, not the
resolved connection value.

Ask only for data needed to prove the behavior. Even sanitized result handling is
not a reason to retrieve unnecessary personal, financial, or regulated fields.

## When Plantain pauses

Plantain stops for clarification or correction when:

- the database type or source reference is missing;
- the destination is not allowed or cannot be validated safely;
- TLS or driver requirements are unmet;
- the business concept maps to multiple plausible tables;
- the request would require scanning or returning too much data;
- a statement cannot be proven read-only;
- parameters do not match the query exactly;
- the requested result shape is ambiguous;
- the task requires mutation.

Refine the business question, approve a narrower source, or correct the environment.
Plantain will not weaken the read-only boundary.

Next, learn about [cross-system testing](cross-system.md),
[running tests](run-tests.md), or [reviewing results](results.md).
