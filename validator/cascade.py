"""The relations a delete has to follow, from the branch's models and the database.

Django does `on_delete` in Python: its foreign keys in the database are plain
NO ACTION constraints. So the model relations (from the extractor) decide what
happens, and database foreign keys fill in tables the branch does not know.
"""
from dataclasses import dataclass, field

# Relation.action values
CASCADE = "cascade"          # child rows are deleted too
SET_NULL = "set_null"        # child column set to NULL
PROTECT = "protect"          # any referencing row blocks the delete (Django PROTECT)
RESTRICT = "restrict"        # blocks unless the child row is deleted through another path
BLOCK = "block"              # cannot be handled automatically (SET_DEFAULT, custom, DB NO ACTION)
ORPHAN = "orphan"            # DO_NOTHING without a DB constraint: rows are left dangling
PARENT_LINK = "parent_link"  # multi-table inheritance: deleting the child row deletes its parent row
GENERIC = "generic"          # GenericRelation: rows matching content type + object id are deleted

FOLLOWED = (CASCADE, PARENT_LINK, GENERIC)
BLOCKING = (PROTECT, RESTRICT, BLOCK)

_MODEL_ACTIONS = {"CASCADE": CASCADE, "SET_NULL": SET_NULL, "PROTECT": PROTECT, "RESTRICT": RESTRICT,
                  "SET_DEFAULT": BLOCK, "CUSTOM": BLOCK}
_DB_ACTIONS = {"c": CASCADE, "n": SET_NULL, "d": BLOCK, "a": BLOCK, "r": BLOCK}


@dataclass(frozen=True)
class Relation:
    id: str
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    action: str
    source: str  # "model" or "db"
    note: str = ""
    ct_column: str = ""
    content_type: tuple = ()


@dataclass
class Graph:
    schema: str
    tables: dict
    relations: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def columns(self, table):
        return {name: c["type"] for name, c in self.tables[table]["columns"].items()}


def relation_id(child_table, child_column, parent_table, parent_column):
    return f"{child_table}.{child_column} -> {parent_table}.{parent_column}"


def _present(tables, table, column):
    return table in tables and column in tables[table]["columns"]


def _db_fk(tables, table, column):
    for fk in tables.get(table, {}).get("fks", []):
        if fk["columns"] == [column]:
            return fk
    return None


def build_graph(db_info, extracted=None, overrides=(), schema="public"):
    """db_info: inspector output; extracted: extractor output or None; overrides: relation ids to cascade."""
    tables = db_info["tables"]
    overrides = set(overrides)
    graph = Graph(schema=schema, tables=tables)
    covered = set()

    for r in (extracted or {}).get("relations", []):
        rid = relation_id(r["child_table"], r["child_column"], r["parent_table"], r["parent_column"])
        if not (_present(tables, r["child_table"], r["child_column"])
                and _present(tables, r["parent_table"], r["parent_column"])):
            graph.skipped.append(f"{rid}: table or column not in this database")
            continue
        covered.add((r["child_table"], r["child_column"]))
        action, note = _MODEL_ACTIONS.get(r["on_delete"], BLOCK), ""
        if r["on_delete"] in ("SET_DEFAULT", "CUSTOM"):
            note = f"on_delete={r['on_delete']} must be handled manually"
        elif r["on_delete"] == "DO_NOTHING":
            constrained = _db_fk(tables, r["child_table"], r["child_column"]) is not None
            action = BLOCK if constrained else ORPHAN
            note = "on_delete=DO_NOTHING" + (" with a database constraint" if constrained else "")
        graph.relations.append(Relation(rid, r["child_table"], r["child_column"], r["parent_table"],
                                        r["parent_column"], action, "model", note))
        if r.get("parent_link"):
            graph.relations.append(Relation(
                relation_id(r["parent_table"], r["parent_column"], r["child_table"], r["child_column"]) + " (parent)",
                r["child_table"], r["child_column"], r["parent_table"], r["parent_column"], PARENT_LINK, "model",
                "multi-table inheritance: the parent row goes with the child row"))

    for table, info in tables.items():
        for fk in info["fks"]:
            if len(fk["columns"]) != 1:
                graph.skipped.append(f"{table}({', '.join(fk['columns'])}) -> {fk['ref_table']}: "
                                     "composite foreign key not followed")
                continue
            column = fk["columns"][0]
            if (table, column) in covered or fk["ref_table"] not in tables:
                continue
            rid = relation_id(table, column, fk["ref_table"], fk["ref_columns"][0])
            action = _DB_ACTIONS.get(fk.get("on_delete", "a"), BLOCK)
            note = "database-only foreign key"
            if action == BLOCK and rid in overrides:
                action, note = CASCADE, "database-only foreign key, treated as cascade"
            graph.relations.append(Relation(rid, table, column, fk["ref_table"], fk["ref_columns"][0],
                                            action, "db", note))

    for g in (extracted or {}).get("generic_relations", []):
        rid = f"{g['child_table']}.{g['object_id_column']} -> {g['parent_table']}.{g['parent_pk']} (generic)"
        if not (_present(tables, g["child_table"], g["object_id_column"])
                and _present(tables, g["child_table"], g["ct_column"])
                and _present(tables, g["parent_table"], g["parent_pk"])):
            graph.skipped.append(f"{rid}: table or column not in this database")
            continue
        graph.relations.append(Relation(rid, g["child_table"], g["object_id_column"], g["parent_table"],
                                        g["parent_pk"], GENERIC, "model", "GenericRelation",
                                        g["ct_column"], (g["parent_app_label"], g["parent_model"])))
    return graph
