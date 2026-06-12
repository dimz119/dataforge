from django.apps import AppConfig


class RegistryConfig(AppConfig):
    """Schema Registry bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "registry"
    default_auto_field = "django.db.models.BigAutoField"
