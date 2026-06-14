# Generated for Phase 5 (Streaming Runtime); hand-finished with the partitioned
# event_buffer parent (database-schema §6.1; delivery-channels §4), Class-T RLS
# (§9.5 / §9.7 / M-6), and the SeparateDatabaseAndState wrapper that registers the
# buffer model in state while the partitioned parent is created by raw DDL.


import django.db.models.deletion
import django.db.models.manager
import tenancy.domain.scoping
from django.db import migrations, models

from delivery.infra.migration_ops import CreateBufferParent
from tenancy.infra.rls import EnableRowLevelSecurity

_SCOPED_MANAGERS = [
    ("objects", django.db.models.manager.Manager()),
    ("all_objects", tenancy.domain.scoping.AllObjectsManager()),
]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenancy", "0002_api_key_auth_bootstrap_rls"),
    ]

    operations = [
        # --- event_buffer (§6.1): state declares the model, the DB side is the
        # partitioned-parent DDL (CreateBufferParent). No FK (C-7). ---
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="EventBuffer",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("stream_id", models.UUIDField()),
                        ("partition_ts", models.DateTimeField()),
                        ("buffer_seq", models.BigIntegerField()),
                        ("event_id", models.UUIDField()),
                        ("event_type", models.TextField()),
                        ("occurred_at", models.DateTimeField()),
                        ("emitted_at", models.DateTimeField()),
                        ("envelope", models.JSONField()),
                        (
                            "workspace",
                            models.ForeignKey(
                                db_column="workspace_id",
                                db_constraint=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="tenancy.workspace",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "event_buffer",
                        "abstract": False,
                        "base_manager_name": "all_objects",
                    },
                    managers=_SCOPED_MANAGERS,
                ),
            ],
            database_operations=[CreateBufferParent()],
        ),
        # --- Layer 2 RLS (database-schema §9.5; M-6 ships RLS with the table) ---
        # Class T on the partitioned parent (§9.7); CreateBufferParent applies the
        # same template to each hourly partition. No-op on SQLite.
        EnableRowLevelSecurity(
            table="event_buffer",
            policy_class="T",
            model_label="delivery.EventBuffer",
        ),
    ]
