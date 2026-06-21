"""Serializers for the Stream Control context — the only payload boundary
(backend-architecture §6).

Two request shapes (create + the lifecycle verbs take no body) and one response
shape (the Stream resource, api-spec §4.8). The pin block is read-only on the wire:
the API has no mutation path for ``manifest_version``/``seed``/``pinned_config``
(domain-model §4.4, INV-STR-5) — they are copied at create and surfaced read-only.
``seed`` is rendered as a STRING (api-spec §4.8: a 63-bit integer that JS would lose
precision on).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

# v1 request bounds (api-spec §4.8): target_tps 1..1000; speed_multiplier 0.1..1000.
_TPS_MIN = 1
_TPS_MAX = 1000
_SEED_MAX = (2**63) - 1


class VirtualClockInputSerializer(serializers.Serializer[Any]):
    """The optional virtual-clock block on create (api-spec §4.8 body)."""

    virtual_epoch = serializers.DateTimeField(required=False)
    speed_multiplier = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0.1"),
        max_value=Decimal("1000.0"),
        required=False,
    )


class StreamCreateSerializer(serializers.Serializer[Any]):
    """``POST /streams`` body (api-spec §4.8).

    ``seed`` is optional (server-generated when omitted) and accepted as a string or
    integer in [0, 2**63-1]. ``target_tps`` defaults to 10, bounded 1..1000 (the v1
    request bound; the plan cap is a separate 403 quota check at command time).
    ``chaos`` is the optional initial policy. v1 runs ``live`` mode only (backfill is
    the datasets resource), so ``clock_mode`` is fixed to ``live`` here.
    """

    workspace_id = serializers.UUIDField()
    scenario_instance_id = serializers.UUIDField()
    name = serializers.CharField(min_length=1, max_length=100)
    seed = serializers.CharField(required=False, allow_null=True)
    target_tps = serializers.IntegerField(
        required=False, default=10, min_value=_TPS_MIN, max_value=_TPS_MAX
    )
    chaos = serializers.DictField(required=False, default=dict)
    # Per-subject schema pins (schema-registry §10.1): {subject: version}. Empty (the
    # default) = latest-at-first-start (PIN-R1); explicit entries override per subject
    # (PIN-R2). The values must be positive integers (a registered version number);
    # the subject-emits + version-exists checks (PIN-R3) run in the service against the
    # pinned manifest → 422 with errors[]. Part of the determinism pin (INV-STR-5).
    schema_version_pins = serializers.DictField(
        child=serializers.IntegerField(min_value=1), required=False, default=dict
    )
    virtual_clock = VirtualClockInputSerializer(required=False)

    def validate_seed(self, value: str | None) -> int | None:
        if value is None or value == "":
            return None
        try:
            seed = int(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("seed must be an integer string.") from exc
        if not (0 <= seed <= _SEED_MAX):
            raise serializers.ValidationError(
                f"seed must be in [0, {_SEED_MAX}] (the R-3 domain)."
            )
        return seed


class StreamPatchSerializer(serializers.Serializer[Any]):
    """``PATCH /streams/{id}`` body (api-spec §4.8.2; RFC 7386 merge-patch).

    Mutable fields: ``name`` and ``target_tps`` (PIN-3). ``target_tps`` is bounded
    1..1,000 (out of range → 400 ``validation-error``); the plan per-stream cap is a
    separate 403 ``quota-exceeded`` check at command time (INV-TEN-5). Everything else
    on the stream is pinned (PIN-4) — patching an immutable field is rejected with a
    ``validation-error`` whose ``errors[0].code = "immutable_field"`` (handled in the
    view, which masks unknown/pinned keys before serializer validation).
    """

    name = serializers.CharField(min_length=1, max_length=100, required=False)
    target_tps = serializers.IntegerField(
        required=False, min_value=_TPS_MIN, max_value=_TPS_MAX
    )


class _DesiredStateSerializer(serializers.Serializer[Any]):
    run_state = serializers.CharField()
    target_tps = serializers.IntegerField()


class _VirtualClockSerializer(serializers.Serializer[Any]):
    virtual_epoch = serializers.DateTimeField()
    speed_multiplier = serializers.DecimalField(max_digits=8, decimal_places=2)
    virtual_now = serializers.DateTimeField(allow_null=True, required=False)


class StreamResponseSerializer(serializers.Serializer[Any]):
    """The Stream resource on the wire (api-spec §4.8). Read-only output shape.

    ``status`` is the surfaced lifecycle string (``running``, ``paused_quota``, …);
    ``status_reason`` is the raw reason. ``seed`` and ``pin_sha256`` are the
    determinism fingerprint surfaced read-only (INV-STR-5).
    """

    stream_id = serializers.UUIDField()
    workspace_id = serializers.UUIDField()
    scenario_instance_id = serializers.UUIDField()
    name = serializers.CharField()
    scenario_slug = serializers.CharField()
    manifest_version = serializers.CharField()
    config_revision = serializers.IntegerField()
    pin_sha256 = serializers.CharField()
    seed = serializers.CharField()
    status = serializers.CharField()
    status_reason = serializers.CharField()
    desired_state = _DesiredStateSerializer()
    virtual_clock = _VirtualClockSerializer()
    # The effective per-subject schema-version map (schema-registry §10.2, additive
    # Phase 10 response field per V-2): {subject: version}, folding the materialized
    # pin with the highest applied upgrade target. ``{}`` before first start (the
    # materialized map is resolved once at T1→T3 and lives in the checkpoint).
    schema_versions = serializers.DictField(child=serializers.IntegerField())
    shard_count = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    last_transition_at = serializers.DateTimeField(allow_null=True)


class SchemaUpgradeCreateSerializer(serializers.Serializer[Any]):
    """``POST /streams/{id}/schema-upgrades`` body (api-spec §4.8.4 / schema-registry §10.3).

    ``subject`` is the dotted subject name; ``target_version`` the registered version
    to evolve to (≥ 1). ``at`` is the SIMULATED-time cutover instant (``occurred_at``
    domain) — optional; omitted means "the next tick boundary" (effectively
    immediately). The REG-U001..U007 semantic checks run in the service against the
    stream's pinned manifest + virtual clock (this serializer only shapes the wire).
    """

    subject = serializers.CharField(min_length=1, max_length=255)
    target_version = serializers.IntegerField(min_value=1)
    at = serializers.DateTimeField(required=False, allow_null=True)


class SchemaUpgradeResponseSerializer(serializers.Serializer[Any]):
    """The schema-upgrade resource on the wire (api-spec §4.8.4 #50-52).

    The ``scheduled`` 201 carries ``upgrade_id``/``status``/``created_at``; ``applied``
    entries additionally carry ``applied_at_wall`` and the per-shard
    ``applied_sequence_no`` written by the runner cutover (§10.4); ``cancelled`` entries
    carry ``cancelled_at``. ``at`` is the simulated-time cutover instant (null ⇒ next
    tick). The output is assembled from the persisted jsonb entry (the runner and the
    cancel path complete the optional members in place).
    """

    upgrade_id = serializers.UUIDField()
    stream_id = serializers.UUIDField()
    subject = serializers.CharField()
    target_version = serializers.IntegerField()
    at = serializers.DateTimeField(allow_null=True)
    status = serializers.ChoiceField(choices=["scheduled", "applied", "cancelled"])
    created_at = serializers.DateTimeField()
    applied_at_wall = serializers.DateTimeField(allow_null=True, required=False)
    applied_sequence_no = serializers.IntegerField(allow_null=True, required=False)
    cancelled_at = serializers.DateTimeField(allow_null=True, required=False)


class StreamSchemaVersionsResponseSerializer(serializers.Serializer[Any]):
    """``GET /streams/{id}/schema-versions`` → ``{effective, pending, applied}`` (§10.2).

    ``effective`` is the per-subject effective-version map
    (``effective = max(materialized pin, highest applied upgrade target)``); ``{}``
    before first start. ``pending`` is the ``scheduled`` upgrade entries (awaiting
    their simulated-time cutover); ``applied`` the ``applied`` entries (each carrying
    ``applied_at_wall`` + ``applied_sequence_no``). Cancelled entries are history,
    surfaced only on the upgrade-list endpoint, not here.
    """

    effective = serializers.DictField(child=serializers.IntegerField())
    pending = SchemaUpgradeResponseSerializer(many=True)
    applied = SchemaUpgradeResponseSerializer(many=True)


class _StatsBufferSerializer(serializers.Serializer[Any]):
    earliest_available_at = serializers.DateTimeField(allow_null=True)
    latest_event_at = serializers.DateTimeField(allow_null=True)
    retention_hours = serializers.IntegerField()


class _StatsVirtualClockSerializer(serializers.Serializer[Any]):
    virtual_now = serializers.DateTimeField(allow_null=True)
    speed_multiplier = serializers.FloatField()


class StreamStatsResponseSerializer(serializers.Serializer[Any]):
    """The ``GET /streams/{id}/stats`` response (api-spec §4.11.1, Phase 6).

    Redis-resident, rebuildable counters (INV-OBS-2): ``total_events``,
    ``observed_tps`` (10 s sliding window), ``by_event_type``, ``last_event_at``.
    ``health`` is the closed enum (``healthy``/``degraded``/``stale``), ``null`` for a
    non-live stream. The shape is assembled by the delivery stats service; this
    serializer is the OpenAPI/output contract.
    """

    stream_id = serializers.UUIDField()
    status = serializers.CharField()
    health = serializers.ChoiceField(
        choices=["healthy", "degraded", "stale"], allow_null=True
    )
    total_events = serializers.IntegerField()
    observed_tps = serializers.FloatField()
    target_tps = serializers.IntegerField()
    last_event_at = serializers.DateTimeField(allow_null=True)
    by_event_type = serializers.DictField(child=serializers.IntegerField())
    buffer = _StatsBufferSerializer()
    virtual_clock = _StatsVirtualClockSerializer()
    as_of = serializers.DateTimeField()
