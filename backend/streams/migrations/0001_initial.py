# Generated for Phase 5 (Streaming Runtime — Stream Control). Hand-finished with
# Class-T RLS on both tenant tables (database-schema §9.5 / M-6 ships RLS with the
# table) and the scoped-manager registration the check_tenancy guard requires.
#
# Two tables: streams (§5.1, the aggregate root + desired/lifecycle state + pin)
# and stream_shards (§5.2, the durable fencing-token authority). The
# stream_checkpoints FK → stream_shards(stream_id, shard_id) is created by the
# generation migration; this migration depends on generation so the FK target's
# unique constraint exists first.


import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
import tenancy.domain.scoping
from django.db import migrations, models

import streams.domain.models
from tenancy.infra.rls import EnableRowLevelSecurity

_SCOPED_MANAGERS = [
    ("objects", django.db.models.manager.Manager()),
    ("all_objects", tenancy.domain.scoping.AllObjectsManager()),
]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenancy", "0002_api_key_auth_bootstrap_rls"),
        ("generation", "0001_initial"),
    ]

    operations = [
        # --- streams (§5.1): the aggregate root ---
        migrations.CreateModel(
            name="Stream",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=streams.domain.models._uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("scenario_config_id", models.UUIDField()),
                ("scenario_slug", models.TextField()),
                ("name", models.TextField()),
                ("manifest_version", models.TextField()),
                ("scenario_definition_id", models.UUIDField()),
                ("pinned_config", models.JSONField(default=dict)),
                ("pinned_config_version", models.IntegerField(default=1)),
                ("pin_sha256", models.TextField(default="")),
                ("seed", models.BigIntegerField()),
                (
                    "desired_state",
                    models.TextField(
                        choices=[
                            ("running", "Running"),
                            ("paused", "Paused"),
                            ("stopped", "Stopped"),
                        ],
                        default="stopped",
                    ),
                ),
                ("target_tps", models.IntegerField(default=10)),
                ("chaos_config", models.JSONField(default=dict)),
                ("schema_version_pins", models.JSONField(default=dict)),
                ("schema_upgrade_schedule", models.JSONField(blank=True, null=True)),
                (
                    "lifecycle_state",
                    models.TextField(
                        choices=[
                            ("created", "Created"),
                            ("starting", "Starting"),
                            ("running", "Running"),
                            ("pausing", "Pausing"),
                            ("paused", "Paused"),
                            ("resuming", "Resuming"),
                            ("stopping", "Stopping"),
                            ("stopped", "Stopped"),
                            ("failed", "Failed"),
                        ],
                        default="created",
                    ),
                ),
                (
                    "status_reason",
                    models.TextField(
                        choices=[
                            ("none", "None"),
                            ("user", "User"),
                            ("quota", "Quota"),
                            ("idle", "Idle"),
                            ("error", "Error"),
                            ("failover_exhausted", "Failover Exhausted"),
                        ],
                        default="none",
                    ),
                ),
                ("virtual_epoch", models.DateTimeField()),
                (
                    "speed_multiplier",
                    models.DecimalField(decimal_places=2, default=1, max_digits=8),
                ),
                (
                    "clock_mode",
                    models.TextField(
                        choices=[("live", "Live"), ("backfill", "Backfill")],
                        default="live",
                    ),
                ),
                ("backfill_days", models.IntegerField(blank=True, null=True)),
                ("shard_count", models.IntegerField(default=1)),
                ("created_by", models.UUIDField()),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("first_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_transition_at", models.DateTimeField(blank=True, null=True)),
                (
                    "workspace",
                    models.ForeignKey(
                        db_column="workspace_id",
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="streams",
                        to="tenancy.workspace",
                    ),
                ),
            ],
            options={
                "db_table": "streams",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        # --- stream_shards (§5.2): the durable fencing-token authority ---
        migrations.CreateModel(
            name="StreamShard",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("stream_id", models.UUIDField()),
                ("shard_id", models.IntegerField()),
                ("fencing_token", models.BigIntegerField(default=0)),
                ("last_runner_id", models.TextField(blank=True, null=True)),
                ("last_acquired_at", models.DateTimeField(blank=True, null=True)),
                ("last_released_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
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
                "db_table": "stream_shards",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        # --- indexes + constraints (streams) ---
        migrations.AddIndex(
            model_name="stream",
            index=models.Index(
                fields=["workspace", "lifecycle_state"], name="streams_ws_ix"
            ),
        ),
        migrations.AddIndex(
            model_name="stream",
            index=models.Index(
                condition=~models.Q(("desired_state", "stopped")),
                fields=["desired_state", "lifecycle_state"],
                name="streams_reconcile_ix",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("desired_state__in", ("running", "paused", "stopped"))
                ),
                name="streams_desired_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(("target_tps__gte", 1), ("target_tps__lte", 100000)),
                name="streams_tps_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "lifecycle_state__in",
                        (
                            "created",
                            "starting",
                            "running",
                            "pausing",
                            "paused",
                            "resuming",
                            "stopping",
                            "stopped",
                            "failed",
                        ),
                    )
                ),
                name="streams_lifecycle_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status_reason__in",
                        ("none", "user", "quota", "idle", "error", "failover_exhausted"),
                    )
                ),
                name="streams_reason_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(("clock_mode__in", ("live", "backfill"))),
                name="streams_clock_mode_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(("seed__gte", 0)), name="streams_seed_ck"
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(("shard_count__gte", 1), ("shard_count__lte", 64)),
                name="streams_shard_count_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="stream",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("clock_mode", "backfill"), ("backfill_days__isnull", False)
                    ),
                    models.Q(("clock_mode", "live"), ("backfill_days__isnull", True)),
                    _connector="OR",
                ),
                name="streams_backfill_ck",
            ),
        ),
        # --- indexes + constraints (stream_shards) ---
        migrations.AddIndex(
            model_name="streamshard",
            index=models.Index(fields=["workspace"], name="stream_shards_ws_ix"),
        ),
        migrations.AddConstraint(
            model_name="streamshard",
            constraint=models.UniqueConstraint(
                fields=("stream_id", "shard_id"), name="stream_shards_pk"
            ),
        ),
        migrations.AddConstraint(
            model_name="streamshard",
            constraint=models.CheckConstraint(
                condition=models.Q(("shard_id__gte", 0)), name="stream_shards_shard_ck"
            ),
        ),
        # --- Layer 2 RLS (database-schema §9.5; M-6 ships RLS with the table) ---
        EnableRowLevelSecurity(
            table="streams", policy_class="T", model_label="streams.Stream"
        ),
        EnableRowLevelSecurity(
            table="stream_shards", policy_class="T", model_label="streams.StreamShard"
        ),
    ]
