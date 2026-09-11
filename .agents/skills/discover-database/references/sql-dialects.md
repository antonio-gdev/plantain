# Reflected SQL dialect rules

Read only the section matching `discoverDatabase.dialect`. These examples show query shape; replace identifiers only with exact names returned by `phase: table`.

## Shared rules

- Generate one `SELECT` or read-only common-table-expression statement.
- Project only required columns; never use `SELECT *`.
- Use SQLAlchemy named binds such as `:status` for every value originating in the prompt or test data. Put the corresponding values in `parameters`.
- Table, schema, view, and column identifiers cannot be bind parameters. Accept them only from reflection and quote them for the reported dialect.
- Make `resultMode: row` and `value` deterministic and limited to at most one row.
- Exclude null ordering values when “latest” or “oldest” requires an actual date/time.
- Add a reflected stable tie-breaker when the primary ordering column can tie.
- Never add locking clauses such as `FOR UPDATE` or SQL Server lock hints.

## `postgresql`

- Quote identifiers with double quotes.
- Limit with `LIMIT <integer>`.

```sql
SELECT "Item", "Price", "Date"
FROM "Brands"."Uno"
WHERE "Date" IS NOT NULL
ORDER BY "Date" DESC, "Item" ASC
LIMIT 1
```

## `mysql`

- Quote identifiers with backticks.
- Limit with `LIMIT <integer>`.
- Treat the reflected schema/database name exactly as Inspector reports it.

```sql
SELECT `Item`, `Price`, `Date`
FROM `Brands`.`Uno`
WHERE `Date` IS NOT NULL
ORDER BY `Date` DESC, `Item` ASC
LIMIT 1
```

## `mssql`

- Quote identifiers with brackets.
- Use `TOP (<integer>)` for a one-row projection.
- Do not generate transaction or lock hints.

```sql
SELECT TOP (1) [Item], [Price], [Date]
FROM [Brands].[Uno]
WHERE [Date] IS NOT NULL
ORDER BY [Date] DESC, [Item] ASC
```

## `oracle`

- Quote case-sensitive reflected identifiers with double quotes.
- Limit an ordered result with `FETCH FIRST <integer> ROWS ONLY`.

```sql
SELECT "Item", "Price", "Date"
FROM "Brands"."Uno"
WHERE "Date" IS NOT NULL
ORDER BY "Date" DESC, "Item" ASC
FETCH FIRST 1 ROWS ONLY
```
