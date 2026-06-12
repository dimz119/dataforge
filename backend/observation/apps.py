from django.apps import AppConfig


class ObservationConfig(AppConfig):
    """Observation bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "observation"
    default_auto_field = "django.db.models.BigAutoField"
