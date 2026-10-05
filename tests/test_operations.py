import sys
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from django.utils import timezone

from tests.conftest import pg_execute
from validator import operations
from validator.jobs import recover_interrupted, start_job
from validator.models import CleanupRecipe, DataOperation, DatabaseTarget, Job, Project

pytestmark = pytest.mark.django_db
SAMPLE = Path(__file__).resolve().parent / "sample_project"


@pytest.fixture
def project():
    return Project.objects.create(name="sample", path=str(SAMPLE), python_path=sys.executable,
                                  extra_env="SAMPLE_SECRET_KEY=x")


def make_target(conninfo, writes=True, environment="stage"):
    db = DatabaseTarget(name=environment, host=conninfo["host"], port=conninfo["port"], dbname=conninfo["dbname"],
                        user=conninfo["user"], writes_enabled=writes, environment=environment)
    db.set_password(conninfo["password"])
    db.save()
    return db


def new_op(db, project, **kw):
    fields = dict(kind="delete", database=db, database_name=db.name, environment=db.environment, project=project,
                  root_table="devices_customer", filters=[{"column": "id", "op": "eq", "value": "1"}])
    fields.update(kw)
    return DataOperation.objects.create(**fields)


def count(conninfo, sql_text):
    with psycopg.connect(**conninfo) as conn:
        return conn.execute(sql_text).fetchone()[0]


def test_preview_delete(sample_db, project):
    op = new_op(make_target(sample_db), project)
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.status == "planned"
    assert op.plan["total_rows"] == 10
    assert op.plan["signature"]["delete"]["devices_device"] == 3
    assert op.rehearsal["ok"] is True
    assert operations.confirmation_phrase(op) == "delete devices_customer 1 on stage"
    assert count(sample_db, "SELECT count(*) FROM devices_customer") == 4


def test_preview_without_writes_skips_rehearsal(sample_db, project):
    op = new_op(make_target(sample_db, writes=False), project)
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.status == "planned" and op.plan["total_rows"] == 10
    assert op.rehearsal == {"skipped": "Writes are not enabled for this database"}


def test_preview_with_bad_filter_value(sample_db, project):
    op = new_op(make_target(sample_db), project, filters=[{"column": "id", "op": "eq", "value": "abc"}])
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.status == "error"
    assert "not valid" in op.error


def test_filtered_delete_phrase_mentions_count(sample_db, project):
    op = new_op(make_target(sample_db), project, root_table="devices_execution",
                filters=[{"column": "status", "op": "eq", "value": "ok"}])
    operations.preview_operation(op)
    assert operations.confirmation_phrase(op) == "delete 2 rows from devices_execution on stage"


def test_check_executable(sample_db, project):
    db = make_target(sample_db, environment="prod")
    op = new_op(db, project)
    operations.preview_operation(op)
    phrase = operations.confirmation_phrase(op)
    assert operations.check_executable(op, phrase, backup_ack=True) == []
    assert "Type the confirmation" in " ".join(operations.check_executable(op, "delete it", backup_ack=True))
    assert "backup" in " ".join(operations.check_executable(op, phrase, backup_ack=False))
    op.planned_at = timezone.now() - timedelta(minutes=16)
    assert "older than 15 minutes" in " ".join(operations.check_executable(op, phrase, backup_ack=True))
    db.writes_enabled = False
    db.save()
    op.refresh_from_db()
    assert "not enabled" in " ".join(operations.check_executable(op, phrase, backup_ack=True))


def test_blocked_operation_cannot_execute(sample_db, project):
    op = new_op(make_target(sample_db), project, filters=[{"column": "id", "op": "eq", "value": "3"}])
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.status == "blocked"
    assert "blocked" in " ".join(operations.check_executable(op, operations.confirmation_phrase(op), True))


def test_run_delete_through_job(sample_db, project):
    op = new_op(make_target(sample_db), project)
    operations.preview_operation(op)
    job = start_job("delete", "Delete", lambda job: operations.run_operation(op, job))
    op.refresh_from_db()
    assert job.status == "done", job.log
    assert op.status == "done"
    assert op.counts["deleted"]["devices_device"] == 3
    assert Path(op.backup_dir, "manifest.json").exists()
    assert "DELETE FROM" in op.sql_log
    assert job.result_url == f"/ops/{op.pk}/"
    assert count(sample_db, "SELECT count(*) FROM devices_customer") == 3


def add_old_executions(conninfo, n=5):
    pg_execute(conninfo, f"INSERT INTO devices_execution (id, device_id, customer_id, started, status) "
                         f"SELECT 1000 + g, 20, 2, now() - interval '400 days', 'ok' FROM generate_series(1, {n}) g")


def cleanup_op(db, project, batch_size=2):
    recipe = CleanupRecipe.objects.create(name="old executions", project=project, root_table="devices_execution",
                                          filters=[{"column": "started", "op": "older_than_days", "value": "90"}],
                                          batch_size=batch_size)
    return new_op(db, project, kind="cleanup", recipe=recipe, root_table=recipe.root_table, filters=recipe.filters,
                  batch_size=recipe.batch_size)


def test_cleanup_in_batches(sample_db, project):
    add_old_executions(sample_db)
    op = cleanup_op(make_target(sample_db), project)
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.plan["root_total"] == 7  # 100, 102 + 5 new
    assert op.plan["root_count"] == 2  # first batch planned in detail
    assert operations.confirmation_phrase(op) == "cleanup old executions on stage"
    job = start_job("cleanup", "Cleanup", lambda job: operations.run_operation(op, job))
    op.refresh_from_db()
    assert job.status == "done", job.log
    assert op.counts["deleted"]["devices_execution"] == 7
    assert op.counts["batches"] == 4
    assert len(list(Path(op.backup_dir).glob("batch-*/manifest.json"))) == 4
    assert count(sample_db, "SELECT count(*) FROM devices_execution") == 1


def test_cancel_between_batches(sample_db, project, monkeypatch):
    add_old_executions(sample_db)
    op = cleanup_op(make_target(sample_db), project)
    operations.preview_operation(op)
    real_execute = operations.execute

    def execute_then_cancel(*args, **kwargs):
        result = real_execute(*args, **kwargs)
        Job.objects.filter(pk=op.job_id).update(cancel_requested=True)
        return result

    monkeypatch.setattr(operations, "execute", execute_then_cancel)
    job = start_job("cleanup", "Cleanup", lambda job: operations.run_operation(op, job))
    op.refresh_from_db()
    assert job.status == "cancelled"
    assert op.status == "cancelled"
    assert op.counts["batches"] == 1
    assert count(sample_db, "SELECT count(*) FROM devices_execution") == 6


def test_failure_marks_operation_failed(sample_db, project):
    op = new_op(make_target(sample_db), project)
    operations.preview_operation(op)
    pg_execute(sample_db, [
        "CREATE FUNCTION no_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'frozen'; END $$",
        "CREATE TRIGGER freeze_devices BEFORE DELETE ON devices_device FOR EACH ROW EXECUTE FUNCTION no_delete()",
    ])
    job = start_job("delete", "Delete", lambda job: operations.run_operation(op, job))
    op.refresh_from_db()
    assert job.status == "failed" and op.status == "failed"
    assert "frozen" in op.error
    assert count(sample_db, "SELECT count(*) FROM devices_customer") == 4


def test_drop_column_operation(sample_db, project):
    pg_execute(sample_db, "ALTER TABLE devices_device ADD COLUMN legacy_code text")
    op = new_op(make_target(sample_db), project, kind="drop_column", root_table="devices_device",
                column="legacy_code", filters=[])
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.status == "planned" and op.plan["in_model"] is False
    assert operations.confirmation_phrase(op) == "drop devices_device.legacy_code on stage"
    job = start_job("drop_column", "Drop", lambda job: operations.run_operation(op, job))
    op.refresh_from_db()
    assert job.status == "done", job.log
    assert op.counts == {"rows_backed_up": 4}


def test_recover_interrupted():
    Job.objects.create(kind="compare", status="running")
    recover_interrupted()
    assert Job.objects.get().status == "interrupted"


def test_extraction_cache_hit_and_miss(project, monkeypatch):
    from validator import project_loader

    calls = []
    real = project_loader.load_project
    monkeypatch.setattr(project_loader, "load_project", lambda p: calls.append(1) or real(p))
    first = project_loader.load_project_cached(project)
    assert project_loader.load_project_cached(project) == first
    assert len(calls) == 1                                   # second call served from the snapshot
    project.extra_env += "\nFEATURE_X=1"
    project.save()
    project_loader.load_project_cached(project)
    assert len(calls) == 2                                   # project settings changed -> re-extract
    project_loader.load_project_cached(project, force=True)
    assert len(calls) == 3


def test_extraction_cache_skipped_outside_git(tmp_path, monkeypatch):
    from validator import project_loader

    outside = Project.objects.create(name="nogit", path=str(tmp_path), python_path=sys.executable)
    calls = []
    monkeypatch.setattr(project_loader, "load_project", lambda p: calls.append(1) or {"x": 1})
    project_loader.load_project_cached(outside)
    project_loader.load_project_cached(outside)
    assert len(calls) == 2
