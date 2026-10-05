# Schema Sync v2 — Data Operations, Data Browser, Comparison Improvements

Date: 2026-10-05
Status: Draft for review
Builds on: `2026-10-04-schema-sync-validator-design.md`

## 1. What changes

Until now the tool was read-only everywhere. v2 adds **two write operations**
that run from the UI against databases you explicitly enable for writes, plus a
read-only data browser and four comparison improvements.

| Area | Feature |
|---|---|
| Data operations (write) | **Delete a record** (customer, device, user, …) with everything that cascades from it |
| | **Delete matching rows** — any filter on a table (from the data browser), with cascade, single transaction |
| | **Cleanup** rows matching a filter (e.g. execution history older than 90 days), with cascade, in batches; saved as reusable recipes |
| | **Drop a column** (typically one the comparison reports as "not in model"), with preview, backup and restore script |
| Read | **Data browser**: tables, rows, filters, related records |
| Comparison | **DB vs DB** compare (e.g. stage vs prod-us) |
| | **Background runs** with progress, **re-run**, **changes since the previous run** |
| | **Ignore rules** (accepted drift per environment) |
| | Cached project extraction (no re-import when the checkout has not changed) |

## 2. Why cascade must come from the models

Django creates foreign keys **without** `ON DELETE CASCADE` and performs
`on_delete` in Python. A plain `DELETE FROM customer` fails on the first child
row. The tool therefore builds the cascade from two sources:

1. **Model relations** (from the extractor): every foreign key pointing at a
   table with its `on_delete` (`CASCADE`, `PROTECT`, `RESTRICT`, `SET_NULL`,
   `SET_DEFAULT`, `DO_NOTHING`, custom), multi-table-inheritance parent links,
   and declared `GenericRelation`s.
2. **Database foreign keys** (from the inspector, with `confdeltype`): catches
   tables the branch does not know about (another feature's app on prod), and
   FKs created with real `ON DELETE` actions.

Rules, per relation pointing at rows being deleted:

| relation | behaviour |
|---|---|
| model `CASCADE` | child rows are deleted too (recursively) |
| model `SET_NULL` | child column set to NULL (row kept) |
| model `PROTECT` / `RESTRICT` | **blocker** if any child row exists that is not itself being deleted |
| model `SET_DEFAULT`, custom `SET(...)` | **blocker** — handle manually |
| model `DO_NOTHING` | if the DB has a constraint → blocker; else warning "rows will be orphaned" |
| MTI parent link | deleting a child-model row also deletes its parent-model row (Django semantics) |
| `GenericRelation` | rows with matching `content_type_id` + `object_id` are deleted |
| DB-only FK, `ON DELETE CASCADE` / `SET NULL` | follows the database action |
| DB-only FK, `NO ACTION` / `RESTRICT` | **blocker**, unless you tick "treat as cascade" for that relation in the preview |

Without a project selected, only database FKs are used (all `NO ACTION` FKs
become blockers); the UI recommends selecting the project.

## 3. Write safety model

**Opt-in per database.** `DatabaseTarget` gains:

- `environment` — `dev` / `stage` / `prod` (badge colour everywhere; prod is red)
- `writes_enabled` — off by default; when off, delete/cleanup are not offered
- `write_user` / write password (encrypted) — optional separate credentials;
  blank means the normal user. Comparisons and the browser always use read-only
  sessions.

**Every operation goes through the same stages:**

1. **Plan** (in a transaction, always rolled back): compute the set of rows per
   table into temporary key tables, breadth-first over the relations in §2,
   until no new rows appear (handles cycles). Produces: per-table row counts,
   SET NULL counts, blockers with sample rows, 5 sample rows per table,
   estimated backup size.
2. **Rehearse** (record delete: always; cleanup: first batch): perform the real
   deletes inside the same transaction, then roll back. Surfaces constraint
   errors and trigger effects before anything is committed.
3. **Confirm** — a danger panel shows the plan. The execute button stays
   disabled until you type the exact phrase, e.g.
   `delete accounts_customer 42 on prod-us`. On `prod` you must also tick
   "I have checked the backup location". A plan older than 15 minutes must be
   re-run.
4. **Back up** — inside the executing transaction, before deleting, every row
   that will be deleted or nulled is streamed (server-side cursor) to
   `data/backups/<operation-id>/<table>.jsonl.gz`. A restore script
   (`INSERT … SELECT * FROM json_populate_record(…)` parents first, then the
   `UPDATE`s for nulled columns) is generated and downloadable.
5. **Execute** — `SET CONSTRAINTS ALL DEFERRED`, nulls first, then deletes
   leaves-first; `lock_timeout = 5s`, `statement_timeout` configurable
   (default 5 min). Counts are compared with the plan; if they differ
   (data changed since the preview) the transaction is rolled back and you are
   asked to re-plan.
6. **Audit** — every plan, rehearsal, execution, and failure is recorded:
   who-typed-what, counts, SQL statements, backup path, duration, error.

**Cleanup batching.** A cleanup recipe has a batch size (default 5 000 root
rows). Each batch is planned, backed up and executed in its own transaction,
re-checking counts per batch; progress is shown live and the job can be
cancelled between batches. A record delete is always a single transaction.

**Drop column** follows the same stages: plan shows non-NULL count, sample
values, dependent indexes/constraints (dropped with the column) and dependent
views (a **blocker** — no `CASCADE`); a warning when the selected branch's
models still use the column. Backup = primary key + column value; restore =
`ADD COLUMN` + `UPDATE`s. Confirmation phrase: `drop <table>.<column> on <db>`.

**Never:** raw SQL from the UI, `TRUNCATE`, `DROP TABLE`, other schema
changes, or executing the comparison fix scripts. Filters are structured (column, operator, value) and
always parameterised.

## 4. Data operations UI

**Delete a record** — `Data → pick database → table → find row → Delete…`
(or from a row in the browser):

```
┌ DANGER ZONE · prod-us ─────────────────────────────────────────────┐
│ Delete accounts_customer #42 "Acme Corp"                           │
│                                                                    │
│ Will delete 18 214 rows in 7 tables      Will set NULL in 2 rows   │
│  accounts_customer        1                                        │
│  └ devices_device        42   CASCADE (customer)                   │
│    └ exec_execution   18 013   CASCADE (device)                    │
│  └ users_user           158   CASCADE (customer)                   │
│ Blockers: none        Rehearsal: passed (1.8 s)                    │
│ Backup: data/backups/op-117/ (~6.2 MB)                             │
│                                                                    │
│ Type  delete accounts_customer 42 on prod-us  to confirm           │
│ [__________________________________]  [Delete 18 214 rows]         │
└────────────────────────────────────────────────────────────────────┘
```

**Cleanup recipes** — `Data → Cleanup`: name, project (optional), root table,
filters (`=`, `≠`, `<`, `≤`, `>`, `≥`, `in`, `is null`, `is not null`,
`contains`, **older than N days** on date/time columns), batch size. Run a
recipe against a writes-enabled database: plan → rehearse first batch →
confirm (`cleanup <recipe> on <db>`) → batches with progress.

**Operations log** — every operation with status, counts, duration, backup
download, restore-script download.

## 5. Data browser (read-only)

- Database → tables with estimated rows and size (from `pg_class`), search.
- Table → paginated rows (50/page), sort, the same structured filters.
- Row → all columns; **parents** (outgoing FKs) as links; **children**
  (incoming FKs, model + DB) with counts linking to filtered child tables.
- Columns whose names look secret (`password`, `secret`, `token`, `api_key`,
  `private_key`, …) are masked; a per-page toggle reveals them.
- Row label heuristic for display: first of `name`, `title`, `email`,
  `username`, `hostname`, `slug`, `code`, else the PK.

## 6. Comparison improvements

**DB vs DB** — choose a reference database and one or more targets. The
reference's tables are converted into the same model shape the branch diff
uses (columns, types, nullability, PK, uniques, indexes, FKs), so the schema
diff and severities are identical; history is compared as two sets of
`(app, name)` rows ("only in reference", "only in target"). Report only, no
fix SQL (use a branch comparison for that).

**Background jobs** — comparisons and data operations run in a background
thread inside the Django process; the page shows progress and polls a small
JSON endpoint. A server restart marks running jobs as `interrupted`. SQLite is
switched to WAL mode.

**Re-run** — one button re-runs a batch with the same project, databases and
options.

**Changes since previous run** — on a run page, findings are compared with the
previous run of the same project + database by key
`(category, app, table, column)`: *new*, *resolved*, *unchanged*.

**Ignore rules** — `IgnoreRule(database|all, project|all, category|any,
object glob, note)`. Added from any finding row ("Ignore…", prefilled).
Matching findings are marked *ignored*: excluded from counts and from the fix
script, listed collapsed with the rule that matched.

**Extraction cache** — the extractor output is stored per project with the git
`HEAD` commit and a hash of `git status --porcelain`; reused while both are
unchanged (non-git folders are never cached). "Refresh models" forces a re-run.

## 7. Extractor additions

Per model:

- `relations[]` — every FK pointing **at** this model: `child_table`,
  `child_column`, `parent_column` (target of `to_field`), `on_delete`
  (`CASCADE`, `PROTECT`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `DO_NOTHING`,
  `CUSTOM`), `nullable`. Taken from hidden reverse relations too, so
  auto-created M2M through tables are included.
- `parent_links[]` — MTI: `parent_table`, `child_column`, `parent_column`.
- `generic_relations[]` — `child_table`, `ct_column`, `object_id_column`,
  plus this model's `app_label` / `model` for the content type lookup.
- `pk` column name.

## 8. New stored data

| model | key fields |
|---|---|
| `DatabaseTarget` (+) | `environment`, `writes_enabled`, `write_user`, `write_password_encrypted`, `operation_timeout_s` |
| `ProjectSnapshot` | project, commit, status hash, extracted JSON, created |
| `Job` | kind (`compare`, `delete`, `cleanup`), status (`queued`, `running`, `done`, `failed`, `cancelled`, `interrupted`), progress 0–100, message, log, links to batch / operation |
| `IgnoreRule` | database (null = all), project (null = all), category (blank = any), object glob, note |
| `CleanupRecipe` | name, project (null), root table, filters JSON, batch size |
| `DataOperation` | kind, database, project, root table, filters / pks, plan JSON, rehearsal result, confirmation text, status, executed counts, backup dir, SQL log, error, timestamps |
| `ComparisonRun` (+) | `kind` (`branch` / `db`), `reference_name`, `ignored` findings |

## 9. Testing

All against real PostgreSQL (skipped if unavailable):

- Planner: CASCADE chains, SET_NULL, PROTECT blocker, RESTRICT satisfied by
  another cascade path, DO_NOTHING with and without a DB constraint, MTI parent
  deletion, GenericRelation, cycles, DB-only FK blocker and "treat as cascade",
  `to_field` FKs.
- Executor: rehearsal rolls back (row counts unchanged); execute deletes exactly
  the planned rows; backup files contain every deleted row; the restore script
  restores them (row-level equality); count drift between plan and execute
  aborts with nothing deleted; writes refused when `writes_enabled` is off; wrong
  confirmation phrase refused; stale plan refused; cleanup batches and cancel.
- Browser: pagination, filters (parameterised; injection attempt is a literal),
  masking, related records.
- Comparison: DB-vs-DB findings; ignore rules exclude from counts and fix SQL;
  changes-since-previous; extraction cache hit/miss; jobs complete and
  interrupted jobs are marked on startup.
- UI: every page renders; danger confirmation disabled until the phrase
  matches; prod requires the extra tick.
