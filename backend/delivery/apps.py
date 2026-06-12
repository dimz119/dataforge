from django.apps import AppConfig


class DeliveryConfig(AppConfig):
    """Delivery bounded context (domain-model §1.3; backend-architecture §2.2)."""

    name = "delivery"
    default_auto_field = "django.db.models.BigAutoField"
