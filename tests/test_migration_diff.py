from validator.migration_diff import diff_migrations


def node(name, deps=(), replaces=(), creates=()):
    return {"name": name, "dependencies": [list(d) for d in deps], "replaces": [list(r) for r in replaces],
            "initial": name.startswith("0001"), "creates": list(creates)}


DISK = {
    "shop": {"nodes": [
        node("0001_initial", creates=["product"]),
        node("0002_price", deps=[("shop", "0001_initial")]),
    ]},
    "orders": {"nodes": [node("0001_initial", deps=[("shop", "0001_initial")], creates=["order"])]},
}
APPS = ["shop", "orders"]
MODELS = [
    {"app_label": "shop", "model": "Product", "db_table": "shop_product"},
    {"app_label": "orders", "model": "Order", "db_table": "orders_order"},
]


def rows(*pairs):
    return [{"app": a, "name": n, "applied": None} for a, n in pairs]


ALL_APPLIED = rows(("shop", "0001_initial"), ("shop", "0002_price"), ("orders", "0001_initial"))


def run(recorded, disk=DISK, tables=("shop_product", "orders_order")):
    return diff_migrations(disk, recorded, APPS, set(tables), MODELS)


def cats(findings):
    return sorted((f["category"], f["severity"], f["app"], f["column"]) for f in findings)


def test_in_sync():
    findings, per_app = run(ALL_APPLIED)
    assert findings == []
    assert per_app["shop"]["disk_leaf"] == ["0002_price"]
    assert per_app["shop"]["db_latest"] == "0002_price"


def test_history_table_missing():
    findings, _ = run(None, tables=())
    assert ("history_missing", "error", "", "") in cats(findings)


def test_ghost_row():
    findings, per_app = run(ALL_APPLIED + rows(("shop", "0003_feature_x")))
    assert cats(findings) == [("ghost", "error", "shop", "0003_feature_x")]
    assert per_app["shop"]["ghosts"] == 1


def test_rows_for_app_not_in_branch():
    findings, per_app = run(ALL_APPLIED + rows(("billing", "0001_initial"), ("billing", "0002_x")))
    assert cats(findings) == [("other_app_rows", "warning", "billing", "")]
    assert findings[0]["data"]["names"] == ["0001_initial", "0002_x"]
    assert per_app["billing"]["installed"] is False


def test_unapplied_is_info_when_tables_absent():
    findings, _ = run(rows(("shop", "0001_initial"), ("shop", "0002_price")), tables=("shop_product",))
    assert cats(findings) == [("unapplied", "info", "orders", "0001_initial")]


def test_unapplied_is_error_when_its_tables_exist():
    findings, _ = run(rows(("shop", "0001_initial"), ("shop", "0002_price")))
    assert cats(findings) == [("unapplied", "error", "orders", "0001_initial")]
    assert "orders_order" in findings[0]["message"]


def test_inconsistent_history():
    findings, _ = run(rows(("shop", "0002_price"), ("orders", "0001_initial")))
    assert ("inconsistent", "error", "shop", "0002_price") in cats(findings)
    assert ("inconsistent", "error", "orders", "0001_initial") in cats(findings)


def test_multiple_leaves():
    disk = {**DISK, "shop": {"nodes": DISK["shop"]["nodes"] + [node("0002_other", deps=[("shop", "0001_initial")])]}}
    findings, per_app = run(ALL_APPLIED + rows(("shop", "0002_other")), disk=disk)
    assert cats(findings) == [("multiple_leaves", "error", "shop", "")]
    assert per_app["shop"]["disk_leaf"] == ["0002_other", "0002_price"]


SQUASH_DISK = {
    "shop": {"nodes": [
        node("0001_initial", creates=["product"]),
        node("0001_squashed_0002_price", replaces=[("shop", "0001_initial"), ("shop", "0002_price")], creates=["product"]),
        node("0002_price", deps=[("shop", "0001_initial")]),
    ]},
    "orders": DISK["orders"],
}


def test_squash_applied_through_replaced_rows():
    findings, per_app = run(ALL_APPLIED, disk=SQUASH_DISK)
    assert findings == []
    assert per_app["shop"]["disk_leaf"] == ["0001_squashed_0002_price"]


def test_replaced_rows_without_files_are_not_ghosts():
    disk = {"shop": {"nodes": [SQUASH_DISK["shop"]["nodes"][1]]}, "orders": DISK["orders"]}
    findings, _ = run(ALL_APPLIED, disk=disk)
    assert findings == []


def test_squash_partially_applied_uses_originals():
    findings, _ = run(rows(("shop", "0001_initial"), ("orders", "0001_initial")), disk=SQUASH_DISK)
    assert cats(findings) == [("unapplied", "info", "shop", "0002_price")]


def test_fresh_database_uses_squash():
    findings, _ = run(rows(), disk=SQUASH_DISK, tables=())
    assert cats(findings) == [
        ("unapplied", "info", "orders", "0001_initial"),
        ("unapplied", "info", "shop", "0001_squashed_0002_price"),
    ]
