from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Audit bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "audit"
    default_auto_field = "django.db.models.BigAutoField"
