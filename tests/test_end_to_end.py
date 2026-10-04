"""Migrate the sample project into a real database, drift it, fix it, check again."""
import os
import shutil
import subprocess
import sys

import pytest

from tests.conftest import pg_execute
from tests.test_runner import SAMPLE, make_database, make_project
from validator.models import ComparisonRun
from validator.runner import run_batch

pytestmark = pytest.mark.django_db

DRIFT = [
    'ALTER TABLE catalog_product DROP COLUMN sku',
    'ALTER TABLE catalog_category ALTER COLUMN name TYPE varchar(50)',
    'CREATE TABLE legacy_report (id int NOT NULL)',
    'ALTER TABLE catalog_tag ADD COLUMN old_flag boolean',
    "DROP INDEX catalog_product_created_77ea188a",
    "ALTER TABLE catalog_product DROP CONSTRAINT catalog_product_category_id_35bf920b_fk_catalog_category_id",
    "DELETE FROM django_migrations WHERE app = 'orders'",
    "INSERT INTO django_migrations (app, name, applied) VALUES ('catalog', '0003_feature_x', now())",
    "INSERT INTO django_migrations (app, name, applied) VALUES ('billing', '0001_initial', now())",
]


def migrate_sample(conninfo):
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "sampleproj.settings", "SAMPLE_SECRET_KEY": "x", "SAMPLE_DB_NAME": conninfo["dbname"],
           "SAMPLE_DB_HOST": conninfo["host"], "SAMPLE_DB_PORT": str(conninfo["port"]),
           "SAMPLE_DB_USER": conninfo["user"], "SAMPLE_DB_PASSWORD": conninfo["password"]}
    proc = subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=SAMPLE, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def run_psql(conninfo, script):
    proc = subprocess.run(
        ["psql", "-v", "ON_ERROR_STOP=1", "-h", conninfo["host"], "-p", str(conninfo["port"]),
         "-U", conninfo["user"], "-d", conninfo["dbname"], "-f", "-"],
        input=script, env={**os.environ, "PGPASSWORD": conninfo["password"]},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def found(findings):
    return {(f["category"], f["severity"], f["table"] or f["app"], f["column"]) for f in findings}


@pytest.mark.skipif(not shutil.which("psql"), reason="psql not installed")
def test_drift_fix_and_recheck(pg_conninfo):
    migrate_sample(pg_conninfo)
    pg_execute(pg_conninfo, DRIFT)
    project, database = make_project(), make_database(pg_conninfo)
    options = {"history_strategy": "fake", "include_other_apps": False}

    run = ComparisonRun.objects.get(batch_id=run_batch(project, [database], options))
    assert run.status == "ok", run.error
    schema = found(run.report["schema"])
    assert {
        ("column_missing", "error", "catalog_product", "sku"),
        ("column_missing", "error", "orders_order", "note"),  # model change without migration
        ("type_mismatch", "error", "catalog_category", "name"),
        ("table_extra", "warning", "legacy_report", ""),
        ("column_extra", "warning", "catalog_tag", "old_flag"),
        ("index_missing", "info", "catalog_product", "created"),
        ("fk_missing", "warning", "catalog_product", "category_id"),
    } <= schema
    migrations = found(run.report["migrations"])
    assert {
        ("ghost", "error", "catalog", "0003_feature_x"),
        ("other_app_rows", "warning", "billing", ""),
        ("unapplied", "error", "orders", "0001_initial"),  # its table already exists
    } <= migrations
    assert run.report["pending"][0]["data"]["operation"] == "Add field note to order"

    run_psql(pg_conninfo, run.fix_sql)

    again = ComparisonRun.objects.get(batch_id=run_batch(project, [database], options))
    errors = [f for f in again.report["schema"] + again.report["migrations"] if f["severity"] == "error"]
    assert errors == []
    categories = {f["category"] for f in again.report["schema"] + again.report["migrations"]}
    assert categories <= {"table_extra", "column_extra", "other_app_rows"}  # kept on purpose
    assert "-- DROP TABLE \"legacy_report\";" in again.fix_sql
