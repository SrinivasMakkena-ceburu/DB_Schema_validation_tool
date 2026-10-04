"""Compare a branch's migration files with a database's django_migrations rows.

Squashed migrations follow Django's loader rules: a squash counts as applied
when every migration it replaces is recorded; when none are recorded the
squash is used instead of the originals; when only some are, the originals
are used.
"""
from .schema_diff import finding


def _resolve_graph(nodes, recorded):
    """Return (active node keys, applied keys, dependency remap)."""
    ignored, applied, remap = set(), set(recorded) & set(nodes), {}
    for key, node in nodes.items():
        replaces = [tuple(r) for r in node["replaces"]]
        if not replaces:
            continue
        done = [r for r in replaces if r in recorded]
        if len(done) == len(replaces):
            applied.add(key)
        if done and len(done) < len(replaces):
            ignored.add(key)
            remap[key] = replaces[-1]
        else:
            ignored.update(replaces)
            remap.update({r: key for r in replaces})
    active = set(nodes) - ignored
    return active, applied, remap


def _leaves(app, active, nodes, remap):
    parents = set()
    for key in active:
        if key[0] != app:
            continue
        for dep in nodes[key]["dependencies"]:
            parents.add(remap.get(tuple(dep), tuple(dep)))
    return sorted(name for (a, name) in active if a == app and (a, name) not in parents)


def diff_migrations(disk, recorded, installed_apps, existing_tables, models):
    """Return (findings, per_app).

    disk: extractor `migrations`; recorded: inspector `migrations` (None when
    the table is missing); existing_tables: table names in the database;
    models: extractor `models` (to map CreateModel operations to tables).
    """
    findings = []
    if recorded is None:
        findings.append(finding("history_missing", "error", "", "",
                                "Table django_migrations does not exist: the database has no migration history"))
        recorded = []

    nodes = {(app, n["name"]): n for app, info in disk.items() for n in info["nodes"]}
    recorded_keys = [(r["app"], r["name"]) for r in recorded]
    recorded_set = set(recorded_keys)
    known_replaced = {tuple(r) for n in nodes.values() for r in n["replaces"]}
    active, applied, remap = _resolve_graph(nodes, recorded_set)
    tables_by_model = {(m["app_label"], m["model"].lower()): m["db_table"] for m in models}

    per_app = {}
    for app in sorted(set(installed_apps) | {a for a, _ in recorded_keys}):
        db_rows = [name for a, name in recorded_keys if a == app]
        per_app[app] = {
            "installed": app in installed_apps,
            "disk_leaf": _leaves(app, active, nodes, remap),
            "db_latest": db_rows[-1] if db_rows else "",
            "recorded": len(db_rows),
            "ghosts": 0,
            "unapplied": 0,
        }

    other = {}
    for app, name in recorded_keys:
        if app not in installed_apps:
            other.setdefault(app, []).append(name)
        elif (app, name) not in nodes and (app, name) not in known_replaced:
            per_app[app]["ghosts"] += 1
            findings.append(finding("ghost", "error", app, "",
                                    f"{app}.{name} is recorded as applied but the migration file "
                                    f"is not in this branch", name, {"name": name}))
    for app, names in other.items():
        findings.append(finding("other_app_rows", "warning", app, "",
                                f"{len(names)} history row(s) for app '{app}', which is not installed "
                                f"in this branch (another feature?)", data={"names": names}))

    for app, name in sorted(active - applied):
        node = nodes[(app, name)]
        clash = sorted(
            tables_by_model[(app, model)]
            for model in node["creates"]
            if tables_by_model.get((app, model)) in existing_tables
        )
        per_app[app]["unapplied"] += 1
        if clash:
            findings.append(finding("unapplied", "error", app, "",
                                    f"{app}.{name} is not recorded but its tables already exist "
                                    f"({', '.join(clash)}): migrate would fail; fake-apply it",
                                    name, {"name": name, "existing_tables": clash}))
        else:
            findings.append(finding("unapplied", "info", app, "",
                                    f"{app}.{name} is not applied yet", name, {"name": name}))

    for key in sorted(active & applied):
        for dep in nodes[key]["dependencies"]:
            dep = remap.get(tuple(dep), tuple(dep))
            if dep in nodes and dep in active and dep not in applied:
                findings.append(finding("inconsistent", "error", key[0], "",
                                        f"{key[0]}.{key[1]} is applied but its dependency "
                                        f"{dep[0]}.{dep[1]} is not", key[1],
                                        {"name": key[1], "dependency": list(dep)}))

    for app, info in per_app.items():
        if len(info["disk_leaf"]) > 1:
            findings.append(finding("multiple_leaves", "error", app, "",
                                    f"App {app} has {len(info['disk_leaf'])} latest migrations "
                                    f"({', '.join(info['disk_leaf'])}); a merge migration is needed",
                                    data={"leaves": info["disk_leaf"]}))
    return findings, per_app
