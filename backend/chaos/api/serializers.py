"""Serializers for the Chaos context — the only payload boundary
(backend-architecture §6).

Two surfaces: the live chaos-policy ``GET | PATCH`` (api-spec §4.8.3) and the
answer-key reads (api-spec §4.13). The PATCH body is a free-form mode-level merge
document validated by :mod:`chaos.application.validation` (the bounds live there,
not in field declarations), so its serializer is a permissive ``DictField`` whose
real contract is the §3.4 validator. The response serializers are the OpenAPI
output shapes only.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


class ChaosPolicyResponseSerializer(serializers.Serializer[Any]):
    """The live ChaosPolicy resource (api-spec §4.8.3 GET/PATCH 200)."""

    stream_id = serializers.UUIDField()
    modes = serializers.DictField()
    updated_at = serializers.DateTimeField()


class ChaosPolicyPatchSerializer(serializers.Serializer[Any]):
    """``PATCH /streams/{id}/chaos`` body (api-spec §4.8.3; chaos-engine §3.5).

    A partial document: only the mode keys to change (each replaced whole) plus an
    optional ``on_stop_policy``. The closed-shape + ``rate ≤ 0.5`` bounds (§3.4) are
    enforced by :func:`chaos.application.validation.validate_chaos_patch`, which the
    view runs before applying — a violation surfaces as ``422``
    ``manifest-validation-failed`` with chaos-scoped ``errors[]``.
    """

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise serializers.ValidationError("Chaos policy body must be an object.")
        return dict(data)


class AnswerKeyInjectionSerializer(serializers.Serializer[Any]):
    """One flattened InjectionRecord on the wire (api-spec §4.13 / event-model §7.3).

    Common fields are typed; mode-specific members (``copies``, ``delay_simulated_ms``,
    ``mutations``, …) are flattened from ``details`` and pass through as extra keys —
    this serializer documents the stable common shape, the view emits the full record.
    """

    injection_id = serializers.UUIDField()
    mode = serializers.CharField()
    stream_id = serializers.UUIDField()
    shard_id = serializers.IntegerField()
    event_id = serializers.UUIDField()
    sequence_no = serializers.IntegerField()
    occurred_at = serializers.DateTimeField()
    canonical_emitted_at = serializers.DateTimeField()
    recorded_at = serializers.DateTimeField()


class _AnswerKeyWindowSerializer(serializers.Serializer[Any]):
    from_ = serializers.DateTimeField(allow_null=True)
    to = serializers.DateTimeField(allow_null=True)

    def get_fields(self) -> Any:
        fields = super().get_fields()
        fields["from"] = fields.pop("from_")
        return fields


class AnswerKeySummarySerializer(serializers.Serializer[Any]):
    """The per-mode answer-key count aggregate (api-spec §4.13 summary)."""

    stream_id = serializers.UUIDField()
    window = _AnswerKeyWindowSerializer()
    by_mode = serializers.DictField()
    total_injections = serializers.IntegerField()
    as_of = serializers.DateTimeField()
