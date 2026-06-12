"""Celery application — control plane only (backend-architecture §7; ADR-0006).

Celery executes commands and jobs about streams, never the streams themselves:
no generation task exists, so the data plane cannot leak into Celery. Queue
topology per §7.1; settings per §7.2.
"""

import os
from typing import Any

from celery import Celery
from celery.signals import celeryd_init
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("dataforge")
app.config_from_object("django.conf:settings", namespace="CELERY")

# The five control-plane queues (backend-architecture §7.1).
app.conf.task_queues = (
    Queue("control"),
    Queue("lifecycle"),
    Queue("validation"),
    Queue("exports"),
    Queue("maintenance"),
)
app.conf.task_default_queue = "control"

# Contractual settings (backend-architecture §7.2).
app.conf.task_acks_late = True  # paired with: every task is idempotent (re-run safe)
app.conf.task_reject_on_worker_lost = True
app.conf.task_ignore_result = True  # jobs persist their own status rows; API never polls
app.conf.broker_transport_options = {"visibility_timeout": 3600}
app.conf.worker_prefetch_multiplier = 1
# Explicit per-task queue routing; populated as tasks land so no task ever
# rides the default queue by accident (§7.2 CI check, Phase 2+).
app.conf.task_routes = {}

app.autodiscover_tasks(related_name="tasks")


@celeryd_init.connect
def _label_worker_service(**_kwargs: Any) -> None:
    """Re-label logging for the `worker` process group (observability §2.2).

    This runs only when a Celery worker boots — `config` is imported by every
    process, so the label cannot be set at module import time.
    """
    os.environ.setdefault("DF_SERVICE", "worker")

    from django.conf import settings

    from config.logging import configure_logging

    configure_logging(
        service=os.environ["DF_SERVICE"],
        env_name=settings.DF_ENV,
        release=settings.RELEASE,
        level=settings.LOG_LEVEL,
        per_logger_levels=settings.DF_LOG_LEVELS,
    )
