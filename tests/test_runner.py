import sys
from pathlib import Path

import pytest

from validator.models import ComparisonRun, DatabaseTarget, Project
from validator.runner import run_batch

SAMPLE = Path(__file__).resolve().parent / "sample_project"
OPTIONS = {"history_strategy": "fake", "include_other_apps": False}


def make_project(**overrides):
    fields = dict(name="sample", path=str(SAMPLE), python_path=sys.executable, extra_env="SAMPLE_SECRET_KEY=x")
    fields.update(overrides)
    return Project.objects.create(**fields)


def make_database(conninfo, name="stage", **overrides):
    db = DatabaseTarget(name=name, host=conninfo["host"], port=conninfo["port"], dbname=conninfo["dbname"],
                        user=conninfo["user"], **overrides)
    db.set_password(conninfo["password"])
    db.save()
    return db


@pytest.mark.django_db
def test_extractor_failure_marks_every_run_error(pg_conninfo):
    project = make_project(extra_env="")  # SAMPLE_SECRET_KEY missing -> settings import fails
    dbs = [make_database(pg_conninfo, "a"), make_database(pg_conninfo, "b")]
    batch = run_batch(project, dbs, OPTIONS)
    runs = ComparisonRun.objects.filter(batch_id=batch)
    assert runs.count() == 2
    for run in runs:
        assert run.status == "error"
        assert "KeyError: 'SAMPLE_SECRET_KEY'" in run.error


@pytest.mark.django_db
def test_unreachable_database_does_not_stop_others(pg_conninfo):
    project = make_project()
    bad = make_database({**pg_conninfo, "port": 1}, "unreachable")
    good = make_database(pg_conninfo, "good")
    batch = run_batch(project, [bad, good], OPTIONS)
    runs = {r.database_name: r for r in ComparisonRun.objects.filter(batch_id=batch)}
    assert runs["unreachable"].status == "error"
    assert "Cannot connect" in runs["unreachable"].error
    assert runs["good"].status == "ok"


@pytest.mark.django_db
def test_successful_run_stores_report_and_fix(pg_conninfo):
    batch = run_batch(make_project(), [make_database(pg_conninfo)], OPTIONS)
    run = ComparisonRun.objects.get(batch_id=batch)
    assert run.status == "ok", run.error
    categories = {f["category"] for f in run.report["schema"]}
    assert categories == {"table_missing"}
    assert {f["category"] for f in run.report["migrations"]} >= {"history_missing", "unapplied"}
    assert run.report["pending"][0]["message"] == "No migration file for: Add field note to order"
    assert run.summary["error"] > 0
    assert 'CREATE TABLE "catalog_product"' in run.fix_sql
    assert run.git_branch  # 'unknown' or a branch name


def test_missing_manage_py(tmp_path, db):
    from validator.project_loader import ExtractorError, load_project

    with pytest.raises(ExtractorError, match="No manage.py"):
        load_project(Project(name="x", path=str(tmp_path), python_path=sys.executable))
