from django.apps import AppConfig


class TenancyConfig(AppConfig):
    """Tenancy bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "tenancy"
    default_auto_field = "django.db.models.BigAutoField"
