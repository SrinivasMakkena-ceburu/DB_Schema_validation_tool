from django.apps import AppConfig


class ValidatorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validator"
    verbose_name = "Schema Sync Validator"
