---
name: c2w-migrate
description: Create or apply an Alembic migration for c2w, including the Row-Level Security policies and brand partitions that every tenant-scoped table needs. Use when adding or changing a table, adding a brand, or when RLS/partition boilerplate is required.
---

# Migrations

```bash
uv run alembic revision -m "add x"     # new revision
uv run alembic upgrade head            # apply
uv run alembic downgrade -1            # roll back one
```

Never use `Base.metadata.create_all()` against a real database — it skips the
RLS policies and partitions, which is precisely the part that enforces the
"brands never share data" requirement.

## Every tenant-scoped table needs four things

1. a `brand_id BIGINT NOT NULL` column,
2. `ENABLE ROW LEVEL SECURITY` **and** `FORCE ROW LEVEL SECURITY`,
3. a policy keyed off the `c2w.brand_id` session variable,
4. a test proving brand A cannot read brand B's rows.

Boilerplate for (2) and (3):

```sql
ALTER TABLE my_table ENABLE ROW LEVEL SECURITY;
ALTER TABLE my_table FORCE ROW LEVEL SECURITY;

CREATE POLICY my_table_brand_isolation ON my_table
  USING (brand_id = current_setting('c2w.brand_id', true)::bigint)
  WITH CHECK (brand_id = current_setting('c2w.brand_id', true)::bigint);

-- Workers and the reconciler legitimately cross brands; they connect as a role
-- holding BYPASSRLS rather than defeating the policy in SQL.
CREATE POLICY my_table_platform ON my_table TO c2w_platform USING (true);
```

`FORCE` matters: without it the policy does not apply to the table's owner, so
it would silently do nothing when the app connects as owner.

Use `current_setting(..., true)` (the `true` = missing_ok). A request that
forgot to set the variable then sees **zero rows** rather than everything —
fail closed, not open.

## Adding a brand means adding partitions

`cdrs` and `recordings` are `PARTITION BY LIST (brand_id)`. A new brand needs
its partitions created or its data has nowhere to go:

```sql
CREATE TABLE cdrs_brand_3 PARTITION OF cdrs FOR VALUES IN (3);
CREATE TABLE recordings_brand_3 PARTITION OF recordings FOR VALUES IN (3);
```

`c2w-admin brand add` does this in the same transaction as the brand row.
If you create a brand by hand in SQL, you must create the partitions too.

## Migrating a table with 19M rows

- Add columns `NULL` first, backfill in batches, then add the constraint.
- Build indexes `CONCURRENTLY` in a separate migration with
  `op.get_context().autocommit_block()` — a plain `CREATE INDEX` takes an
  `ACCESS EXCLUSIVE` lock and stalls every worker.
- Never rewrite `recordings` in one statement; partitions let you do it brand by
  brand.

## audit_events is insert-only

`UPDATE` and `DELETE` are revoked from the application role. If a migration
needs to change its shape, add a column — do not rewrite history.
