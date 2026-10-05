# Schema Sync Validator

A local Django web app that checks whether a Django branch and an
environment's PostgreSQL database agree, writes the SQL to make them agree,
and — on databases you explicitly enable — deletes records and cleans up old
rows with a cascade preview, typed confirmation, backup and restore script.

It answers, per environment (stage, prod-eu, prod-us, …):

- **Schema** — which tables, columns, types, nullability, foreign keys, unique
  constraints and indexes the branch's models need that the database lacks,
  and which the database has that the branch does not.
- **Migration history** — how `django_migrations` differs from the migration
  files in the branch: rows for migrations that aren't in the branch, rows for
  apps that aren't in the branch, migrations not applied yet (and whether
  `migrate` would fail on them), inconsistent history, apps that need a merge
  migration. Squashed migrations follow Django's own rules.
- **Model changes without a migration file** — what `makemigrations --check`
  would report.

For each database it generates a fix script (schema DDL + history rows) and
the equivalent `manage.py` commands. **It never runs them.** Comparisons and
the data browser use read-only sessions.

It also does:

- **Database vs database** — e.g. stage as the reference, prod-eu and prod-us
  as targets: schema and migration-history differences, no branch needed.
- **Changes since the previous run** of the same comparison (new / resolved),
  a **Re-run** button, and **ignore rules** for accepted drift.
- **Data browser** (read-only): tables, filtered rows, a row's parents and
  children. Secret-looking columns are masked.
- **Data operations** (only on databases with *writes enabled*): delete a
  record or any filtered set of rows with everything that cascades from it,
  **cleanup recipes** run in batches (e.g. execution history older than 90
  days), and **drop a column**.

Design: [`docs/superpowers/specs/2026-10-04-schema-sync-validator-design.md`](docs/superpowers/specs/2026-10-04-schema-sync-validator-design.md),
[`docs/superpowers/specs/2026-10-05-data-operations-and-improvements-design.md`](docs/superpowers/specs/2026-10-05-data-operations-and-improvements-design.md)

## Install and run

Python 3.11+.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate          # creates data/schemasync.sqlite3
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000.

Everything the tool stores lives in `data/` (git-ignored):
`schemasync.sqlite3` (projects, databases, runs, operations), `secret.key`
(encrypts saved database passwords — keep it; losing it means re-entering
passwords), `django_secret.txt` and `backups/` (rows removed by operations).
Set `SCHEMASYNC_DATA_DIR` to keep them elsewhere. Comparisons and operations
run in the background with a progress page; restarting the server marks
running jobs as interrupted.

## Use

1. **Projects → Add project.** The folder that contains your backend's
   `manage.py`, and the Python interpreter of the backend's venv (for example
   `/path/backend/.venv/bin/python`). The backend's dependencies must be
   installed in that venv. Settings module is read from `manage.py` if left
   blank. Put any environment variables the settings need in order to import
   (a dummy `SECRET_KEY`, feature flags, …) in *Extra environment variables*.
   Click **Check import** to confirm the tool can load the project.
2. **Databases → Add database** for each environment. A user with read access
   is enough. Leave the password blank to use `~/.pgpass`. **Test connection**
   checks it.
3. Check out the branch you want in the project folder, then on **Compare**
   pick the project and one or more databases and **Run comparison**.
4. The comparison page shows every database side by side. Open one for the
   findings (Schema / Migrations / Without migration) and the **Fix SQL** tab.

### Reading the findings

| mark | meaning |
|---|---|
| `+` | the branch needs it in the database |
| `−` | the database has it, the branch does not |
| `~` | it exists on both sides but differs |
| `!` | the migration history is broken |
| `?` | a model change has no migration file |

Severity: **error** breaks the deploy or the app, **warning** should be looked
at, **info** is expected drift (for example a migration not applied yet).

### The fix script

Sections, inside one transaction:

1. Missing tables (Django's own `CREATE TABLE`, then constraints and indexes)
2. Missing columns (Django's own `ADD COLUMN`; a warning when a NOT NULL column has no default)
3. Column type and NULL changes
4. Missing foreign keys, unique constraints and indexes
5. `django_migrations`: deletes rows for migrations that aren't in the branch and,
   with the default *fake-apply* strategy, inserts rows for the branch's migrations
   that aren't recorded (the schema SQL above already brings the database to the
   branch). Rows of apps not in the branch are kept unless you tick the option.
6. Extra tables and columns — always commented out.

Review it, then run it yourself, for example
`psql -v ON_ERROR_STOP=1 -f fix-prod-us-12.sql`.

## Deleting and cleaning up data

Open **Data**, pick the project whose models describe the database (its
`on_delete` rules decide the cascade — Django does not put `ON DELETE CASCADE`
in the database), pick a database and a table, filter, then **Preview delete**
(or open a row and **Preview delete of this row**).

The preview shows every table and row count the delete reaches, columns that
will be set to NULL, and **blockers** (PROTECT/RESTRICT rows, `SET_DEFAULT`,
foreign keys that exist only in the database). A database-only foreign key
can be ticked to be treated as cascade. Then:

1. **Rehearsal** — the delete runs in a transaction that is rolled back, so
   constraint and trigger errors show up before anything changes.
2. **Confirm** — type the phrase shown (e.g. `delete devices_customer 42 on
   prod-us`; pasting is disabled). On a `prod` database also tick the backup
   acknowledgement. Previews expire after 15 minutes.
3. **Execute** — in one transaction: the plan is computed again and must match
   the preview exactly (otherwise nothing changes); every affected row is
   written to `data/backups/op-<id>/`; then rows are nulled and deleted.
4. **Restore** — the operation page offers a restore script that re-inserts
   the rows (parents first) and restores nulled values. Run it with `psql` if
   you need to undo.

**Cleanup recipes** (`Operations → Cleanup recipes`) save a table + filters +
batch size. Running one previews the total and the first batch, then deletes
batch by batch (each batch its own backed-up transaction) with live progress;
it can be cancelled between batches. **Drop column** (from a table's danger
zone) shows the values that will be lost, indexes/constraints dropped with
it, warns if the branch's models still use the column, and refuses when a
view or foreign key depends on it.

Writes are **off by default** per database (`Databases → Edit → Writes
enabled`); you can give a separate write user there. Every operation is kept
in **Operations** with the typed confirmation, counts, SQL and backup.

## How it works

`validator/extractor.py` is run with **the backend's own interpreter** from the
project folder. It calls `django.setup()` with `DATABASES` replaced by a
PostgreSQL entry that is never connected to, then writes the models, the
migration graph, the pending model changes and the DDL Django would generate as
JSON. The tool reads the target database's catalog with psycopg in a
`default_transaction_read_only` session, diffs both sides and renders the
report. Importing the backend runs its settings and app code, as `manage.py`
would.

Data operations track rows by `ctid` in temporary tables inside one
transaction: a planner expands the delete round by round over model relations
(from the extractor) and database foreign keys, so cycles and rows reachable
by several paths are handled once.

Limits: PostgreSQL 14+ only; only the backend's `default` database alias is
compared; `post_delete` signals and file cleanup in your models do not run
when the tool deletes rows.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Integration tests need a local PostgreSQL (`SCHEMASYNC_TEST_PGHOST`,
`SCHEMASYNC_TEST_PGPORT`, `SCHEMASYNC_TEST_PGUSER`, `SCHEMASYNC_TEST_PGPASSWORD`;
defaults `127.0.0.1:5432`, `postgres`/`postgres`) and are skipped without one.
`tests/sample_project` is a small Django project used as the target.
