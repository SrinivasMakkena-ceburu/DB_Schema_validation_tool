"""Compare two databases: the reference database stands in for the branch's models."""
from .schema_diff import IGNORED_TABLES, finding, guess_app


def db_as_models(db_info, app_labels=()):
    """Turn inspector output into the `models` shape diff_schema() expects."""
    models = []
    for table, info in sorted(db_info["tables"].items()):
        if table in IGNORED_TABLES:
            continue
        fks = {fk["columns"][0]: fk for fk in info["fks"] if len(fk["columns"]) == 1}
        columns = []
        for name, col in info["columns"].items():
            fk = fks.get(name)
            columns.append({
                "name": name, "field": name, "db_type": col["type"], "null": col["nullable"],
                "primary_key": name in info["pk"], "unique": [name] in info["uniques"],
                "fk": {"table": fk["ref_table"], "column": fk["ref_columns"][0]} if fk else None,
                "db_constraint": bool(fk), "has_default": col["has_default"], "has_db_default": False,
            })
        models.append({
            "app_label": guess_app(table, app_labels), "model": table, "db_table": table, "managed": True,
            "auto_created": False, "pk": info["pk"][0] if len(info["pk"]) == 1 else "",
            "columns": columns,
            "unique_sets": [u for u in info["uniques"] if set(u) != set(info["pk"])],
            "index_sets": list(info["indexes"]), "create_sql": [], "add_column_sql": {},
        })
    return models


def diff_history_sets(reference_rows, target_rows):
    ref = {(r["app"], r["name"]) for r in reference_rows}
    tgt = {(r["app"], r["name"]) for r in target_rows}
    out = []
    for app, name in sorted(ref - tgt):
        out.append(finding("history_only_reference", "warning", app, "",
                           f"{app}.{name} is applied on the reference but not on this database", name,
                           {"name": name}))
    for app, name in sorted(tgt - ref):
        out.append(finding("history_only_target", "warning", app, "",
                           f"{app}.{name} is applied on this database but not on the reference", name,
                           {"name": name}))
    return out
