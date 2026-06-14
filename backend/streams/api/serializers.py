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
    shard_count = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    last_transition_at = serializers.DateTimeField(allow_null=True)
