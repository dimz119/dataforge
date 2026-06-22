"""Celery application — control plane only (backend-architecture §7; ADR-0006).

Celery executes commands and jobs about streams, never the streams themselves:
no generation task exists, so the data plane cannot leak into Celery. Queue
topology per §7.1; settings per §7.2.
"""

import os
from typing import Any

from celery import Celery
from celery.schedules import crontab
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
app.conf.task_routes = {
    # Phase 4: the backfill batch generation job rides the exports queue (§7.1).
    "generation.generate_dataset": {"queue": "exports"},
    # Phase 4: the Layer-3 dry-run validation job rides the validation queue
    # (plugin-arch §8.4; backend-architecture §7.1).
    "catalog.validate_manifest_l3": {"queue": "validation"},
    # Phase 5: Stream Control lifecycle supervision (lifecycle queue, §7.1).
    "streams.lease_expiry_watchdog": {"queue": "lifecycle"},
    "streams.system_pause_stream": {"queue": "lifecycle"},
    "streams.fail_stream": {"queue": "lifecycle"},
    # Phase 5: partition maintenance (maintenance queue, §7.1; ADR-0013 retention).
    "streams.maintain_ledger_partitions": {"queue": "maintenance"},
    "streams.maintain_buffer_partitions": {"queue": "maintenance"},
    # Phase 11: ledger archive-to-Parquet (maintenance queue, §9.2-9.3 retention).
    "generation.archive_ledger_partitions": {"queue": "maintenance"},
}

# Beat schedule (control plane only; backend-architecture §7.1, §7.4). The beat
# scheduler runs inside the worker group under a Redis singleton lock (§7.4) so
# scaling worker machines never double-fires. Phase 5 schedules:
#   - the lease-expiry watchdog (T4/T11) on a tight cadence so a stuck start fails
#     within the 60 s window (domain-model §4.3);
#   - event_buffer hourly partition create/drop (database-schema §6.1, 24 h);
#   - ground_truth_ledger daily partition create/drop (database-schema §5.5, 7 d).
app.conf.beat_schedule = {
    "streams-lease-expiry-watchdog": {
        "task": "streams.lease_expiry_watchdog",
        "schedule": 15.0,  # every 15 s (lease TTL); detects no-lease within the window
    },
    "streams-maintain-buffer-partitions": {
        "task": "streams.maintain_buffer_partitions",
        "schedule": 3600.0,  # hourly (event_buffer hourly partitions, §6.1)
    },
    "streams-maintain-ledger-partitions": {
        "task": "streams.maintain_ledger_partitions",
        "schedule": 86400.0,  # daily (ground_truth_ledger daily partitions, §5.5)
    },
    "streams-idle-auto-pause": {
        "task": "streams.idle_auto_pause",
        "schedule": 300.0,  # every 5 min: pause streams idle past idle_pause_minutes (P11-07)
    },
    "generation-archive-ledger-partitions": {
        "task": "generation.archive_ledger_partitions",
        # Daily 02:00 UTC (deployment-architecture §9.2): export ledger partitions
        # older than the 48 h hot window to Parquet, verify counts, then drop.
        "schedule": crontab(hour=2, minute=0),
    },
}

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

    # Expose this worker's df_ metrics on DF_METRICS_PORT (observability §4). The
    # beat scheduler runs inside the worker group (§7.4), so one exposer per worker
    # process covers both worker and beat series.
    from observation.infra import metrics

    metrics.start_metrics_server(settings.DF_METRICS_PORT)
