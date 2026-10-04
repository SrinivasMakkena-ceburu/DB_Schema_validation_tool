# Schema Sync Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single local Django app that compares a Django project checkout (models + migration files) against one or more PostgreSQL databases and generates — never executes — the SQL to align them.

**Architecture:** One Django project (`schemasync`) with one app (`validator`). The app stores projects, databases and runs in local SQLite. A standalone `extractor.py` is run with the target project's interpreter to dump models/migrations/DDL as JSON; the tool inspects the target DB read-only with psycopg, diffs, and renders server-side templates.

**Tech Stack:** Python 3.11, Django 5.2, psycopg 3 (binary), cryptography (Fernet), pytest + pytest-django, PostgreSQL 16 for integration tests.

**Spec:** `docs/superpowers/specs/2026-10-04-schema-sync-validator-design.md`

## Global Constraints

- PostgreSQL only; only the target project's `default` DB alias is modelled.
- Target DB sessions: `default_transaction_read_only=on`, `statement_timeout=30000`, `connect_timeout=10`.
- Extractor timeout 180 s; extractor never opens a DB connection.
- No code path executes generated SQL against a target DB.
- Runs on `127.0.0.1`; `ALLOWED_HOSTS = ["127.0.0.1", "localhost"]`; no login.
- `data/` (SQLite + `secret.key`) is git-ignored; stored passwords encrypted with Fernet; never rendered back.
- DROP statements in the fix script are always commented out.
- Constraints/indexes are matched by column set, never by name.

## Review Focus

1. Backend settings print to stdout or log on import → extractor writes JSON to a file passed by `--output`, not stdout, so noise cannot corrupt it. (Task 3 test: sample settings print a line.)
2. Backend `AppConfig.ready()` touches the DB → connection to the dummy host fails; the run must show a clear error with the traceback, not a 500. (Task 8 test: extractor failure → run status `error`, stderr stored.)
3. Large schemas (hundreds of tables) → inspector uses one query per catalog kind for the whole schema, never per table. (Task 4: assert query count is constant via a 30-table fixture.)
4. Squashed migrations where only the replaced rows are recorded → the squash counts as applied, the replaced rows are not ghosts. (Task 6 test.)
5. DB with no `django_migrations` table, or a non-`public` schema → reported, not a crash. (Task 4 + Task 6 tests.)

---

### Task 1: Scaffold, models, password crypto

**Files:** Create `manage.py`, `requirements.txt`, `pytest.ini`, `schemasync/{__init__,settings,urls,wsgi}.py`, `validator/{__init__,apps,models,crypto,admin}.py`, `validator/migrations/0001_initial.py` (via makemigrations), `tests/test_crypto.py`.

**Interfaces — Produces:**
- `validator.crypto.encrypt(plain: str) -> str`, `decrypt(token: str) -> str`; key at `settings.DATA_DIR / "secret.key"`, created on first use.
- Models per spec §4: `Project(name, path, python_path, settings_module, extra_env)`, `Project.env_dict() -> dict[str,str]`; `DatabaseTarget(name, host, port=5432, dbname, user, password_encrypted, sslmode="prefer", schema="public", notes)`, `DatabaseTarget.set_password(str)`, `.get_password() -> str`, `.conninfo() -> dict`; `ComparisonRun(batch_id, project FK SET_NULL, database FK SET_NULL, project_name, database_name, git_branch, git_commit, started_at, duration_ms, status, error, summary JSON, report JSON, fix_sql, options JSON)`.

- [ ] Test: `test_encrypt_roundtrip` (`decrypt(encrypt("p@ss:wörd")) == "p@ss:wörd"`, token != plaintext); `test_database_password_roundtrip` (set/get through model, `password_encrypted` not plaintext); `test_project_env_dict` (`"A=1\n# c\nB = two\n"` → `{"A":"1","B":"two"}`).
- [ ] Implement, `makemigrations validator`, run `pytest tests/test_crypto.py`, commit.

### Task 2: Type normalisation

**Files:** Create `validator/pgtypes.py`, `tests/test_pgtypes.py`.
**Produces:** `normalize_type(name: str) -> str` per spec §7.1.

- [ ] Test parametrised table: `character varying(255)`→`varchar(255)`, `varchar(255)`→same, `character(2)`→`char(2)`, `timestamp with time zone`→`timestamptz`, `timestamp without time zone`→`timestamp`, `time without time zone`→`time`, `serial`→`integer`, `bigserial`→`bigint`, `smallserial`→`smallint`, `numeric(10, 2)`→`numeric(10,2)`, `integer[]`→`integer[]`, `character varying(20)[]`→`varchar(20)[]`, `JSONB`→`jsonb`, `double precision`→same.
- [ ] Implement, run, commit.

### Task 3: Sample project + extractor

**Files:** Create `tests/sample_project/` with `manage.py`, `sampleproj/settings.py`, and apps `catalog` (Category, Product FK→Category, unique_together, index, M2M Tag) with `0001_initial` + `0002_product_sku`, and `0001_squashed_0002` squash; app `orders` (Order FK→catalog.Product) with `0001_initial`; one model field present in models but absent from migrations (`Order.note`) for pending changes; settings print `"settings loaded"` to stdout. Create `validator/extractor.py`, `tests/test_extractor.py`.

**Produces:** `python extractor.py --settings MOD --output PATH` writes the JSON of spec §5. Column dict keys: `name, db_type, null, primary_key, unique, fk, db_constraint`; model keys: `app_label, model, db_table, managed, columns, unique_sets, index_sets, create_sql, add_column_sql`; top keys: `django_version, apps, models, migrations{app:{nodes:[{name,dependencies,replaces}], leaves:[...]}}, pending_changes{app:[str]}`.

- [ ] Test (runs extractor with `sys.executable`, cwd sample project): `catalog_product` columns include `category_id` with `fk={"table":"catalog_category","column":"id"}`; M2M through table present; `unique_sets` contains `["category_id","name"]`; `create_sql` contains `CREATE TABLE "catalog_product"`; `add_column_sql["sku"]` contains `ADD COLUMN "sku"`; migrations for catalog include squash with 2 replaces; `pending_changes["orders"]` mentions `note`; output file is valid JSON despite the settings print.
- [ ] Test `test_extractor_import_error` → non-zero exit, traceback on stderr.
- [ ] Implement (override `DATABASES` before `django.setup()`; schema editor `collect_sql=True, atomic=False`); run; commit.

### Task 4: DB inspector

**Files:** Create `validator/db_inspector.py`, `tests/conftest.py` (fixture `pg_dsn`: creates a throwaway DB on local Postgres, skip if unavailable), `tests/test_db_inspector.py`.

**Produces:** `inspect_database(conninfo: dict, schema: str) -> dict` with keys `tables{name:{columns{name:{type,nullable,has_default}}, pk[list], uniques[[cols]], fks[{columns,ref_table,ref_columns}], indexes[[cols]]}}, views[list], migrations[{app,name,applied}] | None`; `test_connection(conninfo) -> str` (server version). Raises `InspectError(message)`.

- [ ] Tests: columns/types/nullability read; pk/unique/fk/index as column lists; missing `django_migrations` → `migrations is None`; non-public schema; session is read-only (attempting `CREATE TABLE` inside a helper using the same connect options raises); 30 tables inspected with constant query count.
- [ ] Implement, run, commit.

### Task 5: Schema diff

**Files:** Create `validator/schema_diff.py`, `tests/test_schema_diff.py`.
**Consumes:** extractor `models`, inspector `tables`, `normalize_type`.
**Produces:** `diff_schema(models: list, db: dict) -> list[Finding]`; `Finding` = dict `{category, severity, app, table, column, message, data}`; categories: `table_missing, table_extra, column_missing, column_extra, type_mismatch, null_mismatch, pk_mismatch, fk_missing, fk_wrong, unique_missing, index_missing`. Severities per spec §7.2. `django_migrations` excluded.

- [ ] One test per row of spec §7.2 table using hand-written dicts; plus `test_clean_schema_no_findings`.
- [ ] Implement, run, commit.

### Task 6: Migration diff

**Files:** Create `validator/migration_diff.py`, `tests/test_migration_diff.py`.
**Produces:** `diff_migrations(disk: dict, recorded: list | None, installed_apps: list, existing_tables: set, models: list) -> tuple[list[Finding], dict per_app]`; categories `history_missing, other_app_rows, ghost, unapplied, inconsistent, multiple_leaves`; `per_app[app] = {disk_leaf, db_latest, ghosts, unapplied}`.

- [ ] Tests: ghost; other-app rows; unapplied info vs error when tables exist (initial migration creating existing table); inconsistent; multiple leaves; squash applied via replaced rows (no ghosts for replaced, squash not unapplied); recorded None → `history_missing` error.
- [ ] Implement, run, commit.

### Task 7: Fix SQL

**Files:** Create `validator/fix_sql.py`, `tests/test_fix_sql.py`.
**Produces:** `build_fix_sql(*, header: dict, models: list, schema_findings, migration_findings, disk: dict, options: dict) -> str`; options `history_strategy in {"fake","leave"}`, `include_other_apps: bool`. Also `build_commands(migration_findings, per_app) -> list[str]`.

- [ ] Tests: section order 1–6 between `BEGIN;`/`COMMIT;`; missing tables ordered FK target first; add column uses extractor SQL; type change `USING`; NOT NULL change; DROP lines start with `--`; `fake` inserts unapplied rows, `leave` does not; ghost DELETE present; other-app DELETE only with option; NOT NULL-without-default WARNING comment; empty findings → script says "nothing to do".
- [ ] Implement, run, commit.

### Task 8: Loader + runner

**Files:** Create `validator/project_loader.py`, `validator/runner.py`, `tests/test_runner.py`.
**Produces:** `load_project(project) -> dict` (raises `ExtractorError(message, stderr)`); `git_info(path) -> (branch, commit)`; `run_batch(project, databases, options) -> uuid` creates one `ComparisonRun` per DB (status `ok`/`error`), summary `{"error":n,"warning":n,"info":n,"by_category":{...}}`.

- [ ] Tests: extractor failure → every run `error` with stderr; unreachable DB → that run error, other ok; successful run stores report/fix_sql (Postgres-backed).
- [ ] Implement, run, commit.

### Task 9: UI

**Files:** Create `validator/{forms,views,urls}.py`, `validator/templates/validator/{base,dashboard,project_list,project_form,database_list,database_form,batch,run_detail,run_list,confirm_delete}.html`, `validator/static/validator/{app.css,app.js}`, `tests/test_views.py`.
Routes per spec §9 plus `/projects/<id>/check/`, `/databases/<id>/test/`, `/runs/<id>/fix.sql`, `/runs/<id>/report.json`.

- [ ] Tests: every page 200; project/database create-edit-delete; password field blank on edit keeps stored password and is never in HTML; POST dashboard creates batch and redirects to `/batches/<uuid>/`; downloads have correct content types.
- [ ] Implement, run, commit.

### Task 10: End-to-end + README

**Files:** Create `tests/test_end_to_end.py`, `README.md`.

- [ ] Test: migrate sample project into fresh Postgres DB, introduce drift (drop column, add extra table, change a type, delete one migration row, insert a ghost row), run batch, assert expected findings; execute the generated fix SQL on the DB (test-only), re-run, assert zero schema errors and zero ghost/unapplied.
- [ ] README: install, run, configure, how to read results. Run full suite, commit, push.
