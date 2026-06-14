"""Serializers for the Delivery context — the only payload boundary
(backend-architecture §6).

The REST cursor pull (delivery-channels §5.1): a query-param serializer validating
``cursor`` / ``from`` / ``limit`` / ``types`` (the request boundary), and a response
serializer documenting the ``{data, next_cursor}`` page envelope (api-spec §2.6) for
drf-spectacular. The cursor decode/expiry semantics live in the application service;
this layer only validates shapes and rejects mutually-exclusive ``cursor`` + ``from``.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from rest_framework import serializers

__all__ = ["EventsPageSerializer", "EventsQuerySerializer"]

# Page-limit bounds (§5.1): 1..1000, default 100. ``types`` ≤ 20 entries (§5.1).
_LIMIT_MIN = 1
_LIMIT_MAX = 1000
_LIMIT_DEFAULT = 100
_MAX_TYPES = 20


class EventsQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """Validate the ``GET /streams/{id}/events`` query string (§5.1).

    ``cursor`` and ``from`` are mutually exclusive; ``from`` defaults to ``earliest``
    only when no ``cursor`` is given. ``types`` is a comma list capped at 20 entries.
    The serializer does not decode the cursor (the service owns that, including the
    fingerprint check) — it only shapes inputs.
    """

    cursor = serializers.CharField(required=False, allow_blank=False, max_length=128)
    # Free-form: "earliest" | "latest" | RFC-3339. The service resolves/validates it.
    from_ = serializers.CharField(required=False, allow_blank=False, source="from")
    limit = serializers.IntegerField(
        required=False, min_value=_LIMIT_MIN, max_value=_LIMIT_MAX, default=_LIMIT_DEFAULT
    )
    types = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs.get("cursor") and attrs.get("from"):
            raise serializers.ValidationError(
                {"cursor": "Provide either 'cursor' or 'from', not both."}
            )
        return attrs

    def validate_types(self, value: str) -> tuple[str, ...]:
        parsed = tuple(t.strip() for t in value.split(",") if t.strip())
        if len(parsed) > _MAX_TYPES:
            raise serializers.ValidationError(
                f"At most {_MAX_TYPES} event types may be filtered."
            )
        return parsed

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        # drf-spectacular maps "from_" → query param "from" via the source; map back
        # at the request boundary so ?from=earliest binds correctly.
        if hasattr(data, "copy"):
            data = data.copy()
        if "from" in data and "from_" not in data:
            data["from_"] = data["from"]
        return cast("dict[str, Any]", super().to_internal_value(data))


class _EnvelopeField(serializers.DictField):
    """The delivered 20-field envelope — opaque dict for schema docs (event-model §5)."""


class EventsPageSerializer(serializers.Serializer[dict[str, Any]]):
    """The ``{data, next_cursor}`` page envelope (api-spec §2.6; §5.1).

    Documentation/response shape only — the viewset builds the dict directly from the
    service's :class:`~delivery.application.services.EventsPage` (the envelopes are
    already delivered-shape, so no per-field serialization re-touches them).
    ``next_cursor`` is never null on this endpoint (RC-2).
    """

    data = serializers.ListField(  # type: ignore[assignment]  # docs-only page field (api-spec §2.6)
        child=_EnvelopeField()
    )
    next_cursor = serializers.CharField()

    class Meta:
        ref_name: ClassVar[str] = "EventsPage"
