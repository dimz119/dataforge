"""The WebSocket tail protocol contract (delivery-channels §6; api-spec §5).

The frozen, framework-light definition of the ``dataforge.events.v1`` subprotocol:
the close-code table (§6.5 / api-spec §5.5), the frame-type tokens (§6.3 catalog),
and the timing/quota/limit constants (WS-2/WS-4/WS-5/WS-10/WS-12). It is stdlib-only
domain code so both the per-connection consumer (``delivery.api.consumers``) and the
ws-pusher sink (``delivery.infra.ws_pusher_channel``) share ONE source of truth for
the wire contract, and the CI frame-schema artifact
(``backend/schema/ws-protocol-v1.schema.json``) is generated from it.

The frame *builders* (the S→C JSON shapes of §6.3) live here too so the consumer and
the sink mint byte-identical frames; the consumer adds the transport (close codes,
queueing), and the sink adds the ``frame_seq`` fan-out envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "AUTH_DEADLINE_S",
    "CLOSE_AUTH_FAILED",
    "CLOSE_AUTH_TIMEOUT",
    "CLOSE_FORBIDDEN",
    "CLOSE_GOING_AWAY",
    "CLOSE_INTERNAL_ERROR",
    "CLOSE_NORMAL",
    "CLOSE_NOT_FOUND",
    "CLOSE_OVERLOAD",
    "CLOSE_PROTOCOL_VIOLATION",
    "CLOSE_QUOTA_EXCEEDED",
    "HEARTBEAT_INTERVAL_S",
    "MAX_CLIENT_FRAME_BYTES",
    "MAX_CONNECTIONS_PER_KEY",
    "MAX_CONNECTIONS_PER_WORKSPACE",
    "MAX_TYPES_FILTER",
    "SCOPE_EVENTS_READ",
    "SEND_QUEUE_CAP",
    "SILENCE_TIMEOUT_S",
    "SUBPROTOCOL_V1",
    "build_drop_notice_frame",
    "build_error_frame",
    "build_event_frame",
    "build_heartbeat_frame",
    "build_ready_frame",
    "build_resume_ack_frame",
    "generate_ws_protocol_schema",
]

# -- subprotocol (WS-1) -------------------------------------------------------
# The versioned subprotocol the client MUST offer in Sec-WebSocket-Protocol; the
# server selects + echoes it. A handshake offering no supported subprotocol is
# rejected at HTTP level with 400. Future revisions are new tokens
# (``dataforge.events.v2``) offered side by side; v1 frames never change.
SUBPROTOCOL_V1: Final = "dataforge.events.v1"

# The data-plane read scope the auth frame's api_key must carry (WS-2 / A-5).
SCOPE_EVENTS_READ: Final = "events:read"

# -- timing / limits (WS-2/WS-4/WS-5/WS-10/WS-12) -----------------------------
AUTH_DEADLINE_S: Final = 10.0  # WS-2: auth frame within 10 s else close 4408
HEARTBEAT_INTERVAL_S: Final = 15.0  # WS-12: heartbeat every 15 s
SILENCE_TIMEOUT_S: Final = 90.0  # WS-12: socket-silent 90 s → close 1001
MAX_CLIENT_FRAME_BYTES: Final = 16 * 1024  # §6.3: client frames ≤ 16 KiB
MAX_TYPES_FILTER: Final = 20  # WS-5: types filter ≤ 20 entries
SEND_QUEUE_CAP: Final = 1000  # WS-10: per-connection send queue cap
MAX_CONNECTIONS_PER_KEY: Final = 5  # WS-4: 5 concurrent connections per API key
MAX_CONNECTIONS_PER_WORKSPACE: Final = 250  # WS-4: 250 per workspace

# -- close codes (§6.5 / api-spec §5.5) ---------------------------------------
CLOSE_NORMAL: Final = 1000  # normal close (either side)
CLOSE_GOING_AWAY: Final = 1001  # deploy/restart, or client silent 90 s
CLOSE_INTERNAL_ERROR: Final = 1011  # internal server error
CLOSE_OVERLOAD: Final = 1013  # overload — reconnect with backoff
CLOSE_PROTOCOL_VIOLATION: Final = 4400  # malformed/binary/unknown type/pre-auth
CLOSE_AUTH_FAILED: Final = 4401  # invalid/revoked key, bad/expired JWT
CLOSE_FORBIDDEN: Final = 4403  # authenticated but missing events:read scope
CLOSE_NOT_FOUND: Final = 4404  # stream not found (incl. cross-tenant masking)
CLOSE_AUTH_TIMEOUT: Final = 4408  # auth deadline (10 s) expired
CLOSE_QUOTA_EXCEEDED: Final = 4429  # connection quota exceeded (WS-4)


# -- S→C frame builders (§6.3 catalog) ----------------------------------------
def build_ready_frame(
    *, stream_id: str, cursor: str, types: Sequence[str], sample_rate: float
) -> dict[str, Any]:
    """The ``ready`` frame (§6.3): auth accepted; tailing begins after this frame."""
    return {
        "type": "ready",
        "protocol": SUBPROTOCOL_V1,
        "stream_id": stream_id,
        "position": {"cursor": cursor},
        "filters": {"types": list(types), "sample_rate": sample_rate},
    }


def build_resume_ack_frame(
    *, cursor: str, behind: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The ``resume_ack`` frame (§6.4): the socket never replays; ``behind`` is the
    approximate gap (``events``, ``from_cursor``) to fetch over REST, or ``None``
    when the cursor is at/ahead of the live tail (WS-6)."""
    return {
        "type": "resume_ack",
        "position": {"cursor": cursor},
        "behind": dict(behind) if behind is not None else None,
    }


def build_event_frame(*, cursor: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """The ``event`` frame (§6.3): one delivered 20-field envelope; ``cursor`` is the
    REST-compatible position *after* this event (the client's resume bookmark + the
    REST gap-fill handoff point, WS-7)."""
    return {"type": "event", "cursor": cursor, "event": dict(event)}


def build_heartbeat_frame(
    *, server_time: str, last_cursor: str | None, delivered: int, dropped: int
) -> dict[str, Any]:
    """The ``heartbeat`` frame (§6.3): every 15 s, per-connection counters (WS-12)."""
    return {
        "type": "heartbeat",
        "server_time": server_time,
        "last_cursor": last_cursor,
        "delivered": delivered,
        "dropped": dropped,
    }


def build_drop_notice_frame(*, dropped: int, resume_cursor: str | None) -> dict[str, Any]:
    """The ``drop_notice`` frame (§6.3): ``dropped`` frames discarded under
    backpressure; ``resume_cursor`` is the position before the gap, for REST gap-fill
    (INV-DEL-5: drops are always signaled, with count, WS-10/WS-11)."""
    return {"type": "drop_notice", "dropped": dropped, "resume_cursor": resume_cursor}


def build_error_frame(*, problem: Mapping[str, Any]) -> dict[str, Any]:
    """The ``error`` frame (§6.3): an RFC 9457 problem object — sent once before an
    error close, or standalone for a non-fatal error (e.g. expired cursor, WS-8)."""
    return {"type": "error", "problem": dict(problem)}


# -- frame JSON Schemas (CI artifact backend/schema/ws-protocol-v1.schema.json) --
_UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_CURSOR = {"type": "string", "maxLength": 128}
_SCHEMA_ID = "https://dataforge.dev/schema/ws-protocol-v1.schema.json"


def _client_frames() -> dict[str, Any]:
    """The C→S frame schemas (§6.3): ``auth`` (first message) + ``resume``."""
    return {
        "auth": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"const": "auth"},
                "api_key": {"type": "string"},
                "access_token": {"type": "string"},
                "cursor": _CURSOR,
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_TYPES_FILTER,
                },
                "sample_rate": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            },
            "oneOf": [{"required": ["api_key"]}, {"required": ["access_token"]}],
            "additionalProperties": False,
        },
        "resume": {
            "type": "object",
            "required": ["type", "cursor"],
            "properties": {"type": {"const": "resume"}, "cursor": _CURSOR},
            "additionalProperties": False,
        },
    }


def _server_frames() -> dict[str, Any]:
    """The S→C frame schemas (§6.3): ready/resume_ack/event/heartbeat/drop_notice/error."""
    behind = {
        "type": ["object", "null"],
        "properties": {
            "events": {"type": "integer", "minimum": 0},
            "from_cursor": _CURSOR,
        },
        "required": ["events", "from_cursor"],
        "additionalProperties": False,
    }
    return {
        "ready": {
            "type": "object",
            "required": ["type", "protocol", "stream_id", "position", "filters"],
            "properties": {
                "type": {"const": "ready"},
                "protocol": {"const": SUBPROTOCOL_V1},
                "stream_id": {"type": "string", "pattern": _UUID_PATTERN},
                "position": {
                    "type": "object",
                    "required": ["cursor"],
                    "properties": {"cursor": _CURSOR},
                    "additionalProperties": False,
                },
                "filters": {
                    "type": "object",
                    "required": ["types", "sample_rate"],
                    "properties": {
                        "types": {"type": "array", "items": {"type": "string"}},
                        "sample_rate": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "resume_ack": {
            "type": "object",
            "required": ["type", "position", "behind"],
            "properties": {
                "type": {"const": "resume_ack"},
                "position": {
                    "type": "object",
                    "required": ["cursor"],
                    "properties": {"cursor": _CURSOR},
                    "additionalProperties": False,
                },
                "behind": behind,
            },
            "additionalProperties": False,
        },
        "event": {
            "type": "object",
            "required": ["type", "cursor", "event"],
            "properties": {
                "type": {"const": "event"},
                "cursor": _CURSOR,
                "event": {
                    "type": "object",
                    "description": "The delivered 20-field envelope (envelope-1.0.schema.json).",
                },
            },
            "additionalProperties": False,
        },
        "heartbeat": {
            "type": "object",
            "required": ["type", "server_time", "last_cursor", "delivered", "dropped"],
            "properties": {
                "type": {"const": "heartbeat"},
                "server_time": {"type": "string", "format": "date-time"},
                "last_cursor": {"type": ["string", "null"], "maxLength": 128},
                "delivered": {"type": "integer", "minimum": 0},
                "dropped": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "drop_notice": {
            "type": "object",
            "required": ["type", "dropped", "resume_cursor"],
            "properties": {
                "type": {"const": "drop_notice"},
                "dropped": {"type": "integer", "minimum": 0},
                "resume_cursor": {"type": ["string", "null"], "maxLength": 128},
            },
            "additionalProperties": False,
        },
        "error": {
            "type": "object",
            "required": ["type", "problem"],
            "properties": {
                "type": {"const": "error"},
                "problem": {
                    "type": "object",
                    "description": "An RFC 9457 problem object (same catalog as REST).",
                },
            },
            "additionalProperties": False,
        },
    }


def generate_ws_protocol_schema() -> dict[str, Any]:
    """The frozen ``dataforge.events.v1`` frame JSON Schema (api-spec §5.5, T-3).

    A draft-2020-12 schema with one named ``$def`` per frame type (§6.3 catalog) plus
    the close-code table + subprotocol token as enum metadata; committed as
    ``backend/schema/ws-protocol-v1.schema.json`` and exercised by the cross-channel
    contract suite. Deterministic (sorted defs) so the artifact-diff CI gate is stable.
    """
    defs = {**_client_frames(), **_server_frames()}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": _SCHEMA_ID,
        "title": "DataForge WebSocket tail protocol dataforge.events.v1",
        "description": (
            "Frozen WS frame contract (delivery-channels §6.3; api-spec §5). "
            "Each frame type is a named $def; the close-code table and subprotocol "
            "token are pinned in x-dataforge metadata."
        ),
        "x-dataforge-subprotocol": SUBPROTOCOL_V1,
        "x-dataforge-close-codes": {
            "1000": "normal",
            "1001": "going_away",
            "1011": "internal_error",
            "1013": "overload",
            "4400": "protocol_violation",
            "4401": "auth_failed",
            "4403": "forbidden",
            "4404": "not_found",
            "4408": "auth_timeout",
            "4429": "quota_exceeded",
        },
        "oneOf": [{"$ref": f"#/$defs/{name}"} for name in sorted(defs)],
        "$defs": {name: defs[name] for name in sorted(defs)},
    }
