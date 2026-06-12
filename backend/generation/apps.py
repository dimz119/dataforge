from django.apps import AppConfig


class GenerationConfig(AppConfig):
    """Generation bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "generation"
    default_auto_field = "django.db.models.BigAutoField"
