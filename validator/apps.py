from django.apps import AppConfig


class ValidatorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validator"
    verbose_name = "Schema Sync Validator"

    def ready(self):
        from django.db.backends.signals import connection_created

        connection_created.connect(_sqlite_pragmas)


def _sqlite_pragmas(sender, connection, **kwargs):
    # Background jobs write while pages read: WAL avoids "database is locked".
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
