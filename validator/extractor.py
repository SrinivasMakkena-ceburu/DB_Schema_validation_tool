#!/usr/bin/env python
"""Dump a Django project's models and migration files as JSON.

This script is run by the Schema Sync Validator with the TARGET project's own
interpreter, from the project folder:

    <venv>/bin/python extractor.py --settings config.settings --output out.json

It imports only the standard library and Django, and never opens a database
connection: DATABASES is replaced by a PostgreSQL entry pointing at a socket
directory that does not exist, which is enough for db_type() and the schema
editor (collect_sql mode) to produce PostgreSQL DDL.
"""
import argparse
import json
import os
import sys
import traceback

DUMMY_DB = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "schemasync_extractor",
    "HOST": "/nonexistent-schemasync-socket-dir",
    "USER": "schemasync",
    "PASSWORD": "",
}


def setup_django(settings_module, project_dir):
    sys.path.insert(0, project_dir)
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    from django.conf import settings

    original_engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    settings.DATABASES = {"default": dict(DUMMY_DB)}
    settings.DATABASE_ROUTERS = []

    import django

    django.setup()
    return original_engine


def quote_literal(value):
    """Quote a parameter as an SQL literal without a database connection."""
    try:
        from psycopg import sql
    except ImportError:  # psycopg2 project
        from psycopg2.extensions import adapt

        return adapt(value).getquoted().decode()
    return sql.quote(value)


def compose_sql_offline(sql_text, params):
    # Django's PostgreSQL backend inlines DDL parameters with a server-side
    # cursor (mogrify), which would open a connection.
    if not params:
        return sql_text
    if isinstance(params, dict):
        return sql_text % {k: quote_literal(v) for k, v in params.items()}
    return sql_text % tuple(quote_literal(p) for p in params)


def collect_sql(connection, action):
    connection.ops.compose_sql = compose_sql_offline
    with connection.schema_editor(collect_sql=True, atomic=False) as editor:
        action(editor)
    return [str(s).strip() for s in editor.collected_sql if str(s).strip()]


def column_names(meta, field_names):
    return [meta.get_field(name).column for name in field_names]


def describe_model(model, connection):
    from django.db import models

    meta = model._meta
    columns = []
    unique_sets = []
    index_sets = []
    for field in meta.local_concrete_fields:
        db_type = field.db_type(connection)
        if db_type is None:
            continue
        fk = None
        if field.is_relation and (field.many_to_one or field.one_to_one):
            target = field.target_field
            fk = {"table": target.model._meta.db_table, "column": target.column}
        columns.append(
            {
                "name": field.column,
                "field": field.name,
                "db_type": db_type,
                "null": field.null,
                "primary_key": field.primary_key,
                "unique": field.unique,
                "fk": fk,
                "db_constraint": bool(getattr(field, "db_constraint", False)),
                "has_db_default": getattr(field, "db_default", models.NOT_PROVIDED)
                is not models.NOT_PROVIDED,
                "has_default": field.has_default(),
            }
        )
        if field.unique and not field.primary_key:
            unique_sets.append([field.column])
        elif field.db_index and not field.primary_key:
            index_sets.append([field.column])

    for names in meta.unique_together:
        unique_sets.append(column_names(meta, names))
    for constraint in meta.constraints:
        if (
            isinstance(constraint, models.UniqueConstraint)
            and constraint.fields
            and constraint.condition is None
            and not constraint.expressions
        ):
            unique_sets.append(column_names(meta, constraint.fields))
    for names in getattr(meta, "index_together", ()) or ():
        index_sets.append(column_names(meta, names))
    for index in meta.indexes:
        if index.fields and not getattr(index, "condition", None):
            index_sets.append(column_names(meta, [f.lstrip("-") for f in index.fields]))

    create_sql = collect_sql(connection, lambda e: e.create_model(model))
    # create_model() also creates auto-created M2M tables; those are reported
    # under their own through model, so drop them here.
    for field in meta.local_many_to_many:
        through = field.remote_field.through
        if through._meta.auto_created:
            through_sql = set(collect_sql(connection, lambda e: e.create_model(through)))
            create_sql = [s for s in create_sql if s not in through_sql]

    add_column_sql = {}
    for field in meta.local_concrete_fields:
        if field.primary_key or field.db_type(connection) is None:
            continue
        try:
            add_column_sql[field.column] = collect_sql(
                connection, lambda e, f=field: e.add_field(model, f)
            )
        except Exception as exc:  # keep going; the fix script will say so
            add_column_sql[field.column] = [f"-- could not generate: {exc}"]

    return {
        "app_label": meta.app_label,
        "model": meta.object_name,
        "db_table": meta.db_table,
        "managed": meta.managed,
        "auto_created": bool(meta.auto_created),
        "columns": columns,
        "unique_sets": unique_sets,
        "index_sets": index_sets,
        "create_sql": create_sql,
        "add_column_sql": add_column_sql,
    }


def describe_migrations(loader, app_labels):
    graph = loader.graph
    result = {}
    for (app, name), migration in sorted(loader.disk_migrations.items()):
        dependencies = []
        for dep_app, dep_name in migration.dependencies:
            if dep_name == "__first__":
                roots = graph.root_nodes(dep_app)
                dep_name = roots[0][1] if roots else dep_name
            elif dep_name == "__latest__":
                leaves = graph.leaf_nodes(dep_app)
                dep_name = leaves[0][1] if leaves else dep_name
            dependencies.append([dep_app, dep_name])
        result.setdefault(app, {"nodes": []})["nodes"].append(
            {
                "name": name,
                "dependencies": dependencies,
                "replaces": [list(r) for r in (migration.replaces or [])],
                "initial": bool(getattr(migration, "initial", False)),
                "creates": [
                    op.name.lower()
                    for op in migration.operations
                    if op.__class__.__name__ == "CreateModel"
                ],
            }
        )
    for app in app_labels:
        result.setdefault(app, {"nodes": []})
    return result


def describe_pending_changes(loader):
    from django.apps import apps
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
    from django.db.migrations.state import ProjectState

    autodetector = MigrationAutodetector(
        loader.project_state(),
        ProjectState.from_apps(apps),
        NonInteractiveMigrationQuestioner(specified_apps=None, dry_run=True),
    )
    changes = autodetector.changes(graph=loader.graph)
    return {
        app: [op.describe() for migration in migrations for op in migration.operations]
        for app, migrations in changes.items()
    }


def extract(settings_module, project_dir):
    original_engine = setup_django(settings_module, project_dir)

    import django
    from django.apps import apps
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    app_labels = [config.label for config in apps.get_app_configs()]
    models = []
    for model in apps.get_models(include_auto_created=True):
        if model._meta.proxy or model._meta.swapped:
            continue
        models.append(describe_model(model, connection))

    loader = MigrationLoader(None, ignore_no_migrations=True)
    try:
        pending, pending_error = describe_pending_changes(loader), ""
    except Exception:
        pending, pending_error = {}, traceback.format_exc()

    return {
        "django_version": django.get_version(),
        "settings_module": settings_module,
        "original_engine": original_engine,
        "apps": app_labels,
        "models": models,
        "migrations": describe_migrations(loader, app_labels),
        "pending_changes": pending,
        "pending_changes_error": pending_error,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-dir", default=os.getcwd())
    args = parser.parse_args(argv)
    data = extract(args.settings, os.path.abspath(args.project_dir))
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
