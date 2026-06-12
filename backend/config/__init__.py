"""DataForge Django project package — wiring only, no business code.

Exposes the Celery app so `@shared_task` autodiscovery works whenever Django
loads (backend-architecture §2.1).
"""

from config.celery import app as celery_app

__all__ = ["celery_app"]
