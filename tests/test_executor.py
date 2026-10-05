import gzip
import json

import psycopg
import pytest

from tests.conftest import pg_execute
from validator.cascade import build_graph
from validator.db_inspector import inspect_database
from validator.executor import OperationError, execute, plan_signature, rehearse, restore_script
from validator.filters import build_where
from validator.planner import plan

TABLES = ["devices_customer", "devices_premiumcustomer", "devices_operator", "devices_device",
          "devices_execution", "devices_contract", "devices_auditentry", "devices_note", "devices_group_devices"]


def setup(conninfo, extracted, root="devices_customer", value=1, column="id"):
    db_info = inspect_database(conninfo, "public")
    graph = build_graph(db_info, extracted)
    columns = {n: c["type"] for n, c in db_info["tables"][root]["columns"].items()}
    where = build_where(columns, [{"column": column, "op": "eq", "value": str(value)}])
    return graph, where


def preview(conninfo, graph, root, where):
    with psycopg.connect(**conninfo) as conn:
        result = plan(conn, graph, root, where)
        conn.rollback()
    return result


def snapshot(conninfo):
    with psycopg.connect(**conninfo) as conn:
        return {t: sorted(json.dumps(r[0], sort_keys=True) for r in conn.execute(f"SELECT row_to_json(x) FROM {t} x"))
                for t in TABLES}


def count(conninfo, table, where="TRUE"):
    with psycopg.connect(**conninfo) as conn:
        return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]


def test_rehearsal_rolls_back(sample_db, sample_extracted):
    graph, where = setup(sample_db, sample_extracted)
    before = snapshot(sample_db)
    result = rehearse(sample_db, graph, "devices_customer", where, timeout_s=60)
    assert result["ok"] is True, result
    assert result["counts"]["deleted"]["devices_device"] == 3
    assert snapshot(sample_db) == before


def test_rehearsal_reports_database_errors(sample_db, sample_extracted):
    pg_execute(sample_db, [
        "CREATE FUNCTION no_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'devices are frozen'; END $$",
        "CREATE TRIGGER freeze_devices BEFORE DELETE ON devices_device FOR EACH ROW EXECUTE FUNCTION no_delete()",
    ])
    graph, where = setup(sample_db, sample_extracted)
    result = rehearse(sample_db, graph, "devices_customer", where, timeout_s=60)
    assert result["ok"] is False
    assert "devices are frozen" in result["error"]


def test_execute_deletes_exactly_the_plan(sample_db, sample_extracted, tmp_path):
    graph, where = setup(sample_db, sample_extracted)
    expected = plan_signature(preview(sample_db, graph, "devices_customer", where))
    log = []
    result = execute(sample_db, graph, "devices_customer", where, expected=expected,
                     backup_dir=tmp_path / "op", timeout_s=60, log=log)
    assert result["counts"]["deleted"] == expected["delete"]
    assert result["counts"]["nulled"] == {"devices_execution.operator_id": 1}
    assert count(sample_db, "devices_customer") == 3
    assert count(sample_db, "devices_device") == 1
    assert count(sample_db, "devices_execution", "id = 102 AND operator_id IS NULL") == 1
    assert count(sample_db, "devices_note") == 1
    assert any(line.startswith("DELETE FROM") for line in log)


def test_backup_contains_every_deleted_row(sample_db, sample_extracted, tmp_path):
    graph, where = setup(sample_db, sample_extracted)
    execute(sample_db, graph, "devices_customer", where, expected=None, backup_dir=tmp_path / "op",
            timeout_s=60, log=[])
    with gzip.open(tmp_path / "op" / "devices_device.jsonl.gz", "rt") as fh:
        rows = [json.loads(line) for line in fh]
    assert {r["hostname"] for r in rows} == {"acme-root", "acme-child", "acme-grandchild"}
    manifest = json.loads((tmp_path / "op" / "manifest.json").read_text())
    assert manifest["tables"]["devices_device"]["rows"] == 3
    assert manifest["nulls"][0]["table"] == "devices_execution"


def test_restore_script_restores_everything(sample_db, sample_extracted, tmp_path):
    graph, where = setup(sample_db, sample_extracted)
    before = snapshot(sample_db)
    execute(sample_db, graph, "devices_customer", where, expected=None, backup_dir=tmp_path / "op",
            timeout_s=60, log=[])
    assert snapshot(sample_db) != before
    script = "".join(restore_script(tmp_path / "op"))
    with psycopg.connect(**sample_db, autocommit=True) as conn:
        conn.execute(script)
    assert snapshot(sample_db) == before


def test_count_drift_aborts(sample_db, sample_extracted, tmp_path):
    graph, where = setup(sample_db, sample_extracted)
    expected = plan_signature(preview(sample_db, graph, "devices_customer", where))
    pg_execute(sample_db, "INSERT INTO devices_device (id, customer_id, hostname) VALUES (13, 1, 'new since preview')")
    with pytest.raises(OperationError, match="changed since the preview"):
        execute(sample_db, graph, "devices_customer", where, expected=expected, backup_dir=tmp_path / "op",
                timeout_s=60, log=[])
    assert count(sample_db, "devices_device") == 5
    assert not (tmp_path / "op").exists()


def test_blockers_stop_execution(sample_db, sample_extracted, tmp_path):
    graph, where = setup(sample_db, sample_extracted, value=3)
    with pytest.raises(OperationError, match="blocked"):
        execute(sample_db, graph, "devices_customer", where, expected=None, backup_dir=tmp_path / "op",
                timeout_s=60, log=[])
    assert count(sample_db, "devices_customer") == 4


def test_limit_executes_one_batch(sample_db, sample_extracted, tmp_path):
    db_info = inspect_database(sample_db, "public")
    graph = build_graph(db_info, sample_extracted)
    cols = {n: c["type"] for n, c in db_info["tables"]["devices_execution"]["columns"].items()}
    where = build_where(cols, [{"column": "status", "op": "eq", "value": "ok"}])
    result = execute(sample_db, graph, "devices_execution", where, expected=None, backup_dir=tmp_path / "b1",
                     timeout_s=60, log=[], limit=1)
    assert result["root_count"] == 1
    assert count(sample_db, "devices_execution", "status = 'ok'") == 1
