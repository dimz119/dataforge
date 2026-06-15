"""drf-spectacular schema helpers — the ``{data, next_cursor}`` page envelope.

api-specification §2.6 freezes the response envelope for EVERY paginated collection
endpoint: ``{"data": [ ...resources ], "next_cursor": <opaque|null>}``. The viewsets
build that dict directly (``Response({"data": ..., "next_cursor": ...})``), so the
``@extend_schema`` annotation must declare the *envelope*, not the bare item array,
or the generated OpenAPI (and the TS client built from it, ADR-0014/0016) drifts
from the runtime — the console then reads ``response`` as an array when the wire
shape is the envelope.

This helper wraps an item serializer in an inline ``{data, next_cursor}`` envelope
so the annotation matches the runtime exactly. It is a documentation/response-shape
construct only; it changes no endpoint behavior (the additive-annotation rule).
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import inline_serializer
from rest_framework import serializers


def page_envelope(name: str, item: type[serializers.BaseSerializer[Any]]) -> Any:
    """An inline ``{data: item[], next_cursor: str|null}`` page-envelope serializer.

    ``name`` is the generated component name (kept unique per endpoint so the OpenAPI
    components do not collide). ``next_cursor`` is nullable per api-spec P-2 (``null``
    on the last page; the events endpoints override this since their cursor is never
    null, RC-2 — they keep their own ``EventsPageSerializer``).
    """
    return inline_serializer(
        name=name,
        fields={
            "data": item(many=True),
            "next_cursor": serializers.CharField(allow_null=True),
        },
    )
