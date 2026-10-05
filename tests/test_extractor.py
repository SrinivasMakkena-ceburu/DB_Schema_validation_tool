import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR = ROOT / "validator" / "extractor.py"
SAMPLE = ROOT / "tests" / "sample_project"


def run_extractor(tmp_path, settings="sampleproj.settings"):
    out = tmp_path / "out.json"
    env = {**os.environ, "SAMPLE_SECRET_KEY": "x"}
    proc = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--settings", settings, "--output", str(out)],
        cwd=SAMPLE, env=env, capture_output=True, text=True, timeout=120,
    )
    return proc, out


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    proc, out = run_extractor(tmp_path_factory.mktemp("extract"))
    assert proc.returncode == 0, proc.stderr
    assert "settings loaded" in proc.stdout  # noise went to stdout, JSON to the file
    return json.loads(out.read_text())


def model(data, table):
    return next(m for m in data["models"] if m["db_table"] == table)


def test_columns_and_fk(extracted):
    product = model(extracted, "catalog_product")
    cols = {c["name"]: c for c in product["columns"]}
    assert cols["category_id"]["fk"] == {"table": "catalog_category", "column": "id"}
    assert cols["name"]["db_type"] == "varchar(200)"
    assert cols["id"]["primary_key"] is True
    assert ["category_id", "name"] in product["unique_sets"]
    assert ["created"] in product["index_sets"]


def test_m2m_through_table(extracted):
    through = model(extracted, "catalog_product_tags")
    assert through["auto_created"] is True
    assert ["product_id", "tag_id"] in through["unique_sets"]
    # the parent model's create_sql does not also carry the through table
    product = model(extracted, "catalog_product")
    assert not any("catalog_product_tags" in s for s in product["create_sql"])


def test_create_and_add_column_sql(extracted):
    product = model(extracted, "catalog_product")
    assert product["create_sql"][0].startswith('CREATE TABLE "catalog_product"')
    sku_sql = product["add_column_sql"]["sku"]
    assert 'ADD COLUMN "sku" varchar(40) DEFAULT \'\' NOT NULL' in sku_sql[0]


def test_migrations_and_squash(extracted):
    nodes = {n["name"]: n for n in extracted["migrations"]["catalog"]["nodes"]}
    assert set(nodes) == {"0001_initial", "0002_product_sku", "0001_squashed_0002_product_sku"}
    assert nodes["0001_squashed_0002_product_sku"]["replaces"] == [
        ["catalog", "0001_initial"], ["catalog", "0002_product_sku"],
    ]
    assert nodes["0002_product_sku"]["dependencies"] == [["catalog", "0001_initial"]]
    assert extracted["migrations"]["contenttypes"]["nodes"]


def test_pending_changes(extracted):
    assert extracted["pending_changes"] == {"orders": ["Add field note to order"]}
    assert extracted["original_engine"] == "django.db.backends.postgresql"


def test_import_error_exits_nonzero(tmp_path):
    proc, out = run_extractor(tmp_path, settings="sampleproj.does_not_exist")
    assert proc.returncode != 0
    assert "ModuleNotFoundError" in proc.stderr
    assert not out.exists()


def relation(data, child_table, child_column):
    return next(r for r in data["relations"] if r["child_table"] == child_table and r["child_column"] == child_column)


def test_relations_with_on_delete(extracted):
    r = relation(extracted, "devices_execution", "device_id")
    assert r == {"child_table": "devices_execution", "child_column": "device_id",
                 "parent_table": "devices_device", "parent_column": "id",
                 "on_delete": "CASCADE", "nullable": False, "parent_link": False}
    assert relation(extracted, "devices_execution", "operator_id")["on_delete"] == "SET_NULL"
    assert relation(extracted, "devices_execution", "operator_id")["nullable"] is True
    assert relation(extracted, "devices_contract", "customer_id")["on_delete"] == "PROTECT"
    assert relation(extracted, "devices_auditentry", "device_id")["on_delete"] == "RESTRICT"
    assert relation(extracted, "devices_device", "parent_id")["parent_table"] == "devices_device"
    assert relation(extracted, "devices_group_devices", "device_id")["on_delete"] == "CASCADE"


def test_mti_parent_link(extracted):
    r = relation(extracted, "devices_premiumcustomer", "customer_ptr_id")
    assert r["parent_link"] is True and r["parent_table"] == "devices_customer"


def test_generic_relations_and_pk(extracted):
    assert {"parent_table": "devices_customer", "parent_app_label": "devices", "parent_model": "customer",
            "parent_pk": "id", "child_table": "devices_note", "ct_column": "content_type_id",
            "object_id_column": "object_id"} in extracted["generic_relations"]
    # inherited GenericRelation: notes attached to the subclass's own content type cascade too
    assert {r["parent_model"] for r in extracted["generic_relations"]} == {"customer", "premiumcustomer"}
    assert model(extracted, "devices_premiumcustomer")["pk"] == "customer_ptr_id"
