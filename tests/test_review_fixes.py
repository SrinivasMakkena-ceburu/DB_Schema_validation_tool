"""Regression tests for the final review findings (C1-C3, I1-I6)."""
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from django.urls import reverse

from tests.conftest import pg_execute
from validator.cascade import build_graph
from validator.db_inspector import inspect_database
from validator.executor import OperationError, execute, rehearse, restore_script
from validator.filters import FilterError, build_where, validate_filters
from validator.planner import plan

SAMPLE = Path(__file__).resolve().parent / "sample_project"


def scalar(conninfo, query):
    with psycopg.connect(**conninfo) as conn:
        return conn.execute(query).fetchone()[0]


def graph_where(conninfo, extracted, root, filters):
    info = inspect_database(conninfo, "public")
    graph = build_graph(info, extracted)
    return graph, build_where(graph.columns(root), filters)


def by_id(value):
    return [{"column": "id", "op": "eq", "value": str(value)}]


# C2: partitioned tables ---------------------------------------------------------

PARTITIONED = [
    "CREATE TABLE events (id int, year int NOT NULL, device_id bigint REFERENCES devices_device(id) ON DELETE SET NULL)"
    " PARTITION BY LIST (year)",
    "CREATE TABLE events_2025 PARTITION OF events FOR VALUES IN (2025)",
    "CREATE TABLE events_2026 PARTITION OF events FOR VALUES IN (2026)",
    # first row of each partition: both have ctid (0,1)
    "INSERT INTO events VALUES (1, 2025, 12), (2, 2026, 20)",
    "CREATE TABLE history (id int, year int NOT NULL, device_id bigint REFERENCES devices_device(id) ON DELETE CASCADE)"
    " PARTITION BY LIST (year)",
    "CREATE TABLE history_2025 PARTITION OF history FOR VALUES IN (2025)",
    "CREATE TABLE history_2026 PARTITION OF history FOR VALUES IN (2026)",
    "INSERT INTO history VALUES (1, 2025, 12), (2, 2026, 20)",
]


def test_partitioned_set_null_touches_only_planned_rows(sample_db, sample_extracted, tmp_path):
    pg_execute(sample_db, PARTITIONED)
    graph, where = graph_where(sample_db, sample_extracted, "devices_device", by_id(12))
    result = execute(sample_db, graph, "devices_device", where, expected=None, backup_dir=tmp_path / "op",
                     timeout_s=60, log=[])
    assert scalar(sample_db, "SELECT device_id FROM events WHERE id = 1") is None
    assert scalar(sample_db, "SELECT device_id FROM events WHERE id = 2") == 20   # other partition untouched
    assert result["counts"]["deleted"]["history"] == 1                            # cascade into partitions works
    assert scalar(sample_db, "SELECT count(*) FROM history") == 1


# C3: cascades the planner cannot follow ----------------------------------------

def test_other_schema_and_composite_cascades_block(sample_db, sample_extracted):
    pg_execute(sample_db, [
        "CREATE SCHEMA audit",
        "CREATE TABLE audit.log (id int, customer_id bigint REFERENCES devices_customer(id) ON DELETE CASCADE)",
        "INSERT INTO audit.log VALUES (1, 1), (2, 1)",
        "ALTER TABLE devices_customer ADD CONSTRAINT customer_id_name UNIQUE (id, name)",
        "CREATE TABLE sub (id int, cid bigint, cname varchar(100),"
        " FOREIGN KEY (cid, cname) REFERENCES devices_customer(id, name) ON DELETE CASCADE)",
        "INSERT INTO sub VALUES (1, 1, 'Acme')",
    ])
    graph, where = graph_where(sample_db, sample_extracted, "devices_customer", by_id(1))
    with psycopg.connect(**sample_db) as conn:
        result = plan(conn, graph, "devices_customer", where)
        conn.rollback()
    blocked = {b["table"]: b["count"] for b in result["blockers"]}
    assert blocked == {"audit.log": 2, "sub": 1}


def test_trigger_side_effects_abort(sample_db, sample_extracted, tmp_path):
    pg_execute(sample_db, [
        "CREATE TABLE shadow (id int)", "INSERT INTO shadow VALUES (1), (2)",
        "CREATE FUNCTION wipe() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN DELETE FROM shadow; RETURN OLD; END $$",
        "CREATE TRIGGER wipe_shadow AFTER DELETE ON devices_device FOR EACH ROW EXECUTE FUNCTION wipe()",
    ])
    graph, where = graph_where(sample_db, sample_extracted, "devices_device", by_id(12))
    assert rehearse(sample_db, graph, "devices_device", where, timeout_s=60)["ok"] is False
    with pytest.raises(OperationError, match="shadow"):
        execute(sample_db, graph, "devices_device", where, expected=None, backup_dir=tmp_path / "op",
                timeout_s=60, log=[])
    assert scalar(sample_db, "SELECT count(*) FROM shadow") == 2
    assert scalar(sample_db, "SELECT count(*) FROM devices_device WHERE id = 12") == 1


# C1: never delete a backup folder this call did not create ---------------------

def test_existing_backup_dir_is_never_removed(sample_db, sample_extracted, tmp_path):
    existing = tmp_path / "op"
    existing.mkdir()
    (existing / "earlier.jsonl.gz").write_text("committed batch backup")
    graph, where = graph_where(sample_db, sample_extracted, "devices_customer", by_id(1))
    with pytest.raises(OperationError):
        execute(sample_db, graph, "devices_customer", where, expected=None, backup_dir=existing,
                timeout_s=60, log=[])
    assert (existing / "earlier.jsonl.gz").exists()
    assert scalar(sample_db, "SELECT count(*) FROM devices_customer") == 4


# I1: no truncation / rounding through the cast ---------------------------------

@pytest.fixture
def typed_table(pg_conninfo):
    pg_execute(pg_conninfo, [
        "CREATE TABLE t (code varchar(5), amount numeric(10,2), letter char(3), at time)",
        "INSERT INTO t VALUES ('ABCDE', 1.24, 'ABC', '10:00')",
    ])
    return pg_conninfo


COLS = {"code": "character varying(5)", "amount": "numeric(10,2)", "letter": "character(3)",
        "at": "time without time zone"}


@pytest.mark.parametrize("filters", [
    [{"column": "code", "op": "eq", "value": "ABCDEFGH-old"}],
    [{"column": "code", "op": "in", "value": "ABCDEFGH, X"}],
    [{"column": "amount", "op": "eq", "value": "1.239"}],
    [{"column": "letter", "op": "eq", "value": "ABCD"}],
])
def test_filter_values_are_not_truncated(typed_table, filters):
    with psycopg.connect(**typed_table, autocommit=True) as conn:
        where = build_where(COLS, filters)
        assert conn.execute(psycopg.sql.SQL("SELECT count(*) FROM t WHERE {}").format(where)).fetchone()[0] == 0


def test_filter_values_still_match_exactly(typed_table):
    with psycopg.connect(**typed_table, autocommit=True) as conn:
        for f in ([{"column": "code", "op": "eq", "value": "ABCDE"}], [{"column": "letter", "op": "eq", "value": "ABC"}],
                  [{"column": "amount", "op": "eq", "value": "1.24"}]):
            q = psycopg.sql.SQL("SELECT count(*) FROM t WHERE {}").format(build_where(COLS, f))
            assert conn.execute(q).fetchone()[0] == 1


# I2: invalid filters are validation errors -------------------------------------

def test_more_invalid_filters_are_filter_errors(typed_table):
    with pytest.raises(FilterError):
        build_where(COLS, [{"column": "at", "op": "older_than_days", "value": "3"}])  # time has no date
    with pytest.raises(FilterError):
        build_where({"created": "timestamp with time zone"},
                    [{"column": "created", "op": "older_than_days", "value": "99999999999999999999"}])
    pg_execute(typed_table, "CREATE TABLE j (doc json)")
    with psycopg.connect(**typed_table, autocommit=True) as conn, pytest.raises(FilterError):
        validate_filters(conn, psycopg.sql.Identifier("j"), {"doc": "json"},
                         [{"column": "doc", "op": "eq", "value": "{}"}])


# I6: restore with generated columns --------------------------------------------

def test_restore_with_generated_column(sample_db, sample_extracted, tmp_path):
    pg_execute(sample_db, "ALTER TABLE devices_device ADD COLUMN host_upper text "
                          "GENERATED ALWAYS AS (upper(hostname)) STORED")
    graph, where = graph_where(sample_db, sample_extracted, "devices_customer", by_id(1))
    execute(sample_db, graph, "devices_customer", where, expected=None, backup_dir=tmp_path / "op",
            timeout_s=60, log=[])
    with psycopg.connect(**sample_db, autocommit=True) as conn:
        conn.execute("".join(restore_script(tmp_path / "op")))
    assert scalar(sample_db, "SELECT host_upper FROM devices_device WHERE id = 10") == "ACME-ROOT"


# I5: cache sees repeated edits to an already-modified file -----------------------

@pytest.mark.django_db
def test_cache_notices_second_edit_of_same_file(tmp_path, monkeypatch):
    from validator import project_loader
    from validator.models import Project

    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    project = Project.objects.create(name="r", path=str(repo), python_path=sys.executable,
                                     extra_env="SAMPLE_SECRET_KEY=x")
    calls = []
    monkeypatch.setattr(project_loader, "load_project", lambda p: calls.append(1) or {"n": len(calls)})
    models = repo / "devices" / "models.py"
    models.write_text(models.read_text() + "\n# edit 1\n")
    project_loader.load_project_cached(project)
    project_loader.load_project_cached(project)
    assert len(calls) == 1
    models.write_text(models.read_text() + "# edit 2\n")
    project_loader.load_project_cached(project)
    assert len(calls) == 2


# I3: drop column locks the table before the backup ------------------------------

def test_drop_column_locks_before_backup(pg_conninfo, tmp_path):
    from validator.column_ops import execute_drop_column

    pg_execute(pg_conninfo, ["CREATE TABLE thing (id int PRIMARY KEY, old text)", "INSERT INTO thing VALUES (1, 'x')"])
    log = []
    execute_drop_column(pg_conninfo, "public", "thing", "old", backup_dir=tmp_path / "op", timeout_s=30, log=log)
    assert log[0].startswith("LOCK TABLE") and "DROP COLUMN" in log[-1]


# I4 / C1: one execution per operation; no re-preview after it ran -------------

@pytest.mark.django_db
def test_claim_is_atomic(sample_db):
    from validator.models import DataOperation
    from validator.operations import claim_for_execution

    op = DataOperation.objects.create(kind="delete", database_name="x", environment="dev", root_table="t",
                                      status="planned")
    assert claim_for_execution(op, "phrase") is True
    assert claim_for_execution(op, "phrase") is False


@pytest.mark.django_db
def test_no_repreview_after_execution(client):
    from django.utils import timezone

    from validator.models import DataOperation

    op = DataOperation.objects.create(kind="cleanup", database_name="x", environment="dev", root_table="t",
                                      status="cancelled", executed_at=timezone.now())
    response = client.post(reverse("op_repreview", args=[op.pk]), follow=True)
    assert "already ran" in response.content.decode()
    op.refresh_from_db()
    assert op.status == "cancelled"


# Re-graded: links carry encoded values; pk with '/' resolves ---------------------

@pytest.mark.django_db
def test_child_links_are_encoded(client, sample_db):
    from validator.models import DatabaseTarget

    pg_execute(sample_db, ["CREATE TABLE tag (code text PRIMARY KEY)", "INSERT INTO tag VALUES ('a&b/c#1')",
                           "CREATE TABLE tagged (id int PRIMARY KEY, tag_code text REFERENCES tag(code))",
                           "INSERT INTO tagged VALUES (1, 'a&b/c#1')"])
    db = DatabaseTarget(name="s", host=sample_db["host"], port=sample_db["port"], dbname=sample_db["dbname"],
                        user=sample_db["user"])
    db.set_password(sample_db["password"])
    db.save()
    body = client.get(reverse("data_row", args=[db.pk, "tag", "a&b/c#1"])).content.decode()
    assert "f_val=a%26b%2Fc%231" in body
    assert client.get(reverse("data_row", args=[db.pk, "tagged", "1"])).status_code == 200


@pytest.mark.django_db
def test_preview_database_error_is_reported_not_stuck(sample_db, monkeypatch):
    from validator import operations
    from validator.models import DataOperation, DatabaseTarget

    db = DatabaseTarget(name="s", host=sample_db["host"], port=sample_db["port"], dbname=sample_db["dbname"],
                        user=sample_db["user"])
    db.set_password(sample_db["password"])
    db.save()

    def timeout(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(operations, "plan", timeout)
    op = DataOperation.objects.create(kind="delete", database=db, database_name="s", environment="dev",
                                      root_table="devices_customer", filters=by_id(1), status="planning")
    operations.preview_operation(op)
    op.refresh_from_db()
    assert op.status == "error" and "statement timeout" in op.error


@pytest.mark.django_db
def test_browser_bad_pk_and_timeouts_are_not_500(client, sample_db, monkeypatch):
    from validator import views_data
    from validator.models import DatabaseTarget

    db = DatabaseTarget(name="s", host=sample_db["host"], port=sample_db["port"], dbname=sample_db["dbname"],
                        user=sample_db["user"])
    db.set_password(sample_db["password"])
    db.save()
    assert client.get(reverse("data_row", args=[db.pk, "devices_customer", "abc"])).status_code == 404

    def slow(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(views_data, "validate_filters", slow)
    response = client.get(reverse("data_table", args=[db.pk, "devices_customer"]))
    assert response.status_code == 200 and "statement timeout" in response.content.decode()
