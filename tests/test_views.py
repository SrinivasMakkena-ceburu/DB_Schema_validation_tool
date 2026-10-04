import sys
from pathlib import Path

import pytest
from django.urls import reverse

from validator.models import ComparisonRun, DatabaseTarget, Project

SAMPLE = Path(__file__).resolve().parent / "sample_project"
pytestmark = pytest.mark.django_db


@pytest.fixture
def project():
    return Project.objects.create(name="sample", path=str(SAMPLE), python_path=sys.executable,
                                  extra_env="SAMPLE_SECRET_KEY=x")


@pytest.fixture
def database():
    db = DatabaseTarget(name="stage", host="127.0.0.1", port=1, dbname="x", user="u")
    db.set_password("topsecret")
    db.save()
    return db


@pytest.fixture
def run(project, database):
    report = {
        "schema": [{"category": "table_missing", "severity": "error", "app": "catalog", "table": "catalog_tag",
                    "column": "", "message": "Table catalog_tag (model Tag) is missing", "data": {}}],
        "migrations": [{"category": "ghost", "severity": "error", "app": "catalog", "table": "",
                        "column": "0009_x", "message": "ghost row", "data": {"name": "0009_x"}}],
        "pending": [], "notes": [], "commands": ["python manage.py migrate catalog --prune"],
        "per_app": {"catalog": {"installed": True, "disk_leaf": ["0002_a"], "db_latest": "0009_x",
                                "recorded": 3, "ghosts": 1, "unapplied": 0}},
        "meta": {"django_version": "5.2", "settings_module": "s", "models": 5, "db_tables": 4,
                 "db_views": 0, "history_rows": 3},
    }
    from validator.runner import summarize
    return ComparisonRun.objects.create(
        project=project, database=database, project_name="sample", database_name="stage",
        git_branch="feature/x", git_commit="abc", report=report, summary=summarize(report),
        fix_sql="BEGIN;\nCREATE TABLE x;\nCOMMIT;\n", options={"history_strategy": "fake"},
    )


@pytest.mark.parametrize("name", ["dashboard", "run_list", "project_list", "database_list",
                                  "project_create", "database_create"])
def test_pages_render(client, name, project, database):
    assert client.get(reverse(name)).status_code == 200


def test_dashboard_empty_state(client):
    body = client.get(reverse("dashboard")).content.decode()
    assert "Add a project" in body and "Add a database" in body


def test_batch_and_run_pages(client, run):
    batch = client.get(reverse("batch_detail", args=[run.batch_id])).content.decode()
    assert "Table missing" in batch and "Applied, file not in branch" in batch
    detail = client.get(reverse("run_detail", args=[run.pk])).content.decode()
    assert "catalog_tag" in detail and "0009_x" in detail and "migrate catalog --prune" in detail


def test_unknown_batch_is_404(client):
    assert client.get("/batches/00000000-0000-0000-0000-000000000000/").status_code == 404


def test_downloads(client, run):
    sql = client.get(reverse("run_fix_sql", args=[run.pk]))
    assert sql["Content-Type"].startswith("application/sql")
    assert "attachment" in sql["Content-Disposition"]
    assert b"CREATE TABLE x" in sql.content
    report = client.get(reverse("run_report_json", args=[run.pk]))
    assert report.json()["database"] == "stage"


def test_project_create_validates_paths(client, tmp_path):
    response = client.post(reverse("project_create"), {"name": "p", "path": str(tmp_path), "python_path": "/nope"})
    assert response.status_code == 200
    assert "No manage.py in this folder." in response.content.decode()
    response = client.post(reverse("project_create"), {"name": "p", "path": str(SAMPLE), "python_path": sys.executable})
    assert response.status_code == 302
    assert Project.objects.filter(name="p").exists()


def test_database_password_never_rendered_and_kept_when_blank(client, database):
    body = client.get(reverse("database_edit", args=[database.pk])).content.decode()
    assert "topsecret" not in body and database.password_encrypted not in body
    data = {"name": "stage", "host": "db.local", "port": 5432, "dbname": "x", "user": "u",
            "sslmode": "prefer", "schema": "public", "notes": "", "password": ""}
    assert client.post(reverse("database_edit", args=[database.pk]), data).status_code == 302
    database.refresh_from_db()
    assert database.host == "db.local" and database.get_password() == "topsecret"
    client.post(reverse("database_edit", args=[database.pk]), {**data, "clear_password": "on"})
    database.refresh_from_db()
    assert database.get_password() == ""


def test_delete_keeps_runs(client, run, database):
    assert client.post(reverse("database_delete", args=[database.pk])).status_code == 302
    assert not DatabaseTarget.objects.exists()
    run.refresh_from_db()
    assert run.database is None and run.database_name == "stage"


def test_database_test_reports_failure(client, database):
    response = client.post(reverse("database_test", args=[database.pk]), follow=True)
    assert "Cannot connect" in response.content.decode()


def test_project_check_success_and_failure(client, project):
    body = client.post(reverse("project_check", args=[project.pk])).content.decode()
    assert "Imported with Django" in body and "Add field note to order" in body
    project.extra_env = ""
    project.save()
    body = client.post(reverse("project_check", args=[project.pk])).content.decode()
    assert "could not be imported" in body and "SAMPLE_SECRET_KEY" in body


def test_run_comparison_redirects_to_batch(client, project, database):
    response = client.post(reverse("dashboard"), {
        "project": project.pk, "databases": [database.pk], "history_strategy": "fake",
    })
    assert response.status_code == 302
    run = ComparisonRun.objects.get()
    assert response["Location"] == reverse("batch_detail", args=[run.batch_id])
    assert run.status == "error"  # port 1 is unreachable
    page = client.get(response["Location"]).content.decode()
    assert "run failed" in page
