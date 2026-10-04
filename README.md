# Schema Sync Validator

A local Django web app that checks whether a Django branch and an
environment's PostgreSQL database agree, and writes the SQL to make them agree.

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
the equivalent `manage.py` commands. **It never runs them.** Target databases
are opened in read-only sessions.

Design: [`docs/superpowers/specs/2026-10-04-schema-sync-validator-design.md`](docs/superpowers/specs/2026-10-04-schema-sync-validator-design.md)

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
`schemasync.sqlite3` (projects, databases, run history), `secret.key` (encrypts
saved database passwords — keep it; losing it means re-entering passwords) and
`django_secret.txt`. Set `SCHEMASYNC_DATA_DIR` to keep them elsewhere.

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

## How it works

`validator/extractor.py` is run with **the backend's own interpreter** from the
project folder. It calls `django.setup()` with `DATABASES` replaced by a
PostgreSQL entry that is never connected to, then writes the models, the
migration graph, the pending model changes and the DDL Django would generate as
JSON. The tool reads the target database's catalog with psycopg in a
`default_transaction_read_only` session, diffs both sides and renders the
report. Importing the backend runs its settings and app code, as `manage.py`
would.

Limits: PostgreSQL only; only the backend's `default` database alias is
compared.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Integration tests need a local PostgreSQL (`SCHEMASYNC_TEST_PGHOST`,
`SCHEMASYNC_TEST_PGPORT`, `SCHEMASYNC_TEST_PGUSER`, `SCHEMASYNC_TEST_PGPASSWORD`;
defaults `127.0.0.1:5432`, `postgres`/`postgres`) and are skipped without one.
`tests/sample_project` is a small Django project used as the target.
