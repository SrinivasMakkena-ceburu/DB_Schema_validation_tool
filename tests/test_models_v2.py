import pytest

from validator.models import DatabaseTarget, IgnoreRule, Project

pytestmark = pytest.mark.django_db


def make_db(**kw):
    db = DatabaseTarget(name=kw.pop("name", "stage"), host="h", dbname="d", user="reader", **kw)
    db.set_password("readpw")
    db.save()
    return db


def test_write_conninfo_falls_back_to_read_credentials():
    db = make_db()
    info = db.write_conninfo()
    assert info["user"] == "reader" and info["password"] == "readpw"


def test_write_credentials_are_separate_and_encrypted():
    db = make_db(write_user="writer")
    db.set_write_password("writepw")
    db.save()
    db.refresh_from_db()
    assert "writepw" not in db.write_password_encrypted
    info = db.write_conninfo()
    assert info["user"] == "writer" and info["password"] == "writepw"
    assert db.conninfo()["user"] == "reader"


def test_defaults_are_safe():
    db = make_db()
    assert db.writes_enabled is False
    assert db.environment == "dev"
    assert db.operation_timeout_s == 300
    assert make_db(name="p", environment="prod").is_prod


def finding(category="table_extra", table="legacy_report", column="", app=""):
    return {"category": category, "severity": "warning", "app": app, "table": table, "column": column,
            "message": "", "data": {}}


def test_ignore_rule_matching():
    stage, prod = make_db(), make_db(name="prod")
    project = Project.objects.create(name="p", path="/x", python_path="/x")
    rule = IgnoreRule.objects.create(database=stage, category="table_extra", pattern="legacy_*")
    assert rule.matches(finding(), stage.id, None)
    assert not rule.matches(finding(), prod.id, None)
    assert not rule.matches(finding(category="column_extra"), stage.id, None)
    assert not rule.matches(finding(table="other"), stage.id, None)

    anywhere = IgnoreRule.objects.create(pattern="billing*")
    assert anywhere.matches(finding(category="other_app_rows", table="", app="billing"), prod.id, project.id)

    scoped = IgnoreRule.objects.create(project=project, pattern="*")
    assert scoped.matches(finding(), stage.id, project.id)
    assert not scoped.matches(finding(), stage.id, None)


def test_database_form_write_password_keep_and_clear():
    from validator.forms import DatabaseForm

    db = make_db(write_user="writer")
    db.set_write_password("w1")
    db.save()
    data = {"name": "stage", "host": "h", "port": 5432, "dbname": "d", "user": "reader", "sslmode": "prefer",
            "schema": "public", "environment": "prod", "writes_enabled": "on", "write_user": "writer",
            "operation_timeout_s": 60}
    form = DatabaseForm(data, instance=db)
    assert form.is_valid(), form.errors
    db = form.save()
    assert db.writes_enabled and db.is_prod and db.write_conninfo()["password"] == "w1"
    form = DatabaseForm({**data, "clear_write_password": "on"}, instance=db)
    assert form.is_valid()
    db = form.save()
    assert db.write_password_encrypted == ""
    assert "password" not in db.write_conninfo()


def test_settings_for_operations(settings):
    assert settings.JOBS_INLINE is True  # set by tests/conftest.py
    assert settings.PLAN_MAX_AGE_MINUTES == 15
    assert str(settings.BACKUP_DIR).endswith("backups")
