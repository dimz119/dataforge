"""Serializers for the Generation context — the dataset (backfill batch) payload
boundary (api-spec §4.10; backend-architecture §6).

The only place dataset request/response shapes are defined. ``seed`` is carried as
a string in the API (api-spec §4.10.1 ``"424242424242"``) to avoid JS precision
loss on the 63-bit domain; it is parsed to an int in the serializer.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from dataforge_engine.seeds import SEED_MAX, SEED_MIN
from generation.domain.models import COMPRESSIONS

__all__ = ["DatasetCreateSerializer", "DatasetSerializer"]


class DatasetCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST /datasets`` request body (api-spec §4.10.1)."""

    workspace_id = serializers.UUIDField()
    scenario_instance_id = serializers.UUIDField()
    name = serializers.CharField(max_length=200)
    seed = serializers.CharField(required=False, allow_null=True, default=None)
    simulated_days = serializers.IntegerField(min_value=1)
    virtual_epoch = serializers.DateTimeField(required=False, allow_null=True, default=None)
    compression = serializers.ChoiceField(choices=COMPRESSIONS, default="gzip")
    # ``chaos`` is accepted positionally per event-model §3.4; chaos application is
    # the chaos engine's concern (Phase 9). Stored-but-unused here so the contract
    # is stable.
    chaos = serializers.DictField(required=False, default=dict)

    def validate_seed(self, value: str | None) -> int | None:
        if value is None or value == "":
            return None
        try:
            seed = int(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("seed must be an integer string.") from exc
        if not SEED_MIN <= seed <= SEED_MAX:
            raise serializers.ValidationError(
                f"seed must be in [{SEED_MIN}, {SEED_MAX}] (R-3)."
            )
        return seed


class DatasetSerializer(serializers.Serializer[dict[str, Any]]):
    """The dataset resource (api-spec §4.10.2)."""

    dataset_id = serializers.UUIDField()
    workspace_id = serializers.UUIDField()
    scenario_instance_id = serializers.UUIDField()
    name = serializers.CharField()
    status = serializers.CharField()
    progress = serializers.FloatField()
    seed = serializers.CharField()
    pin_sha256 = serializers.CharField()
    simulated_window = serializers.DictField()
    estimated_events = serializers.IntegerField()
    event_count = serializers.IntegerField(allow_null=True)
    size_bytes = serializers.IntegerField(allow_null=True)
    compression = serializers.CharField()
    created_at = serializers.DateTimeField()
    ready_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    failure_reason = serializers.CharField(allow_blank=True)
    download_path = serializers.CharField()
