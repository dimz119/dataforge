from django.apps import AppConfig


class IdentityConfig(AppConfig):
    """Identity bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "identity"
    default_auto_field = "django.db.models.BigAutoField"
