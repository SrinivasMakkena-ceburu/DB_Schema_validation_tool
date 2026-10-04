"""Run one comparison batch: one project against one or more databases."""
import time
import uuid

from django.utils import timezone

from .crypto import DecryptError
from .db_inspector import InspectError, inspect_database
from .fix_sql import build_commands, build_fix_sql
from .migration_diff import diff_migrations
from .models import ComparisonRun
from .project_loader import ExtractorError, git_info, load_project
from .schema_diff import diff_schema, finding

SEVERITIES = ("error", "warning", "info")


def summarize(report):
    summary = {s: 0 for s in SEVERITIES}
    summary["by_category"] = {}
    summary["sections"] = {}
    for section in ("schema", "migrations", "pending"):
        counts = {s: 0 for s in SEVERITIES}
        for f in report[section]:
            counts[f["severity"]] += 1
            summary[f["severity"]] += 1
            cat = summary["by_category"].setdefault(f["category"], {s: 0 for s in SEVERITIES})
            cat[f["severity"]] += 1
        summary["sections"][section] = counts
    return summary


def compare(extracted, db_info, header, options):
    """Pure comparison of extractor output with inspector output -> (report, fix_sql)."""
    schema_findings = diff_schema(extracted["models"], db_info)
    migration_findings, per_app = diff_migrations(
        extracted["migrations"], db_info["migrations"], extracted["apps"],
        set(db_info["tables"]), extracted["models"],
    )
    pending = [
        finding("pending_change", "warning", app, "", f"No migration file for: {op}", data={"operation": op})
        for app, ops in sorted(extracted["pending_changes"].items())
        for op in ops
    ]
    notes = []
    engine = extracted.get("original_engine", "")
    if engine and "postgresql" not in engine and "postgis" not in engine:
        notes.append(f"The project's default database engine is {engine}; DDL is generated for PostgreSQL.")
    if extracted.get("pending_changes_error"):
        notes.append("Could not compute model changes without migrations:\n" + extracted["pending_changes_error"])
    report = {
        "schema": schema_findings,
        "migrations": migration_findings,
        "pending": pending,
        "per_app": per_app,
        "commands": build_commands(migration_findings, per_app, options),
        "notes": notes,
        "meta": {
            "django_version": extracted["django_version"],
            "settings_module": extracted["settings_module"],
            "models": len(extracted["models"]),
            "db_tables": len(db_info["tables"]),
            "db_views": len(db_info["views"]),
            "history_rows": len(db_info["migrations"] or []),
        },
    }
    fix = build_fix_sql(header=header, models=extracted["models"], schema_findings=schema_findings,
                        migration_findings=migration_findings, pending_changes=extracted["pending_changes"],
                        options=options)
    return report, fix


def run_batch(project, databases, options):
    """Compare `project` with each database; returns the batch id."""
    batch_id = uuid.uuid4()
    branch, commit = git_info(project.path)
    extract_started = time.monotonic()
    try:
        extracted, extract_error = load_project(project), ""
    except ExtractorError as exc:
        extracted, extract_error = None, str(exc)
    extract_ms = int((time.monotonic() - extract_started) * 1000)

    for database in databases:
        started = time.monotonic()
        run = ComparisonRun(
            batch_id=batch_id, project=project, database=database, project_name=project.name,
            database_name=database.name, git_branch=branch, git_commit=commit, options=options,
        )
        if extracted is None:
            run.status, run.error = ComparisonRun.STATUS_ERROR, extract_error
        else:
            header = {"project": project.name, "branch": branch, "commit": commit, "database": database.name,
                      "schema": database.schema, "generated": timezone.now().strftime("%Y-%m-%d %H:%M UTC")}
            try:
                db_info = inspect_database(database.conninfo(), database.schema)
                run.report, run.fix_sql = compare(extracted, db_info, header, options)
                run.summary = summarize(run.report)
            except (InspectError, DecryptError) as exc:
                run.status, run.error = ComparisonRun.STATUS_ERROR, f"{database.name}: {exc}"
        run.duration_ms = extract_ms + int((time.monotonic() - started) * 1000)
        run.save()
    return batch_id
