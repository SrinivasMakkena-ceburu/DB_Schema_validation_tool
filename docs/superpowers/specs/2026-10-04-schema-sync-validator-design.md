# Schema Sync Validator — Design

Date: 2026-10-04
Status: Draft for review

## 1. Problem

The backend is a Django project. Many developers change models quickly, and we do
not accept their migration files — migrations are managed centrally. We run several
environments (stage, prod-eu, prod-us, …) that carry different feature sets per the
release plan, so their schemas and their `django_migrations` history differ, and we
do not keep full migration history until the product is stable.

Before deploying a branch to an environment we need to know, and fix:

1. Which tables / columns / constraints the branch's models need that the
   environment's database does not have (and vice versa).
2. How the environment's `django_migrations` history differs from the migration
   files in the branch, so history can be aligned and `migrate` will behave.
3. Which model changes in the branch have no migration file at all.

## 2. Goals and non-goals

Goals

- A single local Django web app. Everything — projects, databases, runs — is
  configured and viewed from the browser and stored in a local SQLite file.
- Read-only against target databases. The tool never executes DDL or DML on them.
- Generates the fix (SQL + `manage.py` commands) for a human to review and run.
- Compare one branch against several environments side by side.

Non-goals

- Applying fixes, writing migration files, or managing git branches.
- Engines other than PostgreSQL.
- Multi-user access, login, or deployment as a shared service (runs on
  `127.0.0.1`).
- Multiple Django database aliases / routers in the target project — only the
  `default` alias is modelled.

## 3. Shape

One Django project, one app. No separate frontend, no REST API, no task queue.
Pages are server-rendered templates with a little vanilla JS (tabs, filtering,
copy-to-clipboard).

```
manage.py
requirements.txt                Django 5.2, psycopg[binary] 3, cryptography
schemasync/                     project: settings, urls, wsgi
validator/                      the app
  models.py                     Project, DatabaseTarget, ComparisonRun
  forms.py, views.py, urls.py
  templates/validator/…
  static/validator/…            one CSS file, one JS file
  crypto.py                     encrypt/decrypt stored DB passwords
  extractor.py                  standalone script, run with the TARGET project's python
  project_loader.py             runs extractor.py as a subprocess, parses its JSON
  db_inspector.py               read-only psycopg introspection of a target DB
  pgtypes.py                    type-name normalisation
  schema_diff.py                models vs DB tables/columns/constraints
  migration_diff.py             disk migrations vs django_migrations
  fix_sql.py                    builds the fix script
  runner.py                     orchestrates one comparison batch
data/                           git-ignored: schemasync.sqlite3, secret.key
tests/
  sample_project/               small Django project used as a fixture
```

`extractor.py` is the only piece that cannot run inside the tool's own process:
it must import the backend's code with the backend's own interpreter and
dependencies. The tool starts it as a short-lived child process and reads JSON
from its stdout. It is a file in the app, not a separate service.

## 4. Stored data (local SQLite)

**Project** — a local checkout of the backend.

| field | notes |
|---|---|
| name | unique, e.g. "drfbackend (local)" |
| path | absolute path to the folder containing `manage.py` |
| python_path | the backend venv's interpreter, e.g. `/…/venv/bin/python` |
| settings_module | e.g. `config.settings`; defaulted from `manage.py` if blank |
| extra_env | text, `KEY=VALUE` per line — whatever the settings need to import (dummy `SECRET_KEY`, feature flags, …) |

**DatabaseTarget** — one environment's database.

| field | notes |
|---|---|
| name | unique, e.g. "prod-us" |
| host, port, dbname, user | |
| password | stored encrypted (Fernet); blank means "use `~/.pgpass`" |
| sslmode | default `prefer` |
| schema | default `public` |
| notes | free text (release / feature set) |

**ComparisonRun** — one project × one database comparison, kept as history.

| field | notes |
|---|---|
| batch_id | UUID shared by runs started together (matrix page) |
| project, database | FKs (`SET_NULL`; names are also copied so history survives deletion) |
| git_branch, git_commit | read from the project folder with `git rev-parse` at run time |
| started_at, duration_ms, status | status: `ok` / `error` |
| error | text (extractor stderr, connection error, …) |
| summary | JSON counts per category and severity |
| report | JSON — full findings |
| fix_sql | text |

Password encryption key: `data/secret.key`, generated on first start. Losing it
means re-entering passwords; it is never committed. The UI never displays a stored
password back.

## 5. Extractor (runs in the backend's venv)

Invocation: `<python_path> <tool>/validator/extractor.py --settings <module>`
with `cwd=path`, `sys.path` including `path`, env = current env + `extra_env` +
`DJANGO_SETTINGS_MODULE`. Timeout 180 s.

Before `django.setup()` it replaces `settings.DATABASES` with a single
`default` PostgreSQL entry pointing at a non-existent host. The connection is
never opened; it only gives Django a PostgreSQL backend for `db_type()` and the
schema editor.

It prints one JSON document:

- `django_version`, `apps` (installed app labels)
- `models[]` — every concrete model including auto-created M2M through tables:
  `app_label`, `model`, `db_table`, `managed`, `columns[]`
  (`name`, `db_type`, `null`, `primary_key`, `unique`, `fk: {table, column} | null`,
  `db_constraint`), `unique_sets[]` (column lists from `unique=True`,
  `unique_together`, unconditional `UniqueConstraint`), `index_sets[]`
  (column lists from `db_index`, `index_together`, `Meta.indexes`),
  `create_sql[]` (schema editor `create_model`, `collect_sql=True`, deferred SQL
  included), `add_column_sql{column: [stmts]}` (schema editor `add_field` per
  local concrete field).
  Proxy models are skipped; unmanaged models are included with `managed=false`.
- `migrations` — from `MigrationLoader(None, ignore_no_migrations=True)`:
  per app a list of `{name, dependencies[[app,name]], replaces[[app,name]]}`,
  and `leaf_nodes` per app.
- `pending_changes` — `MigrationAutodetector(loader.project_state(),
  ProjectState.from_apps(apps)).changes(graph=loader.graph)`, reduced to
  `{app: [operation.describe(), …]}`. This is what `makemigrations --check`
  would report.

Any exception → non-zero exit with the traceback on stderr, which the UI shows
verbatim.

## 6. DB inspector (runs in the tool)

psycopg 3 connection with `options='-c default_transaction_read_only=on
-c statement_timeout=30000'`, `connect_timeout=10`. Queries only `pg_catalog`
and `django_migrations` for the configured schema:

- base tables and partitioned tables (views are listed separately, not diffed)
- columns: name, `format_type(atttypid, atttypmod)`, not-null, identity/default
- primary key, unique constraints and unique indexes (as column lists)
- foreign keys: column → referenced table/column
- non-unique indexes (as column lists)
- `django_migrations` rows (`app`, `name`, `applied`); a missing table means
  "no history" and is itself reported

## 7. Comparisons

### 7.1 Type normalisation (`pgtypes.py`)

Both sides are reduced to one canonical spelling before comparing:
`character varying(n)`→`varchar(n)`, `character(n)`→`char(n)`,
`timestamp with time zone`→`timestamptz`,
`timestamp without time zone`→`timestamp`, `time without time zone`→`time`,
`serial`→`integer`, `bigserial`→`bigint`, `smallserial`→`smallint`,
whitespace in `numeric(p, s)` removed, array suffix `[]` kept. Unknown names are
compared as lower-cased text.

### 7.2 Schema diff (`schema_diff.py`)

Expected tables = models' `db_table`s. Actual = DB tables in the schema.
Columns, unique sets, FKs and indexes are matched by **column set**, not by
constraint name, because Django's generated names are hashes and differ across
environments.

| finding | severity |
|---|---|
| table missing (managed model) | error |
| table missing (unmanaged model) | info |
| table in DB, no model in this branch | warning (likely another feature/branch) |
| column missing | error |
| column in DB, not in model, NOT NULL without default | error (Django inserts will fail) |
| column in DB, not in model, otherwise | warning |
| type mismatch | error |
| model NOT NULL, DB nullable | warning |
| model nullable, DB NOT NULL | error |
| primary key column differs | error |
| FK missing (`db_constraint=True`) | warning |
| FK points at a different table | error |
| unique set missing | warning |
| index set missing | info |

Tables of installed contrib apps (`auth_*`, `django_content_type`, …) are models
and are compared like any other. `django_migrations` has no model; it is excluded
from the table diff and handled in §7.3.

### 7.3 Migration history diff (`migration_diff.py`)

D = migrations on disk, A = rows in `django_migrations`. A squashed migration on
disk counts as applied when all of its `replaces` are in A (Django's rule).

| finding | severity |
|---|---|
| row in A for an app not installed in this branch | warning ("other feature") |
| row in A, app installed, migration not on disk (ghost) | error |
| on disk, not in A (unapplied) | info, or error if its model tables already exist (migrate would fail with "already exists") |
| applied migration whose dependency is not applied (inconsistent history) | error |
| app has more than one leaf on disk (needs a merge migration) | error |

Per app the report shows: latest on disk (leaf), latest recorded in DB (highest
`id`), and the counts above.

### 7.4 Pending model changes

Listed per app from the extractor's `pending_changes`. Severity: warning. No fix
is generated — these need a migration file, which is created outside the tool.

## 8. Fix script (`fix_sql.py`)

One `.sql` text per run, shown in the UI, downloadable, and stored on the run.
Never executed by the tool. Structure:

```
-- Schema Sync fix: <project> @ <branch> (<commit>) → <database>  generated <ts>
-- REVIEW BEFORE RUNNING. Generated, not executed.
BEGIN;
-- 1. Missing tables        (extractor create_sql, ordered so FK targets come first;
--                           deferred FK/index statements after all CREATEs)
-- 2. Missing columns       (extractor add_column_sql)
-- 3. Column changes        ALTER COLUMN … TYPE <t> USING <c>::<t>;  SET/DROP NOT NULL
-- 4. Missing FKs / uniques (ALTER TABLE … ADD CONSTRAINT …)
-- 5. Migration history     DELETE ghost rows; INSERT unapplied rows (fake-apply)
-- 6. Extras (commented out) -- DROP TABLE / DROP COLUMN, never active
COMMIT;
```

Options chosen per run on the New Comparison form:

- **History strategy**: "fake-apply all branch migrations" (default; assumes
  sections 1–4 bring the schema to the branch state) or "leave unapplied for
  `migrate`" (section 5 only deletes ghosts).
- **Include rows for apps not in this branch**: off by default (they belong to
  other features and are kept).

Statements carrying risk get an inline `-- WARNING:` comment: adding a NOT NULL
column without a default to a non-empty table, a type change that may fail the
cast, deleting history rows.

Equivalent commands are listed under the SQL for teams preferring them:
`python manage.py migrate --prune` (deletes ghost rows; Django ≥ 4.1) and
`python manage.py migrate <app> <leaf> --fake` per app.

## 9. Pages

| URL | page |
|---|---|
| `/` | Dashboard: New Comparison form (project, one or more databases, options) + recent runs |
| `/projects/` | list, add, edit, delete; **Check** runs the extractor and shows model/migration counts or the error |
| `/databases/` | list, add, edit, delete; **Test connection** |
| `/batches/<uuid>/` | Matrix: one column per database, rows = finding categories, cells = counts by severity, linking to each report |
| `/runs/<id>/` | Report with tabs: Summary, Schema, Migrations, Pending changes, Fix SQL; filter by severity/app/text; download `.sql` and `.json` |
| `/runs/` | Run history, filter by project/database |

A comparison runs synchronously in the request: the extractor runs once per
batch and its output is reused for each selected database; databases are
inspected one after another. A failure on one database records an `error` run
and the others continue.

## 10. Error handling

- Extractor not found / interpreter missing / import error / timeout → run
  status `error`, stderr shown in a `<pre>` on the report.
- DB unreachable, auth failure, schema missing → that database's run is `error`
  with the message; other databases still run.
- `git` missing or folder not a repo → branch/commit shown as `unknown`.
- Missing `secret.key` with stored passwords → passwords can't be decrypted;
  the database page asks for them to be re-entered.

## 11. Security

- Runs with `manage.py runserver 127.0.0.1:8000`; `ALLOWED_HOSTS` is
  `localhost`/`127.0.0.1`. No login.
- Target DB sessions are read-only at the server
  (`default_transaction_read_only=on`); the tool contains no code path that
  executes generated SQL.
- Stored passwords are encrypted at rest; `data/` is git-ignored.
- Running the extractor executes the backend's own code (settings and app
  imports) — the same trust as running its `manage.py`.

## 12. Testing

pytest + pytest-django.

- Unit (no DB): `pgtypes` normalisation table; `schema_diff` and
  `migration_diff` over hand-written JSON inputs covering every finding row in
  §7; `fix_sql` output for each section and option; password encryption round
  trip.
- Extractor: run against `tests/sample_project` (two apps, an FK, an M2M, a
  `unique_together`, an index, a squashed migration, one model change with no
  migration) and assert the JSON.
- Integration (skipped if no PostgreSQL available): create a database, apply the
  sample project's migrations, then introduce drift (drop a column, add an extra
  table, change a type, delete and add `django_migrations` rows); run the full
  comparison and assert the findings; apply the generated fix script and assert a
  second comparison reports no schema errors and no ghost/unapplied rows.
- View smoke tests: each page renders; forms create/edit/delete; a comparison
  POST creates a batch and redirects to the matrix.
