"""Compare the models of a branch with the tables of a database.

Constraints and indexes are matched by column set, never by name: Django's
generated names are hashes and differ between environments.
"""
from .pgtypes import normalize_type

IGNORED_TABLES = {"django_migrations"}


def finding(category, severity, app, table, message, column="", data=None):
    return {
        "category": category,
        "severity": severity,
        "app": app,
        "table": table,
        "column": column,
        "message": message,
        "data": data or {},
    }


def guess_app(table, app_labels):
    matches = [label for label in app_labels if table.startswith(label + "_")]
    return max(matches, key=len) if matches else ""


def _has_set(column_sets, cols):
    return any(set(existing) == set(cols) for existing in column_sets)


def _diff_columns(model, actual):
    app, table = model["app_label"], model["db_table"]
    expected = {c["name"]: c for c in model["columns"]}
    out = []
    for name, col in expected.items():
        found = actual["columns"].get(name)
        if found is None:
            out.append(finding("column_missing", "error", app, table, f"Column {name} is missing", name,
                               {"expected": normalize_type(col["db_type"])}))
            continue
        want, have = normalize_type(col["db_type"]), normalize_type(found["type"])
        if want != have:
            out.append(finding("type_mismatch", "error", app, table,
                               f"Column {name} is {have}, model expects {want}", name,
                               {"expected": want, "actual": have}))
        if not col["null"] and found["nullable"]:
            out.append(finding("null_mismatch", "warning", app, table,
                               f"Column {name} is nullable, model says NOT NULL", name,
                               {"expected_null": False}))
        elif col["null"] and not found["nullable"]:
            out.append(finding("null_mismatch", "error", app, table,
                               f"Column {name} is NOT NULL, model allows NULL (inserts of NULL will fail)",
                               name, {"expected_null": True}))
    for name, found in actual["columns"].items():
        if name in expected:
            continue
        blocking = not found["nullable"] and not found["has_default"]
        message = f"Column {name} exists in the database but not in the model"
        if blocking:
            message += " and is NOT NULL without a default (Django inserts will fail)"
        out.append(finding("column_extra", "error" if blocking else "warning", app, table, message, name,
                           {"actual": normalize_type(found["type"]), "nullable": found["nullable"]}))
    return out


def _diff_constraints(model, actual):
    app, table = model["app_label"], model["db_table"]
    present = set(actual["columns"])
    out = []

    expected_pk = [c["name"] for c in model["columns"] if c["primary_key"]]
    if expected_pk and set(expected_pk) != set(actual["pk"]):
        out.append(finding("pk_mismatch", "error", app, table,
                           f"Primary key is ({', '.join(actual['pk']) or 'none'}), model expects "
                           f"({', '.join(expected_pk)})", data={"expected": expected_pk, "actual": actual["pk"]}))

    for col in model["columns"]:
        if not col["fk"] or not col["db_constraint"] or col["name"] not in present:
            continue
        fks = [fk for fk in actual["fks"] if fk["columns"] == [col["name"]]]
        target = col["fk"]["table"]
        if not fks:
            out.append(finding("fk_missing", "warning", app, table,
                               f"Foreign key {col['name']} → {target} is missing", col["name"],
                               {"ref_table": target, "ref_column": col["fk"]["column"]}))
        elif not any(fk["ref_table"] == target for fk in fks):
            out.append(finding("fk_wrong", "error", app, table,
                               f"Foreign key {col['name']} points at {fks[0]['ref_table']}, model expects {target}",
                               col["name"], {"ref_table": target, "actual": fks[0]["ref_table"]}))

    unique_like = actual["uniques"] + [actual["pk"]]
    for cols in model["unique_sets"]:
        if set(cols) <= present and not _has_set(unique_like, cols):
            out.append(finding("unique_missing", "warning", app, table,
                               f"Unique constraint on ({', '.join(cols)}) is missing", ",".join(cols),
                               {"columns": cols}))
    for cols in model["index_sets"]:
        if set(cols) <= present and not _has_set(actual["indexes"] + unique_like, cols):
            out.append(finding("index_missing", "info", app, table,
                               f"Index on ({', '.join(cols)}) is missing", ",".join(cols), {"columns": cols}))
    return out


def diff_schema(models, db):
    """models: extractor `models`; db: inspector result. Returns a list of findings."""
    tables, views = db["tables"], set(db.get("views", []))
    findings = []
    model_tables = set()
    for model in models:
        table, app = model["db_table"], model["app_label"]
        model_tables.add(table)
        actual = tables.get(table)
        if actual is None:
            if not model["managed"]:
                if table not in views:
                    findings.append(finding("table_missing", "info", app, table,
                                            f"Table {table} of unmanaged model {model['model']} is missing"))
            else:
                findings.append(finding("table_missing", "error", app, table,
                                        f"Table {table} (model {model['model']}) is missing"))
            continue
        findings += _diff_columns(model, actual)
        findings += _diff_constraints(model, actual)

    app_labels = {m["app_label"] for m in models}
    for table in sorted(set(tables) - model_tables - IGNORED_TABLES):
        findings.append(finding("table_extra", "warning", guess_app(table, app_labels), table,
                                f"Table {table} exists in the database but no model in this branch uses it"))
    return findings
