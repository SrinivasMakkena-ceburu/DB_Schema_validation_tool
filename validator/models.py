import uuid

from django.db import models

from . import crypto


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

    name = models.CharField(max_length=100, unique=True, help_text="e.g. stage, prod-eu, prod-us")
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=5432)
    dbname = models.CharField("Database name", max_length=255)
    user = models.CharField(max_length=255)
    password_encrypted = models.TextField(blank=True)
    sslmode = models.CharField(max_length=20, choices=SSLMODES, default="prefer")
    schema = models.CharField(max_length=255, default="public")
    notes = models.TextField(blank=True, help_text="Release / feature set of this environment")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

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
