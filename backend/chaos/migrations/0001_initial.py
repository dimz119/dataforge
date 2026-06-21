# Phase 9 (Chaos Engine): the answer-key store (chaos_injections, §6.2) and the
# durable late-arrival buffer (late_arrival_buffer, §6.3). Class-T RLS on both
# tenant tables (§9.5 / M-6). Modelled flat here (the production partitioning of
# chaos_injections by recorded_at is applied by the partition manager separately).

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
import tenancy.domain.scoping
from django.db import migrations, models

import chaos.domain.models
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
        migrations.CreateModel(
            name="ChaosInjection",
            fields=[
                (
                    "injection_id",
                    models.UUIDField(
                        default=chaos.domain.models._uuid7_placeholder,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("stream_id", models.UUIDField()),
                ("shard_id", models.IntegerField()),
                ("mode", models.TextField()),
                ("event_id", models.UUIDField()),
                ("sequence_no", models.BigIntegerField()),
                ("occurred_at", models.DateTimeField()),
                ("canonical_emitted_at", models.DateTimeField()),
                ("details", models.JSONField(default=dict)),
                ("recorded_at", models.DateTimeField(default=django.utils.timezone.now)),
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
                "db_table": "chaos_injections",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        migrations.CreateModel(
            name="LateArrivalBufferEntry",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=chaos.domain.models._uuid7_placeholder,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("stream_id", models.UUIDField()),
                ("shard_id", models.IntegerField()),
                ("injection_id", models.UUIDField()),
                ("event_id", models.UUIDField()),
                ("envelope", models.JSONField()),
                ("due_at", models.DateTimeField()),
                ("state", models.TextField(default="pending")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
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
                "db_table": "late_arrival_buffer",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        migrations.AddConstraint(
            model_name="chaosinjection",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "mode__in",
                        (
                            "duplicates",
                            "late_arriving",
                            "missing",
                            "out_of_order",
                            "corrupted_values",
                            "nulls",
                            "schema_drift",
                        ),
                    )
                ),
                name="chaos_injections_mode_ck",
            ),
        ),
        migrations.AddIndex(
            model_name="chaosinjection",
            index=models.Index(
                fields=["stream_id", "mode", "recorded_at"],
                name="chaos_inj_stream_mode_ix",
            ),
        ),
        migrations.AddIndex(
            model_name="chaosinjection",
            index=models.Index(
                fields=["stream_id", "event_id"], name="chaos_injections_event_ix"
            ),
        ),
        migrations.AddConstraint(
            model_name="latearrivalbufferentry",
            constraint=models.CheckConstraint(
                condition=models.Q(("state__in", ("pending", "emitted", "discarded"))),
                name="late_buffer_state_ck",
            ),
        ),
        migrations.AddIndex(
            model_name="latearrivalbufferentry",
            index=models.Index(
                condition=models.Q(("state", "pending")),
                fields=["due_at"],
                name="late_buffer_due_ix",
            ),
        ),
        migrations.AddIndex(
            model_name="latearrivalbufferentry",
            index=models.Index(fields=["stream_id", "state"], name="late_buffer_stream_ix"),
        ),
        migrations.AddIndex(
            model_name="latearrivalbufferentry",
            index=models.Index(fields=["workspace"], name="late_buffer_ws_ix"),
        ),
        # --- Layer 2 RLS (database-schema §9.5; M-6 ships RLS with the table) ---
        EnableRowLevelSecurity(
            table="chaos_injections",
            policy_class="T",
            model_label="chaos.ChaosInjection",
        ),
        EnableRowLevelSecurity(
            table="late_arrival_buffer",
            policy_class="T",
            model_label="chaos.LateArrivalBufferEntry",
        ),
    ]
