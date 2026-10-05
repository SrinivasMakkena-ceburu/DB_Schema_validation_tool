"""Through the UI: browse, preview a customer delete, confirm, restore with psql; cleanup in batches."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from django.urls import reverse

from validator.models import DataOperation, DatabaseTarget, Project

pytestmark = [pytest.mark.django_db, pytest.mark.skipif(not shutil.which("psql"), reason="psql not installed")]
SAMPLE = Path(__file__).resolve().parent / "sample_project"


def scalar(conninfo, query):
    with psycopg.connect(**conninfo) as conn:
        return conn.execute(query).fetchone()[0]


def psql(conninfo, script):
    proc = subprocess.run(
        ["psql", "-v", "ON_ERROR_STOP=1", "-h", conninfo["host"], "-p", str(conninfo["port"]), "-U",
         conninfo["user"], "-d", conninfo["dbname"], "-f", "-"],
        input=script, env={**os.environ, "PGPASSWORD": conninfo["password"]}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_customer_delete_and_restore_through_ui(client, sample_db):
    project = Project.objects.create(name="backend", path=str(SAMPLE), python_path=sys.executable,
                                     extra_env="SAMPLE_SECRET_KEY=x")
    db = DatabaseTarget(name="prod-us", environment="prod", writes_enabled=True, host=sample_db["host"],
                        port=sample_db["port"], dbname=sample_db["dbname"], user=sample_db["user"])
    db.set_password(sample_db["password"])
    db.save()
    client.post(reverse("data_project"), {"project": project.pk, "next": "/data/"})

    # Find the customer in the browser.
    page = client.get(reverse("data_table", args=[db.pk, "devices_customer"]) + "?f_col=name&f_op=eq&f_val=Acme")
    assert "Acme" in page.content.decode()

    # Preview: everything that goes with it.
    response = client.post(reverse("op_new"), {"kind": "delete", "database": db.pk, "table": "devices_customer",
                                               "f_col": "id", "f_op": "eq", "f_val": "1"}, follow=True)
    body = response.content.decode()
    assert "PRODUCTION" in body and "delete devices_customer 1 on prod-us" in body
    op = DataOperation.objects.get()
    assert op.plan["total_rows"] == 10 and op.rehearsal["ok"]

    # Confirm on prod (phrase + backup acknowledgement) and run.
    client.post(reverse("op_execute", args=[op.pk]),
                {"confirmation": "delete devices_customer 1 on prod-us", "backup_ack": "on"})
    op.refresh_from_db()
    assert op.status == "done", op.error
    assert scalar(sample_db, "SELECT count(*) FROM devices_device WHERE customer_id = 1") == 0
    assert scalar(sample_db, "SELECT operator_id FROM devices_execution WHERE id = 102") is None

    # Restore with psql from the downloaded script.
    script = b"".join(client.get(reverse("op_restore", args=[op.pk])).streaming_content).decode()
    psql(sample_db, script)
    assert scalar(sample_db, "SELECT count(*) FROM devices_device WHERE customer_id = 1") == 3
    assert scalar(sample_db, "SELECT operator_id FROM devices_execution WHERE id = 102") == 1
    assert scalar(sample_db, "SELECT count(*) FROM devices_note") == 2
