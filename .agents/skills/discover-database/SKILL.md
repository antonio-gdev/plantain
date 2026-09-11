---
name: discover-database
description: "Answer plain-language questions about PostgreSQL, SQL Server, MySQL, or Oracle and create database evidence or test YAML by discovering schemas, tables, and one table's reflected metadata before issuing bounded read-only SELECTs. Use for current database facts, relationships, business-state checks, or database-backed test planning; never use for mutation."
---

# Discover Database

Translate the tester's intent into the minimum reviewable YAML needed to discover the live structure, retrieve narrowly scoped facts, and give a direct answer. A non-technical tester should not need to write SQL or understand drivers, pools, or reflection.

## Immutable safety boundary

Agent-authored database work may use only these registered YAML activities:

- `discoverDatabase` for SQLAlchemy Inspector metadata;
- `queryDatabase` for one parameterized, proven read-only `SELECT`;
- `verifyDatabaseResult` for assertions against a chained result without another database call.

Never generate or invoke the separate human-only mutation command. Never generate `INSERT`, `UPDATE`, `DELETE`, `MERGE`, DDL, stored-procedure calls, locking reads, or multiple statements. If the request would modify data, stop and state that agent database access is read-only.

Never read `.env` or ask for credential values. Ask only for environment-variable names when the defaults are unsuitable. Every discovery/query step carries an explicit source whose values remain environment references:

```yaml
source:
  username: "env:APP_DB_USERNAME"
  password: "env:APP_DB_PASSWORD"
  dbUrl: "env:APP_DB_URL"
```

`APP_DB_URL` contains the full JDBC-style target, including the database/service name. Do not place
its resolved value in YAML, output, logs, or explanations. The human must authorize its exact
`host:port` through `PLANTAIN_DB_ALLOWED_TARGETS`; private endpoints and non-local deployments also
require their explicit network controls. Require least-privilege read-only credentials for agent
runs even though the runtime also applies a statement firewall, transaction safeguards, TLS
policy, timeouts, result bounds, and bounded pooling.

## Progressive workflow

Do not scan or reflect the entire database. Work progressively:

1. Run `discoverDatabase` with `phase: schemas`. Follow `nextCursor` while `hasMore` is true until the requested schema is found or discovery is exhausted.
2. Run `phase: tables` for exactly one reflected schema. Set `includeViews: true` only when views are in scope. Follow pagination until the requested object is found or discovery is exhausted.
3. Run `phase: table` for exactly one reflected table/view. Inspect `tableMetadata.columns`, keys, constraints, and the returned `dialect`. This phase retrieves metadata only—never application rows.
4. Only when the question needs row data, generate one narrow `queryDatabase` `SELECT` using the exact reflected identifiers. Project only necessary columns, bind all user/business values through `parameters`, and apply deterministic ordering and a hard row limit.
5. For a reusable assertion, chain the result into `verifyDatabaseResult`. For an ad-hoc question, declare only the requested values in `outputs` and answer from the sanitized scenario result.

Even when the prompt names a schema, table, or column, confirm its exact spelling and case through reflection before querying it. Several phases may share one scenario only when the later identifiers were already verified by current evidence; otherwise append the next phase after inspecting the prior approved run.

Before every command, show the exact command, explain what it does and why it is necessary, and wait for explicit approval.

Create ad-hoc/reusable files under `scenarios/generated/database/<intent-slug>.yaml`. Run `plantain validate` before execution. Explain that execution connects read-only to the configured database, then wait for separate approval before `plantain run`.

## Query decisions

Before writing a `queryDatabase` statement, read [references/sql-dialects.md](references/sql-dialects.md) for the reflected dialect.

Map common phrases as follows:

- “most recent” → exclude null dates when appropriate, order the evidenced date/time column descending, add a reflected stable tie-breaker, and limit to one row;
- “oldest” → the corresponding ascending deterministic query;
- “how many” → `COUNT(*)` or a narrower evidenced count expression with `resultMode: value`;
- “where”, “with”, or “matching” → bind values as `:named_parameter`; never concatenate them into SQL;
- “price and item” → project only those columns plus ordering/filter columns needed to prove the answer.

Never guess which column represents recency, price, status, identity, or a relationship. If reflection does not establish the business meaning, ask the tester. Never use `SELECT *`.

Choose `resultMode` deliberately:

- `value`: exactly one projected column and at most one row;
- `row`: one object or null, with at most one row;
- `rows`: a bounded list; `maxRows` is valid only in this mode.

`rowCount` distinguishes no row from a one-row SQL `NULL`. Treat `truncated: true` as incomplete evidence.

## Discovery example

For “In database CLOTHING, schema Brands, what was the most recent item released by Uno and what is the price?”, first generate metadata-only YAML:

```yaml
scenario: "Discover the most recent Uno item and price"
tags:
  - database
  - discovery

steps:
  - discoverDatabase:
      id: discover_schemas
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: schemas

  - discoverDatabase:
      id: discover_brand_tables
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: tables
      schema: "Brands"

  - discoverDatabase:
      id: discover_uno
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: table
      schema: "Brands"
      table: "Uno"

outputs:
  dialect: "${discover_uno.dialect}"
  tableMetadata: "${discover_uno.tableMetadata}"
```

After an approved run proves the dialect and exact `Item`, `Price`, and `Date` identifiers, append a dialect-correct `queryDatabase` step. For example, only when the reflected dialect is `postgresql`:

```yaml
  - queryDatabase:
      id: latest_uno_item
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      sql: |-
        SELECT "Item", "Price", "Date"
        FROM "Brands"."Uno"
        WHERE "Date" IS NOT NULL
        ORDER BY "Date" DESC, "Item" ASC
        LIMIT 1
      resultMode: row

outputs:
  result: "${latest_uno_item.result}"
  rowCount: "${latest_uno_item.rowCount}"
  truncated: "${latest_uno_item.truncated}"
```

Do not reuse that SQL quoting/limit syntax for a different reported dialect.

## Answering the tester

After approved execution:

1. Read only the sanitized declared outputs or sanitized result report; do not inspect connection configuration.
2. State the direct answer first.
3. State the database dialect, schema, table/view, projected columns, filters, ordering, and limit as concise provenance.
4. If `rowCount` is zero, say no matching row was returned. If `truncated` is true, state that the evidence is incomplete. Never manufacture an answer.
5. Preserve identifier case and portable returned values exactly; explain encoded binary/LOB values rather than decoding or expanding them without a request.
6. Report the scenario path so the evidence can be reused by test planning or automation generation.
