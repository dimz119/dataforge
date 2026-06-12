from django.apps import AppConfig


class StreamsConfig(AppConfig):
    """Stream Control bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "streams"
    default_auto_field = "django.db.models.BigAutoField"
