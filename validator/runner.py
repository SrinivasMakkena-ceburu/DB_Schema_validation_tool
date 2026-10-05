"""Run one comparison batch: a branch (or a reference database) against one or more databases."""
import time
import uuid

from django.utils import timezone

from .crypto import DecryptError
from .db_compare import db_as_models, diff_history_sets
from .db_inspector import InspectError, inspect_database
from .fix_sql import build_commands, build_fix_sql
from .ignore import SECTIONS, apply_ignore_rules
from .migration_diff import diff_migrations
from .models import ComparisonRun, IgnoreRule
from .project_loader import ExtractorError, git_info, load_project_cached
from .schema_diff import diff_schema, finding

SEVERITIES = ("error", "warning", "info")


def summarize(report):
    summary = {s: 0 for s in SEVERITIES}
    summary["by_category"] = {}
    summary["sections"] = {}
    for section in SECTIONS:
        counts = {s: 0 for s in SEVERITIES}
        for f in report.get(section, []):
            counts[f["severity"]] += 1
            summary[f["severity"]] += 1
            cat = summary["by_category"].setdefault(f["category"], {s: 0 for s in SEVERITIES})
            cat[f["severity"]] += 1
        summary["sections"][section] = counts
    summary["ignored"] = len(report.get("ignored", []))
    return summary


def compare(extracted, db_info, header, options, rules=(), database_id=None, project_id=None):
    """Branch vs database -> (report, fix_sql). Ignored findings are left out of counts and the fix."""
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
    apply_ignore_rules(report, rules, database_id, project_id)
    report["commands"] = build_commands(report["migrations"], per_app, options)
    fix = build_fix_sql(header=header, models=extracted["models"], schema_findings=report["schema"],
                        migration_findings=report["migrations"], pending_changes=extracted["pending_changes"],
                        options=options)
    return report, fix


def compare_databases(reference_info, db_info, rules=(), database_id=None):
    """Reference database vs database -> report (no fix SQL: compare a branch for that)."""
    apps = {r["app"] for r in (reference_info["migrations"] or [])}
    report = {
        "schema": diff_schema(db_as_models(reference_info, apps), db_info),
        "migrations": [],
        "pending": [],
        "per_app": {},
        "commands": [],
        "notes": [],
        "meta": {"db_tables": len(db_info["tables"]), "db_views": len(db_info["views"]),
                 "reference_tables": len(reference_info["tables"]),
                 "history_rows": len(db_info["migrations"] or [])},
    }
    if reference_info["migrations"] is None or db_info["migrations"] is None:
        report["notes"].append("One of the databases has no django_migrations table; history was not compared.")
    else:
        report["migrations"] = diff_history_sets(reference_info["migrations"], db_info["migrations"])
    apply_ignore_rules(report, rules, database_id, None)
    return report


def run_batch(project, databases, options, *, kind="branch", reference=None, job=None, force_refresh=False):
    """Compare `project` (or `reference`) with each database; returns the batch id."""
    from .jobs import update

    batch_id = uuid.uuid4()
    options = {**options, "kind": kind, "reference_id": reference.pk if reference else None}
    rules = list(IgnoreRule.objects.all())
    started = time.monotonic()
    source, source_error = None, ""
    branch = commit = ""
    if kind == "db":
        try:
            source = inspect_database(reference.conninfo(), reference.schema)
        except (InspectError, DecryptError) as exc:
            source_error = f"Reference {reference.name}: {exc}"
    else:
        branch, commit = git_info(project.path)
        if job:
            update(job, progress=5, message=f"Reading models of {project.name}")
        try:
            source = load_project_cached(project, force=force_refresh)
        except ExtractorError as exc:
            source_error = str(exc)
    prep_ms = int((time.monotonic() - started) * 1000)

    for index, database in enumerate(databases):
        if job:
            update(job, progress=10 + 85 * index / max(1, len(databases)), message=f"Inspecting {database.name}")
        t0 = time.monotonic()
        run = ComparisonRun(
            batch_id=batch_id, kind=kind, project=project, database=database,
            project_name=project.name if project else f"{reference.name} (database)",
            reference_name=reference.name if reference else "", database_name=database.name,
            git_branch=branch, git_commit=commit, options=options,
        )
        if source is None:
            run.status, run.error = ComparisonRun.STATUS_ERROR, source_error
        else:
            try:
                db_info = inspect_database(database.conninfo(), database.schema)
                if kind == "db":
                    run.report = compare_databases(source, db_info, rules, database.pk)
                else:
                    header = {"project": project.name, "branch": branch, "commit": commit,
                              "database": database.name, "schema": database.schema,
                              "generated": timezone.now().strftime("%Y-%m-%d %H:%M UTC")}
                    run.report, run.fix_sql = compare(source, db_info, header, options, rules, database.pk,
                                                      project.pk)
                run.summary = summarize(run.report)
            except (InspectError, DecryptError) as exc:
                run.status, run.error = ComparisonRun.STATUS_ERROR, f"{database.name}: {exc}"
        run.duration_ms = prep_ms + int((time.monotonic() - t0) * 1000)
        run.save()
    return batch_id


def finding_key(f):
    return f"{f['category']}|{f['table'] or f['app']}|{f['column']}"


def previous_run(run):
    return (ComparisonRun.objects
            .filter(kind=run.kind, project_name=run.project_name, database_name=run.database_name,
                    reference_name=run.reference_name, status=ComparisonRun.STATUS_OK, pk__lt=run.pk)
            .order_by("-pk").first())


def changes_since(run, previous):
    """Findings new in `run` (keys) and findings of `previous` that are gone."""
    def findings(r):
        return [f for section in SECTIONS for f in r.report.get(section, [])]

    current = {finding_key(f) for f in findings(run)}
    before = {finding_key(f): f for f in findings(previous)}
    return {"new": current - set(before), "resolved": [f for k, f in before.items() if k not in current]}
