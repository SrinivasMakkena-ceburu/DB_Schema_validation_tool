import fnmatch
import uuid

from django.db import models

from . import crypto
from .schema_diff import finding_object


class Project(models.Model):
    """A local checkout of a Django backend."""

    name = models.CharField(max_length=100, unique=True)
    path = models.CharField(max_length=500, help_text="Folder containing manage.py")
    python_path = models.CharField(
        max_length=500, help_text="The project's venv interpreter, e.g. /path/venv/bin/python"
    )
    settings_module = models.CharField(
        max_length=200, blank=True, help_text="e.g. config.settings — read from manage.py if blank"
    )
    extra_env = models.TextField(
        blank=True, help_text="KEY=VALUE per line, needed for the settings to import"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def env_dict(self):
        env = {}
        for line in self.extra_env.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
        return env


class DatabaseTarget(models.Model):
    """One environment's PostgreSQL database."""

    SSLMODES = [(m, m) for m in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")]
    ENVIRONMENTS = [("dev", "dev"), ("stage", "stage"), ("prod", "prod")]

    name = models.CharField(max_length=100, unique=True, help_text="e.g. stage, prod-eu, prod-us")
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=5432)
    dbname = models.CharField("Database name", max_length=255)
    user = models.CharField(max_length=255)
    password_encrypted = models.TextField(blank=True)
    sslmode = models.CharField(max_length=20, choices=SSLMODES, default="prefer")
    schema = models.CharField(max_length=255, default="public")
    notes = models.TextField(blank=True, help_text="Release / feature set of this environment")
    environment = models.CharField(max_length=10, choices=ENVIRONMENTS, default="dev")
    writes_enabled = models.BooleanField(
        default=False, help_text="Allow deletes, cleanups and column drops on this database from the UI"
    )
    write_user = models.CharField(
        max_length=255, blank=True, help_text="Separate user for writes; blank uses the user above"
    )
    write_password_encrypted = models.TextField(blank=True)
    operation_timeout_s = models.PositiveIntegerField(
        "Operation timeout (seconds)", default=300, help_text="statement_timeout for write operations"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_prod(self):
        return self.environment == "prod"

    def set_write_password(self, plain):
        self.write_password_encrypted = crypto.encrypt(plain) if plain else ""

    def write_conninfo(self):
        info = self.conninfo()
        if self.write_user:
            info["user"] = self.write_user
            info.pop("password", None)
            if self.write_password_encrypted:
                info["password"] = crypto.decrypt(self.write_password_encrypted)
        return info

    def set_password(self, plain):
        self.password_encrypted = crypto.encrypt(plain) if plain else ""

    def get_password(self):
        return crypto.decrypt(self.password_encrypted) if self.password_encrypted else ""

    @property
    def has_password(self):
        return bool(self.password_encrypted)

    def conninfo(self):
        info = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "sslmode": self.sslmode,
        }
        password = self.get_password()
        if password:
            info["password"] = password
        return info


class ComparisonRun(models.Model):
    """One project x one database comparison, kept as history."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"

    kind = models.CharField(max_length=10, default="branch")  # branch | db
    reference_name = models.CharField(max_length=100, blank=True)
    batch_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    project = models.ForeignKey(Project, null=True, on_delete=models.SET_NULL)
    database = models.ForeignKey(DatabaseTarget, null=True, on_delete=models.SET_NULL)
    project_name = models.CharField(max_length=100)
    database_name = models.CharField(max_length=100)
    git_branch = models.CharField(max_length=255, blank=True)
    git_commit = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    duration_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, default=STATUS_OK)
    error = models.TextField(blank=True)
    options = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    report = models.JSONField(default=dict)
    fix_sql = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.project_name} → {self.database_name} ({self.started_at:%Y-%m-%d %H:%M})"


class ProjectSnapshot(models.Model):
    """Cached extractor output for a project at a commit + working-tree state."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="snapshots")
    commit = models.CharField(max_length=64)
    status_hash = models.CharField(max_length=64)
    data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Job(models.Model):
    """Background work (comparisons and data operations) with progress."""

    STATUSES = ["queued", "running", "done", "failed", "cancelled", "interrupted"]

    kind = models.CharField(max_length=20)
    title = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, default="queued")
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=500, blank=True)
    log = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)
    result_url = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def finished(self):
        return self.status in ("done", "failed", "cancelled", "interrupted")


class IgnoreRule(models.Model):
    """A finding that is accepted drift: excluded from counts and fix scripts."""

    database = models.ForeignKey(DatabaseTarget, null=True, blank=True, on_delete=models.CASCADE,
                                 help_text="Blank = every database")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE,
                                help_text="Blank = every project")
    category = models.CharField(max_length=40, blank=True, help_text="Blank = any kind of finding")
    pattern = models.CharField(max_length=300, help_text="Object to match, wildcards allowed: legacy_*, billing*")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pattern"]

    def __str__(self):
        return self.pattern

    def matches(self, finding, database_id, project_id):
        if self.database_id and self.database_id != database_id:
            return False
        if self.project_id and self.project_id != project_id:
            return False
        if self.category and self.category != finding["category"]:
            return False
        return fnmatch.fnmatchcase(finding_object(finding), self.pattern)


class CleanupRecipe(models.Model):
    """A saved, reusable cleanup: rows of a table matching filters, deleted in batches."""

    name = models.CharField(max_length=100, unique=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL,
                                help_text="Used for cascade rules (on_delete); strongly recommended")
    root_table = models.CharField(max_length=255)
    filters = models.JSONField(default=list)
    batch_size = models.PositiveIntegerField(default=5000)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DataOperation(models.Model):
    """A delete, cleanup or column drop: planned, confirmed, backed up, executed, audited."""

    KINDS = [("delete", "Delete rows"), ("cleanup", "Cleanup"), ("drop_column", "Drop column")]

    kind = models.CharField(max_length=12, choices=KINDS)
    database = models.ForeignKey(DatabaseTarget, null=True, on_delete=models.SET_NULL)
    database_name = models.CharField(max_length=100)
    environment = models.CharField(max_length=10)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    recipe = models.ForeignKey(CleanupRecipe, null=True, blank=True, on_delete=models.SET_NULL)
    root_table = models.CharField(max_length=255)
    column = models.CharField(max_length=255, blank=True)
    filters = models.JSONField(default=list)
    cascade_overrides = models.JSONField(default=list)
    batch_size = models.PositiveIntegerField(default=0)
    plan = models.JSONField(default=dict)
    planned_at = models.DateTimeField(null=True, blank=True)
    rehearsal = models.JSONField(default=dict)
    confirmation = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=12, default="planning")
    counts = models.JSONField(default=dict)
    backup_dir = models.CharField(max_length=500, blank=True)
    sql_log = models.TextField(blank=True)
    error = models.TextField(blank=True)
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.root_table} on {self.database_name}"

    @property
    def rows_summary(self):
        if self.kind == "drop_column":
            return f"{self.plan.get('non_null', '?')} values" if self.plan else ""
        if self.counts.get("deleted"):
            return f"{sum(self.counts['deleted'].values())} deleted"
        planned = self.plan.get("root_total", self.plan.get("total_rows")) if self.plan else None
        return f"{planned} planned" if planned is not None else ""
