import sys
from pathlib import Path

import pytest
from django.urls import reverse

from tests.conftest import pg_execute
from validator.db_compare import db_as_models, diff_history_sets
from validator.db_inspector import inspect_database
from validator.models import ComparisonRun, DatabaseTarget, IgnoreRule, Project
from validator.runner import changes_since, previous_run, run_batch
from validator.schema_diff import diff_schema

pytestmark = pytest.mark.django_db
SAMPLE = Path(__file__).resolve().parent / "sample_project"
OPTIONS = {"history_strategy": "fake", "include_other_apps": False}


@pytest.fixture
def project():
    return Project.objects.create(name="sample", path=str(SAMPLE), python_path=sys.executable,
                                  extra_env="SAMPLE_SECRET_KEY=x")


def target(conninfo, name):
    db = DatabaseTarget(name=name, host=conninfo["host"], port=conninfo["port"], dbname=conninfo["dbname"],
                        user=conninfo["user"])
    db.set_password(conninfo["password"])
    db.save()
    return db


def found(findings):
    return {(f["category"], f["severity"], f["table"] or f["app"], f["column"]) for f in findings}


def test_db_as_models_matches_itself(sample_db):
    info = inspect_database(sample_db, "public")
    assert diff_schema(db_as_models(info), info) == []


def test_diff_history_sets():
    ref = [{"app": "shop", "name": "0001"}, {"app": "shop", "name": "0002"}]
    tgt = [{"app": "shop", "name": "0001"}, {"app": "billing", "name": "0001"}]
    assert found(diff_history_sets(ref, tgt)) == {
        ("history_only_reference", "warning", "shop", "0002"),
        ("history_only_target", "warning", "billing", "0001"),
    }


def test_db_vs_db_comparison(sample_db, sample_db2):
    pg_execute(sample_db2, [
        "ALTER TABLE devices_device DROP COLUMN hostname",
        "CREATE TABLE legacy_report (id int)",
        "DELETE FROM django_migrations WHERE app = 'orders'",
    ])
    ref, tgt = target(sample_db, "stage"), target(sample_db2, "prod-us")
    batch = run_batch(None, [tgt], {}, kind="db", reference=ref)
    run = ComparisonRun.objects.get(batch_id=batch)
    assert run.status == "ok", run.error
    assert run.kind == "db" and run.reference_name == "stage"
    assert {("column_missing", "error", "devices_device", "hostname"),
            ("table_extra", "warning", "legacy_report", "")} <= found(run.report["schema"])
    assert ("history_only_reference", "warning", "orders", "0001_initial") in found(run.report["migrations"])
    assert run.fix_sql == ""


def test_ignore_rules_exclude_findings(sample_db, project):
    pg_execute(sample_db, "CREATE TABLE legacy_report (id int)")
    db = target(sample_db, "stage")
    first = ComparisonRun.objects.get(batch_id=run_batch(project, [db], OPTIONS))
    assert ("table_extra", "warning", "legacy_report", "") in found(first.report["schema"])

    IgnoreRule.objects.create(database=db, category="table_extra", pattern="legacy_*", note="old reporting")
    run = ComparisonRun.objects.get(batch_id=run_batch(project, [db], OPTIONS))
    assert ("table_extra", "warning", "legacy_report", "") not in found(run.report["schema"])
    assert [f["table"] for f in run.report["ignored"]] == ["legacy_report"]
    assert run.summary["warning"] == first.summary["warning"] - 1
    assert "legacy_report" not in run.fix_sql


def test_changes_since_previous_run(sample_db, project):
    db = target(sample_db, "stage")
    first = ComparisonRun.objects.get(batch_id=run_batch(project, [db], OPTIONS))
    pg_execute(sample_db, ["CREATE TABLE legacy_report (id int)",
                           "ALTER TABLE orders_order ADD COLUMN note text NOT NULL DEFAULT ''"])
    second = ComparisonRun.objects.get(batch_id=run_batch(project, [db], OPTIONS))
    assert previous_run(second) == first
    changes = changes_since(second, first)
    assert "table_extra|legacy_report|" in changes["new"]
    assert [(f["table"], f["column"]) for f in changes["resolved"]] == [("orders_order", "note")]


def test_run_page_shows_changes_and_ignore_link(client, sample_db, project):
    db = target(sample_db, "stage")
    run_batch(project, [db], OPTIONS)
    pg_execute(sample_db, "CREATE TABLE legacy_report (id int)")
    run = ComparisonRun.objects.get(batch_id=run_batch(project, [db], OPTIONS))
    body = client.get(reverse("run_detail", args=[run.pk])).content.decode()
    assert "Since the previous run" in body and "<strong>1 new</strong>" in body
    assert reverse("ignore_create") in body


def test_dashboard_starts_db_mode_and_validates(client, sample_db, sample_db2):
    ref, tgt = target(sample_db, "stage"), target(sample_db2, "prod-us")
    response = client.post(reverse("dashboard"), {"mode": "db", "databases": [tgt.pk], "history_strategy": "fake"})
    assert "Pick the reference database" in response.content.decode()
    response = client.post(reverse("dashboard"), {"mode": "branch", "databases": [tgt.pk], "history_strategy": "fake"})
    assert "Pick a project" in response.content.decode()
    response = client.post(reverse("dashboard"), {"mode": "db", "reference": ref.pk, "databases": [tgt.pk],
                                                  "history_strategy": "fake"})
    run = ComparisonRun.objects.get()
    assert response["Location"] == reverse("batch_detail", args=[run.batch_id])
    assert run.kind == "db"


def test_rerun_batch(client, sample_db, project):
    db = target(sample_db, "stage")
    batch = run_batch(project, [db], OPTIONS)
    response = client.post(reverse("batch_rerun", args=[batch]))
    assert ComparisonRun.objects.count() == 2
    new = ComparisonRun.objects.exclude(batch_id=batch).get()
    assert response["Location"] == reverse("batch_detail", args=[new.batch_id])
    assert new.options["history_strategy"] == "fake"


def test_ignore_rule_views(client, sample_db):
    db = target(sample_db, "stage")
    url = reverse("ignore_create") + f"?database={db.pk}&category=table_extra&pattern=legacy_report"
    body = client.get(url).content.decode()
    assert 'value="legacy_report"' in body
    response = client.post(reverse("ignore_create"), {"database": db.pk, "category": "table_extra",
                                                      "pattern": "legacy_*", "note": "old"})
    assert response.status_code == 302
    assert "legacy_*" in client.get(reverse("ignore_list")).content.decode()
    rule = IgnoreRule.objects.get()
    client.post(reverse("ignore_delete", args=[rule.pk]))
    assert not IgnoreRule.objects.exists()
