import copy

import pytest

from validator.schema_diff import diff_schema


def col(name, db_type="integer", null=False, pk=False, unique=False, fk=None, db_constraint=None):
    return {
        "name": name, "field": name, "db_type": db_type, "null": null, "primary_key": pk,
        "unique": unique or pk, "fk": fk,
        "db_constraint": bool(fk) if db_constraint is None else db_constraint,
        "has_db_default": False, "has_default": False,
    }


MODELS = [
    {
        "app_label": "shop", "model": "Category", "db_table": "shop_category", "managed": True,
        "auto_created": False,
        "columns": [col("id", "bigint", pk=True), col("name", "varchar(100)", unique=True)],
        "unique_sets": [["name"]], "index_sets": [], "create_sql": [], "add_column_sql": {},
    },
    {
        "app_label": "shop", "model": "Product", "db_table": "shop_product", "managed": True,
        "auto_created": False,
        "columns": [
            col("id", "bigint", pk=True),
            col("category_id", "bigint", fk={"table": "shop_category", "column": "id"}),
            col("name", "varchar(200)"),
            col("price", "numeric(10, 2)"),
            col("note", "text", null=True),
        ],
        "unique_sets": [["category_id", "name"]],
        "index_sets": [["category_id"], ["price"]],
        "create_sql": [], "add_column_sql": {},
    },
]


def dbcol(type_, nullable=False, has_default=False):
    return {"type": type_, "nullable": nullable, "has_default": has_default}


DB = {
    "tables": {
        "shop_category": {
            "columns": {"id": dbcol("bigint", has_default=True), "name": dbcol("character varying(100)")},
            "pk": ["id"], "uniques": [["name"]], "fks": [], "indexes": [],
        },
        "shop_product": {
            "columns": {
                "id": dbcol("bigint", has_default=True),
                "category_id": dbcol("bigint"),
                "name": dbcol("character varying(200)"),
                "price": dbcol("numeric(10,2)"),
                "note": dbcol("text", nullable=True),
            },
            "pk": ["id"],
            "uniques": [["name", "category_id"]],
            "fks": [{"columns": ["category_id"], "ref_table": "shop_category", "ref_columns": ["id"]}],
            "indexes": [["category_id"], ["price"]],
        },
        "django_migrations": {"columns": {}, "pk": [], "uniques": [], "fks": [], "indexes": []},
    },
    "views": [],
}


@pytest.fixture
def db():
    return copy.deepcopy(DB)


def categories(findings):
    return sorted((f["category"], f["severity"], f["table"], f["column"]) for f in findings)


def test_clean_schema_no_findings(db):
    assert diff_schema(MODELS, db) == []


def test_table_missing(db):
    del db["tables"]["shop_category"]
    assert ("table_missing", "error", "shop_category", "") in categories(diff_schema(MODELS, db))


def test_unmanaged_table_missing_is_info(db):
    models = copy.deepcopy(MODELS)
    models[0]["managed"] = False
    del db["tables"]["shop_category"]
    assert ("table_missing", "info", "shop_category", "") in categories(diff_schema(models, db))


def test_unmanaged_model_backed_by_view_is_fine(db):
    models = copy.deepcopy(MODELS)
    models[0]["managed"] = False
    del db["tables"]["shop_category"]
    db["views"] = ["shop_category"]
    assert not [f for f in diff_schema(models, db) if f["table"] == "shop_category"]


def test_table_extra_guesses_app(db):
    db["tables"]["shop_legacy"] = {"columns": {}, "pk": [], "uniques": [], "fks": [], "indexes": []}
    findings = diff_schema(MODELS, db)
    assert categories(findings) == [("table_extra", "warning", "shop_legacy", "")]
    assert findings[0]["app"] == "shop"


def test_column_missing(db):
    del db["tables"]["shop_product"]["columns"]["price"]
    assert ("column_missing", "error", "shop_product", "price") in categories(diff_schema(MODELS, db))


def test_column_extra_not_null_without_default_is_error(db):
    db["tables"]["shop_product"]["columns"]["legacy"] = dbcol("integer")
    assert categories(diff_schema(MODELS, db)) == [("column_extra", "error", "shop_product", "legacy")]


def test_column_extra_nullable_is_warning(db):
    db["tables"]["shop_product"]["columns"]["legacy"] = dbcol("integer", nullable=True)
    assert categories(diff_schema(MODELS, db)) == [("column_extra", "warning", "shop_product", "legacy")]


def test_type_mismatch(db):
    db["tables"]["shop_product"]["columns"]["name"] = dbcol("character varying(100)")
    findings = diff_schema(MODELS, db)
    assert categories(findings) == [("type_mismatch", "error", "shop_product", "name")]
    assert findings[0]["data"] == {"expected": "varchar(200)", "actual": "varchar(100)"}


def test_model_not_null_db_nullable_is_warning(db):
    db["tables"]["shop_product"]["columns"]["name"]["nullable"] = True
    assert categories(diff_schema(MODELS, db)) == [("null_mismatch", "warning", "shop_product", "name")]


def test_model_nullable_db_not_null_is_error(db):
    db["tables"]["shop_product"]["columns"]["note"]["nullable"] = False
    assert categories(diff_schema(MODELS, db)) == [("null_mismatch", "error", "shop_product", "note")]


def test_pk_mismatch(db):
    db["tables"]["shop_product"]["pk"] = []
    assert ("pk_mismatch", "error", "shop_product", "") in categories(diff_schema(MODELS, db))


def test_fk_missing(db):
    db["tables"]["shop_product"]["fks"] = []
    assert categories(diff_schema(MODELS, db)) == [("fk_missing", "warning", "shop_product", "category_id")]


def test_fk_without_db_constraint_not_required(db):
    models = copy.deepcopy(MODELS)
    models[1]["columns"][1]["db_constraint"] = False
    db["tables"]["shop_product"]["fks"] = []
    assert diff_schema(models, db) == []


def test_fk_wrong_target(db):
    db["tables"]["shop_product"]["fks"][0]["ref_table"] = "shop_other"
    assert categories(diff_schema(MODELS, db)) == [("fk_wrong", "error", "shop_product", "category_id")]


def test_unique_missing(db):
    db["tables"]["shop_product"]["uniques"] = []
    assert categories(diff_schema(MODELS, db)) == [("unique_missing", "warning", "shop_product", "category_id,name")]


def test_index_missing(db):
    db["tables"]["shop_product"]["indexes"] = [["category_id"]]
    assert categories(diff_schema(MODELS, db)) == [("index_missing", "info", "shop_product", "price")]


def test_index_satisfied_by_unique(db):
    db["tables"]["shop_category"]["indexes"] = []
    models = copy.deepcopy(MODELS)
    models[0]["index_sets"] = [["name"]]
    assert diff_schema(models, db) == []


def test_missing_column_not_reported_again_as_unique_or_index(db):
    del db["tables"]["shop_product"]["columns"]["price"]
    db["tables"]["shop_product"]["indexes"] = [["category_id"]]
    assert categories(diff_schema(MODELS, db)) == [("column_missing", "error", "shop_product", "price")]
