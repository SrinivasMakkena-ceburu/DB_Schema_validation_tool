import sys
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from django.urls import reverse
from django.utils import timezone

from validator.models import CleanupRecipe, DataOperation, DatabaseTarget, Job, Project

pytestmark = pytest.mark.django_db
SAMPLE = Path(__file__).resolve().parent / "sample_project"


@pytest.fixture
def project():
    return Project.objects.create(name="sample", path=str(SAMPLE), python_path=sys.executable,
                                  extra_env="SAMPLE_SECRET_KEY=x")


def target(conninfo, writes=True, environment="stage", name=None):
    db = DatabaseTarget(name=name or environment, host=conninfo["host"], port=conninfo["port"],
                        dbname=conninfo["dbname"], user=conninfo["user"], writes_enabled=writes,
                        environment=environment)
    db.set_password(conninfo["password"])
    db.save()
    return db


@pytest.fixture
def stage(sample_db):
    return target(sample_db)


def rows(conninfo, query):
    with psycopg.connect(**conninfo) as conn:
        return conn.execute(query).fetchone()[0]


def use_project(client, project):
    client.post(reverse("data_project"), {"project": project.pk, "next": "/data/"})


def test_data_home_and_tables(client, stage):
    assert stage.name in client.get(reverse("data_home")).content.decode()
    body = client.get(reverse("data_tables", args=[stage.pk])).content.decode()
    assert "devices_customer" in body and "devices_execution" in body


def test_table_rows_filters_and_masking(client, stage):
    url = reverse("data_table", args=[stage.pk, "devices_operator"])
    body = client.get(url).content.decode()
    assert "a@acme" in body and "pw1" not in body and "••••" in body
    assert "pw1" in client.get(url + "?reveal=1").content.decode()
    body = client.get(url + "?f_col=email&f_op=contains&f_val=other").content.decode()
    assert "b@other" in body and "a@acme" not in body


def test_bad_filter_value_shows_error(client, stage):
    body = client.get(reverse("data_table", args=[stage.pk, "devices_device"]) +
                      "?f_col=id&f_op=eq&f_val=abc").content.decode()
    assert "not valid" in body


def test_unknown_table_404(client, stage):
    assert client.get(reverse("data_table", args=[stage.pk, "x'; DROP TABLE y;--"])).status_code == 404


def test_row_page_shows_parents_and_children(client, stage, project):
    use_project(client, project)
    body = client.get(reverse("data_row", args=[stage.pk, "devices_customer", "1"])).content.decode()
    assert "devices_device" in body            # children with counts
    assert "devices_note" in body              # generic relation
    body = client.get(reverse("data_row", args=[stage.pk, "devices_device", "11"])).content.decode()
    assert reverse("data_row", args=[stage.pk, "devices_customer", "1"]) in body  # parent link


def start_delete(client, db, table="devices_customer", filters=(("id", "eq", "1"),)):
    data = {"kind": "delete", "database": db.pk, "table": table,
            "f_col": [f[0] for f in filters], "f_op": [f[1] for f in filters], "f_val": [f[2] for f in filters]}
    response = client.post(reverse("op_new"), data)
    return response, DataOperation.objects.order_by("-pk").first()


def test_preview_delete_shows_danger_panel(client, stage, project):
    use_project(client, project)
    response, op = start_delete(client, stage)
    assert response.status_code == 302
    assert op.status == "planned"
    body = client.get(reverse("op_detail", args=[op.pk])).content.decode()
    assert "delete devices_customer 1 on stage" in body
    assert "devices_device" in body and "danger" in body


def test_delete_requires_a_filter(client, stage):
    response = client.post(reverse("op_new"), {"kind": "delete", "database": stage.pk,
                                               "table": "devices_customer"}, follow=True)
    assert "at least one filter" in response.content.decode()
    assert not DataOperation.objects.exists()


def execute(client, op, phrase, backup_ack=False):
    data = {"confirmation": phrase}
    if backup_ack:
        data["backup_ack"] = "on"
    return client.post(reverse("op_execute", args=[op.pk]), data, follow=True)


def test_execute_refusals(client, sample_db, project):
    use_project(client, project)
    prod = target(sample_db, environment="prod")
    _, op = start_delete(client, prod)
    phrase = "delete devices_customer 1 on prod"
    assert "Type the confirmation" in execute(client, op, "delete devices_customer 1").content.decode()
    assert "backup" in execute(client, op, phrase).content.decode()
    DataOperation.objects.filter(pk=op.pk).update(planned_at=timezone.now() - timedelta(minutes=20))
    assert "older than 15 minutes" in execute(client, op, phrase, True).content.decode()
    assert rows(sample_db, "SELECT count(*) FROM devices_customer") == 4


def test_execute_refused_when_writes_disabled(client, sample_db, project):
    use_project(client, project)
    db = target(sample_db, writes=False)
    _, op = start_delete(client, db)
    body = client.get(reverse("op_detail", args=[op.pk])).content.decode()
    assert "Writes are not enabled" in body
    assert "not enabled" in execute(client, op, "delete devices_customer 1 on stage").content.decode()
    assert rows(sample_db, "SELECT count(*) FROM devices_customer") == 4


def test_execute_then_restore(client, stage, sample_db, project):
    use_project(client, project)
    _, op = start_delete(client, stage)
    response = execute(client, op, "delete devices_customer 1 on stage")
    op.refresh_from_db()
    assert op.status == "done", op.error
    assert rows(sample_db, "SELECT count(*) FROM devices_customer") == 3
    assert "Deleted" in response.content.decode()
    restore = client.get(reverse("op_restore", args=[op.pk]))
    assert restore["Content-Disposition"].startswith("attachment")
    script = b"".join(restore.streaming_content).decode()
    with psycopg.connect(**sample_db, autocommit=True) as conn:
        conn.execute(script)
    assert rows(sample_db, "SELECT count(*) FROM devices_customer") == 4


def test_override_db_only_relation(client, stage, sample_db, project):
    use_project(client, project)
    with psycopg.connect(**sample_db, autocommit=True) as conn:
        conn.execute("CREATE TABLE billing_invoice (id int PRIMARY KEY, customer_id bigint REFERENCES devices_customer(id))")
        conn.execute("INSERT INTO billing_invoice VALUES (1, 2)")
    _, op = start_delete(client, stage, filters=(("id", "eq", "2"),))
    assert op.status == "blocked"
    rel = "billing_invoice.customer_id -> devices_customer.id"
    client.post(reverse("op_repreview", args=[op.pk]), {"override": [rel]})
    op.refresh_from_db()
    assert op.status == "planned" and op.cascade_overrides == [rel]


def test_backup_file_download_and_traversal(client, stage, project):
    use_project(client, project)
    _, op = start_delete(client, stage)
    execute(client, op, "delete devices_customer 1 on stage")
    ok = client.get(reverse("op_backup_file", args=[op.pk, "devices_device.jsonl.gz"]))
    assert ok.status_code == 200
    assert client.get(reverse("op_backup_file", args=[op.pk, "../../secret.key"])).status_code == 404


def test_drop_column_flow(client, stage, sample_db, project):
    use_project(client, project)
    with psycopg.connect(**sample_db, autocommit=True) as conn:
        conn.execute("ALTER TABLE devices_device ADD COLUMN legacy_code text")
    response = client.post(reverse("op_new"), {"kind": "drop_column", "database": stage.pk,
                                               "table": "devices_device", "column": "legacy_code"})
    op = DataOperation.objects.get(kind="drop_column")
    assert response.status_code == 302 and op.status == "planned"
    execute(client, op, "drop devices_device.legacy_code on stage")
    op.refresh_from_db()
    assert op.status == "done"


def test_recipes_crud_and_run(client, stage, project):
    response = client.post(reverse("recipe_create"), {
        "name": "old executions", "project": project.pk, "root_table": "devices_execution", "batch_size": 2,
        "f_col": ["started"], "f_op": ["older_than_days"], "f_val": ["90"], "notes": "",
    })
    assert response.status_code == 302
    recipe = CleanupRecipe.objects.get()
    assert recipe.filters == [{"column": "started", "op": "older_than_days", "value": "90"}]
    assert "old executions" in client.get(reverse("recipe_list")).content.decode()
    client.post(reverse("recipe_run", args=[recipe.pk]), {"database": stage.pk})
    op = DataOperation.objects.get(kind="cleanup")
    assert op.status == "planned" and op.plan["root_total"] == 2
    execute(client, op, "cleanup old executions on stage")
    op.refresh_from_db()
    assert op.status == "done" and op.counts["deleted"]["devices_execution"] == 2


def test_job_pages(client):
    job = Job.objects.create(kind="compare", title="x", status="done", progress=100, result_url="/runs/")
    assert client.get(reverse("job_detail", args=[job.pk]))["Location"] == "/runs/"  # finished: go to result
    assert client.get(reverse("job_detail", args=[job.pk]) + "?stay=1").status_code == 200
    assert client.get(reverse("job_json", args=[job.pk])).json()["status"] == "done"
    running = Job.objects.create(kind="cleanup", status="running")
    from validator import jobs
    jobs._RUNNING.add(running.pk)
    client.post(reverse("job_cancel", args=[running.pk]))
    running.refresh_from_db()
    assert running.cancel_requested
    jobs._RUNNING.discard(running.pk)


def test_operations_log(client, stage, project):
    use_project(client, project)
    start_delete(client, stage)
    body = client.get(reverse("op_list")).content.decode()
    assert "devices_customer" in body and "stage" in body
