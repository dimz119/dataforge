from django.apps import AppConfig


class ChaosConfig(AppConfig):
    """Chaos bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "chaos"
    default_auto_field = "django.db.models.BigAutoField"
