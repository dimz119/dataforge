# Generated for Phase 4 (Generation Core + Batch Datasets); hand-finished with the
# partitioned ledger parent (database-schema §5.5), Class-T RLS on every tenant
# table (§9.5 / M-6), and the SeparateDatabaseAndState wrapper that registers the
# ledger model in state while the partitioned parent is created by raw DDL.


import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
import tenancy.domain.scoping
from django.db import migrations, models

import generation.domain.models
from generation.infra.migration_ops import CreateLedgerParent
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
        # --- ground_truth_ledger (§5.5): state declares the model, the DB side is
        # the partitioned-parent DDL (CreateLedgerParent). No FK (C-7). ---
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="GroundTruthLedger",
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
                        ("shard_id", models.IntegerField()),
                        ("sequence_no", models.BigIntegerField()),
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
                        "db_table": "ground_truth_ledger",
                        "abstract": False,
                        "base_manager_name": "all_objects",
                    },
                    managers=_SCOPED_MANAGERS,
                ),
            ],
            database_operations=[CreateLedgerParent()],
        ),
        # --- stream_checkpoints (§5.3) ---
        migrations.CreateModel(
            name="StreamCheckpoint",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("stream_id", models.UUIDField()),
                ("shard_id", models.IntegerField()),
                ("checkpoint_seq", models.BigIntegerField()),
                ("fencing_token", models.BigIntegerField(default=0)),
                ("state", models.BinaryField()),
                ("state_format", models.IntegerField(default=1)),
                ("last_sequence_no", models.BigIntegerField()),
                ("virtual_clock_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
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
                "db_table": "stream_checkpoints",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        # --- entity_pool_snapshots (§5.4) ---
        migrations.CreateModel(
            name="EntityPoolSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("stream_id", models.UUIDField()),
                ("shard_id", models.IntegerField()),
                ("entity_type", models.TextField()),
                ("snapshot_epoch", models.BigIntegerField()),
                ("fencing_token", models.BigIntegerField(default=0)),
                ("payload", models.BinaryField()),
                ("entity_count", models.IntegerField()),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
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
                "db_table": "entity_pool_snapshots",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        # --- datasets (api-spec §4.10) ---
        migrations.CreateModel(
            name="Dataset",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=generation.domain.models._uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("scenario_instance_id", models.UUIDField()),
                ("name", models.TextField()),
                (
                    "status",
                    models.TextField(
                        choices=[
                            ("queued", "Queued"),
                            ("generating", "Generating"),
                            ("ready", "Ready"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                        ],
                        default="queued",
                    ),
                ),
                ("progress", models.FloatField(default=0.0)),
                ("seed", models.BigIntegerField()),
                (
                    "stream_id",
                    models.UUIDField(
                        default=generation.domain.models._uuid4, editable=False
                    ),
                ),
                ("pin_sha256", models.TextField(default="")),
                ("simulated_from", models.DateTimeField()),
                ("simulated_to", models.DateTimeField()),
                ("estimated_events", models.BigIntegerField(default=0)),
                ("event_count", models.BigIntegerField(blank=True, null=True)),
                ("size_bytes", models.BigIntegerField(blank=True, null=True)),
                (
                    "compression",
                    models.TextField(
                        choices=[("gzip", "gzip"), ("none", "none")], default="gzip"
                    ),
                ),
                ("file_path", models.TextField(default="")),
                ("failure_reason", models.TextField(default="")),
                ("created_by", models.UUIDField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("ready_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "workspace",
                    models.ForeignKey(
                        db_column="workspace_id",
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="datasets",
                        to="tenancy.workspace",
                    ),
                ),
            ],
            options={
                "db_table": "datasets",
                "abstract": False,
                "base_manager_name": "all_objects",
            },
            managers=_SCOPED_MANAGERS,
        ),
        # --- indexes + constraints ---
        migrations.AddIndex(
            model_name="streamcheckpoint",
            index=models.Index(fields=["workspace"], name="stream_checkpoints_ws_ix"),
        ),
        migrations.AddConstraint(
            model_name="streamcheckpoint",
            constraint=models.UniqueConstraint(
                fields=("stream_id", "shard_id"), name="stream_checkpoints_pk"
            ),
        ),
        migrations.AddIndex(
            model_name="entitypoolsnapshot",
            index=models.Index(fields=["workspace"], name="entity_pool_snapshots_ws_ix"),
        ),
        migrations.AddConstraint(
            model_name="entitypoolsnapshot",
            constraint=models.UniqueConstraint(
                fields=("stream_id", "shard_id", "entity_type"),
                name="entity_pool_snapshots_pk",
            ),
        ),
        migrations.AddIndex(
            model_name="dataset",
            index=models.Index(fields=["workspace", "status"], name="datasets_ws_status_ix"),
        ),
        migrations.AddIndex(
            model_name="dataset",
            index=models.Index(
                fields=["workspace", "-created_at"], name="datasets_ws_created_ix"
            ),
        ),
        migrations.AddConstraint(
            model_name="dataset",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ("queued", "generating", "ready", "failed", "expired"))
                ),
                name="datasets_status_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="dataset",
            constraint=models.CheckConstraint(
                condition=models.Q(("seed__gte", 0)), name="datasets_seed_ck"
            ),
        ),
        # --- Layer 2 RLS (database-schema §9.5; M-6 ships RLS with the table) ---
        # Class T on every tenant table; the ledger policy attaches to the
        # partitioned parent (§9.7) and CreateLedgerParent applies it to each
        # daily partition too. No-op on SQLite.
        EnableRowLevelSecurity(
            table="ground_truth_ledger",
            policy_class="T",
            model_label="generation.GroundTruthLedger",
        ),
        EnableRowLevelSecurity(
            table="stream_checkpoints",
            policy_class="T",
            model_label="generation.StreamCheckpoint",
        ),
        EnableRowLevelSecurity(
            table="entity_pool_snapshots",
            policy_class="T",
            model_label="generation.EntityPoolSnapshot",
        ),
        EnableRowLevelSecurity(
            table="datasets", policy_class="T", model_label="generation.Dataset"
        ),
    ]
