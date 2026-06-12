from django.apps import AppConfig


class CatalogConfig(AppConfig):
    """Scenario Catalog bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "catalog"
    default_auto_field = "django.db.models.BigAutoField"
