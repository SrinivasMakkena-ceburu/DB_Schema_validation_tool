import psycopg
import pytest

from tests.conftest import pg_execute
from validator.cascade import build_graph
from validator.db_inspector import inspect_database
from validator.filters import build_where
from validator.planner import PlanError, plan


def do_plan(conninfo, extracted, root, filters, overrides=(), limit=None):
    db_info = inspect_database(conninfo, "public")
    graph = build_graph(db_info, extracted, overrides)
    columns = {name: c["type"] for name, c in db_info["tables"][root]["columns"].items()}
    with psycopg.connect(**conninfo) as conn:
        result = plan(conn, graph, root, build_where(columns, filters), limit=limit)
        conn.rollback()
    return result


def counts(result):
    return {t["table"]: t["count"] for t in result["tables"]}


def by_id(value, column="id"):
    return [{"column": column, "op": "eq", "value": str(value)}]


def test_delete_customer_cascades(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(1))
    assert counts(result) == {
        "devices_customer": 1,
        "devices_device": 3,          # 10, 11 (parent 10), 12 (parent 11)
        "devices_execution": 2,       # 100 reachable twice (customer + device), counted once
        "devices_operator": 1,
        "devices_auditentry": 1,      # RESTRICT on device is satisfied: it is cascaded via customer
        "devices_note": 1,            # GenericRelation
        "devices_group_devices": 1,   # auto-created M2M through table
    }
    assert result["root_count"] == 1
    assert result["total_rows"] == 10
    assert result["nulls"] == [{"table": "devices_execution", "column": "operator_id", "count": 1,
                                "relation": "devices_execution.operator_id -> devices_operator.id"}]
    assert result["blockers"] == []


def test_diamond_counted_once(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(1))
    execution = next(t for t in result["tables"] if t["table"] == "devices_execution")
    assert sum(v["count"] for v in execution["via"]) == 2


def test_cycle_self_fk(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_device", by_id(10))
    assert counts(result)["devices_device"] == 3


def test_protect_blocks(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(3))
    assert [(b["relation"], b["action"], b["count"]) for b in result["blockers"]] == [
        ("devices_contract.customer_id -> devices_customer.id", "protect", 1)]
    assert result["blockers"][0]["samples"][0]["code"] == "C-3"


def test_restrict_blocks_when_child_not_cascaded(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_device", by_id(20))
    assert [(b["relation"], b["action"]) for b in result["blockers"]] == [
        ("devices_auditentry.device_id -> devices_device.id", "restrict")]


def test_mti_child_delete_includes_parent_row(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_premiumcustomer", by_id(4, "customer_ptr_id"))
    assert counts(result) == {"devices_premiumcustomer": 1, "devices_customer": 1}


def test_mti_parent_delete_includes_child_row(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(4))
    assert counts(result) == {"devices_customer": 1, "devices_premiumcustomer": 1}


def test_db_only_fk_blocks_unless_overridden(sample_db, sample_extracted):
    pg_execute(sample_db, [
        "CREATE TABLE billing_invoice (id int PRIMARY KEY, customer_id bigint REFERENCES devices_customer(id))",
        "INSERT INTO billing_invoice VALUES (1, 2), (2, 1)",
    ])
    rel = "billing_invoice.customer_id -> devices_customer.id"
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(2))
    assert [(b["relation"], b["action"], b["source"]) for b in result["blockers"]] == [(rel, "block", "db")]
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(2), overrides=[rel])
    assert result["blockers"] == []
    assert counts(result)["billing_invoice"] == 1


def test_without_project_django_fks_block(sample_db):
    result = do_plan(sample_db, None, "devices_customer", by_id(2))
    blocked = {b["relation"] for b in result["blockers"]}
    assert "devices_device.customer_id -> devices_customer.id" in blocked
    assert counts(result) == {"devices_customer": 1}


def test_relation_for_missing_table_is_skipped(sample_db, sample_extracted):
    pg_execute(sample_db, "DROP TABLE devices_auditentry")
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(1))
    assert "devices_auditentry" not in counts(result)
    assert any("devices_auditentry" in s for s in result["skipped"])


def test_filters_and_limit(sample_db, sample_extracted):
    filters = [{"column": "started", "op": "older_than_days", "value": "90"}]
    result = do_plan(sample_db, sample_extracted, "devices_execution", filters)
    assert counts(result) == {"devices_execution": 2}
    assert do_plan(sample_db, sample_extracted, "devices_execution", filters, limit=1)["root_count"] == 1


def test_samples_present(sample_db, sample_extracted):
    result = do_plan(sample_db, sample_extracted, "devices_customer", by_id(1))
    device = next(t for t in result["tables"] if t["table"] == "devices_device")
    assert {s["hostname"] for s in device["samples"]} == {"acme-root", "acme-child", "acme-grandchild"}
    assert device["depth"] == 1


def test_plan_changes_nothing(sample_db, sample_extracted):
    do_plan(sample_db, sample_extracted, "devices_customer", by_id(1))
    with psycopg.connect(**sample_db) as conn:
        assert conn.execute("SELECT count(*) FROM devices_device").fetchone()[0] == 4


def test_unknown_root_table(sample_db, sample_extracted):
    db_info = inspect_database(sample_db, "public")
    graph = build_graph(db_info, sample_extracted)
    with psycopg.connect(**sample_db) as conn, pytest.raises(PlanError, match="not in this database"):
        plan(conn, graph, "nope", build_where({}, []))
