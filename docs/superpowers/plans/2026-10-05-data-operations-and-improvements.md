# Schema Sync v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, previewed, backed-up delete/cleanup/drop-column operations, a read-only data browser, DB-vs-DB comparison, background jobs, ignore rules, run-to-run changes and an extraction cache to the Schema Sync tool.

**Architecture:** Same single Django app. A cascade *planner* resolves rows to delete into temp tables (by `ctid`) inside one PostgreSQL transaction using model relations from the extractor merged with DB foreign keys; an *executor* re-plans inside the executing transaction, verifies counts against the confirmed preview, backs up rows to gzipped JSONL, nulls then deletes deepest-first, and commits. Long work runs as `Job`s on a thread (inline in tests).

**Tech Stack:** Python 3.11, Django 5.2, psycopg 3, PostgreSQL ≥ 14 (tid hashing), pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-data-operations-and-improvements-design.md` (builds on `2026-10-04-schema-sync-validator-design.md`)

## Global Constraints

- Writes only when `DatabaseTarget.writes_enabled`; comparisons and browser always use the read-only session options.
- Write sessions: `lock_timeout=5s`, `statement_timeout = operation_timeout_s` (default 300).
- Confirmation phrases exactly: `delete <table> <pk> on <db>` (single-row delete), `delete <N> rows from <table> on <db>` (filtered delete), `cleanup <recipe name> on <db>`, `drop <table>.<column> on <db>`. Prod additionally requires the backup checkbox.
- A plan older than 15 minutes cannot be executed.
- Execution aborts (rollback, nothing changed) when re-planned counts differ from the confirmed preview.
- Backups: `DATA_DIR/backups/op-<id>/<table>.jsonl.gz`, written inside the executing transaction before any change.
- No raw SQL input anywhere; filter values always bound as parameters; identifiers via `psycopg.sql.Identifier`.
- Never TRUNCATE, DROP TABLE, or run comparison fix scripts.
- Django FK `on_delete` names: `CASCADE, PROTECT, RESTRICT, SET_NULL, SET_DEFAULT, DO_NOTHING, CUSTOM`.

## Review Focus

1. A row reachable through two cascade paths, or a cycle (self-FK `parent_id`) → counted and deleted once, planner terminates. (Task 4 tests `test_cycle_self_fk`, `test_diamond_counted_once`.)
2. A filter value that is not valid for the column type (`abc` for an integer) → clear validation error, not a 500. (Task 3 `test_bad_value_for_type`.)
3. Data changing between preview and execute → abort with nothing deleted. (Task 5 `test_count_drift_aborts`.)
4. A table name / filter crafted as SQL (`x'; DROP TABLE y;--`) → treated as a literal or rejected (unknown table). (Task 3 `test_injection_is_literal`, Task 8 `test_unknown_table_404`.)
5. Writes attempted on a database without `writes_enabled`, with a wrong phrase, or with a stale plan → refused. (Task 8 view tests.)

---

### Task 1: Models, settings, database form

**Files:** Modify `validator/models.py`, `validator/forms.py`, `schemasync/settings.py`, `validator/apps.py`; create migration `0002`; tests `tests/test_models_v2.py`.

**Produces:**
- `DatabaseTarget`: `environment` (`dev|stage|prod`, default `dev`), `writes_enabled` (False), `write_user` (blank), `write_password_encrypted`, `operation_timeout_s` (300); methods `set_write_password(str)`, `write_conninfo() -> dict` (falls back to read user/password), `is_prod` property.
- `ProjectSnapshot(project FK CASCADE, commit, status_hash, data JSON, created_at)`.
- `Job(kind, status, progress int, message, log text, cancel_requested bool, result_url, created_at, finished_at)`; statuses `queued, running, done, failed, cancelled, interrupted`.
- `IgnoreRule(database FK null CASCADE, project FK null CASCADE, category blank, pattern, note, created_at)`; `matches(finding, database_id, project_id) -> bool` using `fnmatch` on `finding_object(finding)`.
- `CleanupRecipe(name unique, project FK null SET_NULL, root_table, filters JSON list, batch_size=5000, notes)`.
- `DataOperation(kind delete|cleanup|drop_column, database FK SET_NULL, database_name, environment, project FK SET_NULL, recipe FK SET_NULL, root_table, column, filters JSON, cascade_overrides JSON list, plan JSON, planned_at, rehearsal JSON, confirmation, status planned|running|done|failed|cancelled, counts JSON, backup_dir, sql_log text, error, job FK null, created_at, finished_at)`.
- `ComparisonRun`: `kind` (`branch|db`, default `branch`), `reference_name`.
- Settings: `JOBS_INLINE` (env `SCHEMASYNC_JOBS_INLINE`, tests set True), `BACKUP_DIR = DATA_DIR/"backups"`, `PLAN_MAX_AGE_MINUTES = 15`; SQLite `journal_mode=WAL`, `busy_timeout=5000` via `connection_created`.
- `DatabaseForm`: new fields incl. `write_password` (same keep/clear behaviour as `password`).

- [ ] Tests: write conninfo falls back to read creds; write password stored encrypted; IgnoreRule matching (db/project scoping, category any, glob `legacy_*`); migration applies.
- [ ] Implement, `makemigrations`, run tests, commit.

### Task 2: Extractor relations + inspector FK actions/sizes

**Files:** Modify `validator/extractor.py`, `validator/db_inspector.py`; tests in `tests/test_extractor.py`, `tests/test_db_inspector.py`; sample project gains app `devices` (Customer→Device CASCADE, Device→Execution CASCADE, Execution.operator→User SET_NULL, Contract PROTECT→Customer, Note with GenericForeignKey + `Customer.notes = GenericRelation`, MTI `PremiumCustomer(Customer)`, self-FK `Device.parent` CASCADE nullable).

**Produces:**
- Extractor top-level `relations[]`: `{child_table, child_column, parent_table, parent_column, on_delete, nullable, parent_link}`; `generic_relations[]`: `{parent_table, parent_app_label, parent_model, parent_pk, child_table, ct_column, object_id_column}`; per model `pk`.
- Inspector: each FK gains `on_delete` (`a|r|c|n|d` from `confdeltype`) and `deferrable`; each table gains `estimated_rows`, `total_bytes`; new `inspect_tables_light(conninfo, schema)` is not needed — reuse `inspect_database`.

- [ ] Tests: `devices_execution.device_id → devices_device` CASCADE; `operator_id` SET_NULL nullable; MTI relation has `parent_link: true`; generic relation row; inspector FK `on_delete == "a"` for Django FKs and `"c"` for an `ON DELETE CASCADE` FK.
- [ ] Implement, regenerate sample migrations (`devices/0001_initial`), keep `orders.note` pending, run full suite, commit.

### Task 3: Structured filters

**Files:** Create `validator/filters.py`; test `tests/test_filters.py`.

**Produces:** `OPERATORS` (dict op → label): `eq, ne, lt, le, gt, ge, in, isnull, notnull, contains, older_than_days, newer_than_days`; `class FilterError(ValueError)`; `build_where(columns: dict[name, type], filters: list[{column, op, value}]) -> psycopg.sql.Composed` (returns `TRUE` when empty), values cast with `CAST(%s AS <type>)` after validating the type string; `parse_filters(querydict) -> list` (keys `f_col`, `f_op`, `f_val`, repeated); `describe(filters) -> str`.

- [ ] Tests against Postgres: each operator selects the expected rows; `in` with `"1, 2"`; `contains` escapes `%`/`_`; unknown column → FilterError; `test_bad_value_for_type` → FilterError (raised when executed: wrap `psycopg.errors.InvalidTextRepresentation`/`DataError` via `validate_filters(conn, table, columns, filters)`); `test_injection_is_literal`.
- [ ] Implement, run, commit.

### Task 4: Cascade graph + planner

**Files:** Create `validator/cascade.py` (graph), `validator/planner.py`; test `tests/test_planner.py` (uses sample `devices` schema migrated into a fresh DB).

**Produces:**
- `build_graph(db_info: dict, extracted: dict | None, overrides: list[str]) -> Graph`; `Relation(id, child_table, child_column, parent_table, parent_column, action, source)` where `action ∈ cascade|set_null|protect|restrict|block|orphan|parent_link|generic`; id = `child.col->parent.col`; DB-only `a|r` → `block` unless id in overrides → `cascade`; `d`/SET_DEFAULT/CUSTOM → `block`; DO_NOTHING → `block` if DB FK exists else `orphan`. Relations whose tables/columns are missing in the DB are dropped and listed in `graph.skipped`.
- `plan(conn, graph, root_table, where_sql, *, limit=None, samples=5) -> dict`: creates temp tables `_ss_del(tbl, rid tid, depth, via)` and `_ss_null(tbl, rid tid, col)` (ON COMMIT DROP), fixpoint by rounds, returns `{root_table, root_count, total_rows, tables:[{table, count, depth, via:[{relation, action, count}], samples:[row]}], nulls:[{table, column, count}], blockers:[{relation, action, count, samples}], orphans:[...], skipped:[...]}`. Caller owns the transaction.

- [ ] Tests: customer → devices → executions counted; operator SET_NULL listed in nulls; Contract PROTECT blocker; RESTRICT satisfied when child also cascaded; `test_cycle_self_fk`; `test_diamond_counted_once`; MTI child delete includes parent row; generic relation rows included; DB-only FK blocker and override → cascade; no project → all Django FKs are blockers; plan leaves DB unchanged.
- [ ] Implement, run, commit.

### Task 5: Executor, backup, restore script

**Files:** Create `validator/executor.py`; test `tests/test_executor.py`.

**Produces:** `class OperationError(Exception)`; `rehearse(conninfo, graph, root_table, where_sql, timeout_s) -> dict` (`{ok, duration_ms, error, counts}`; always rolls back); `execute(conninfo, graph, root_table, where_sql, *, expected: dict | None, backup_dir: Path, timeout_s, limit=None, log: list) -> dict` (counts per table + nulls; raises OperationError on blockers or count drift; writes backups); `restore_script(backup_dir: Path) -> Iterator[str]` (parents first, `OVERRIDING SYSTEM VALUE`, then `UPDATE`s restoring nulled columns by PK).

- [ ] Tests: rehearsal leaves counts unchanged; execute deletes exactly the plan and nulls operator; backup files contain every deleted row; running the restore script restores rows (compare `row_to_json` sets); `test_count_drift_aborts` (expected counts differ → nothing deleted); blockers → OperationError, nothing deleted.
- [ ] Implement, run, commit.

### Task 6: Drop column

**Files:** Create `validator/column_ops.py`; test `tests/test_column_ops.py`.

**Produces:** `plan_drop_column(conninfo, schema, table, column, extracted=None) -> dict` (`{type, non_null, samples, indexes, constraints, views (blocker), in_model: bool, pk}`); `execute_drop_column(conninfo, table, column, *, backup_dir, timeout_s, log) -> dict`; `drop_column_restore_script(backup_dir) -> Iterator[str]`.

- [ ] Tests: plan lists the index on the column and a dependent view as blocker; execute drops the column and backs up pk+value; restore script re-adds column with values; refusing when a view depends.
- [ ] Implement, run, commit.

### Task 7: Jobs + operation runner + cleanup batches

**Files:** Create `validator/jobs.py`, `validator/operations.py`; test `tests/test_operations.py`.

**Produces:** `start_job(kind: str, target: Callable[[Job], str|None]) -> Job` (thread or inline per `JOBS_INLINE`; sets running/done/failed, stores returned result URL, closes DB connections); `recover_interrupted()`; `Job.check_cancel()` raises `JobCancelled`.
`operations.py`: `load_graph(op) -> Graph` (uses project snapshot when op.project set); `preview_operation(op) -> None` (fills plan + rehearsal; cleanup plans first batch and counts all matching roots); `confirmation_phrase(op) -> str`; `run_operation(op, job)` (delete → one `execute`; cleanup → batches until no rows, cancelled, or previewed root count reached; drop_column → `execute_drop_column`).

- [ ] Tests: delete op end-to-end via runner; cleanup with batch_size 2 deletes all matching in several batches and records per-batch log; cancel between batches stops; failure marks op failed with error; recover_interrupted marks running jobs.
- [ ] Implement, run, commit.

### Task 8: Data browser + operations UI

**Files:** Create `validator/views_data.py`, templates `data_home, data_tables, data_table, data_row, op_new, op_detail, op_list, recipe_form, recipe_list, job`; modify `validator/urls.py`, `base.html`, `app.css`, `app.js`; test `tests/test_views_data.py`.

Routes: `/data/` (pick DB), `/data/<db>/` (tables), `/data/<db>/t/<table>/` (rows + filters + "Delete matching…"), `/data/<db>/t/<table>/r/<pk>/` (row, parents, children, "Delete this row…"), `/data/<db>/t/<table>/c/<column>/drop/` (start drop-column op), `/ops/` (log), `/ops/new/` (POST creates op + preview job), `/ops/<id>/` (danger panel, confirm form), `/ops/<id>/execute/` (POST), `/ops/<id>/backup/<file>`, `/ops/<id>/restore.sql`, `/recipes/…` CRUD + run, `/jobs/<id>/` + `/jobs/<id>.json`, `/jobs/<id>/cancel/`.

- [ ] Tests: browser pages render with filters and masking (`password` column shows `••••`); `test_unknown_table_404`; delete buttons absent when writes disabled; execute refused when writes disabled / wrong phrase / stale plan / prod without checkbox; correct phrase executes and redirects to job → op done; restore.sql downloads.
- [ ] Implement, run, commit.

### Task 9: Comparison improvements

**Files:** Modify `validator/runner.py`, `validator/project_loader.py`, `validator/views.py`, `validator/forms.py`, templates `dashboard, batch, run_detail`; create `validator/db_compare.py`, `validator/ignore.py`; tests `tests/test_v2_compare.py`.

**Produces:** `load_project_cached(project, force=False) -> dict` (snapshot by commit + `git status --porcelain` hash); `db_as_models(db_info, app_labels) -> list` and `diff_history_sets(ref_rows, target_rows) -> list[Finding]` (categories `history_only_reference`, `history_only_target`, warning); `apply_ignore_rules(report, rules, database_id, project_id) -> report` (moves to `report["ignored"]` with `rule_id`); `run_batch` gains `kind`/`reference` and runs through `start_job`; `previous_run(run)`, `changes_since(run, prev) -> {new: set[key], resolved: list[finding]}`; batch "Re-run"; run page "Ignore…" (prefilled rule form) and "new"/"resolved" display.

- [ ] Tests: DB-vs-DB finds a column only in reference; history-only sets; ignore rule excludes finding from summary and fix SQL; changes-since marks new/resolved; cache hit avoids extractor (monkeypatch counts), miss after file edit; re-run creates new batch.
- [ ] Implement, run, commit.

### Task 10: End-to-end, README, browser check

- [ ] `tests/test_end_to_end_v2.py`: migrate sample project, insert data, delete a customer via the views (preview → confirm → execute), verify rows gone, apply restore.sql with psql, verify rows back.
- [ ] README sections for Data browser, Operations, safety model, DB-vs-DB, ignore rules.
- [ ] Full suite green; drive the UI in Chromium, screenshot danger panel/browser; push.
