"""REST-interchangeable cursors for the WebSocket tail (delivery-channels §6.4 / §5.2).

Every WS ``event`` frame carries a ``cursor`` that is the REST-compatible position
*after* that event (WS-7): the client's resume bookmark and the position it hands to
``GET /events?cursor=<from_cursor>`` for the at-least-once gap-fill. The cursor uses
the **same** ``c1.`` codec (:mod:`delivery.domain.cursor`) and the **same** filter
fingerprint binding (``f`` = first 8 hex of SHA-256(stream_id || "|" || filter_set))
as the REST channel, so the handoff needs no translation (§6.4).

The ws-pusher reads the post-chaos Kafka topic, not the replay-stable buffer, so it
does not know a row's ``(partition_ts, buffer_seq)``. It mints the REST position from
the envelope's own clock + counter:

* ``p`` = ``emitted_at`` epoch ms — the wall-clock processing instant. The buffer
  writer assigns ``partition_ts = now()`` at ingest, within a few seconds of
  ``emitted_at`` (delivery-channels §4.4 p95 ≤ 2 s), so the WS position lands close
  to its REST row position; resume is explicitly *approximate* (the ``behind`` gap is
  approximate, WS-6) and REST gap-fill is at-least-once, so a small lead/lag costs at
  most a handful of re-read events the client already deduplicates on ``event_id``.
* ``s`` = ``sequence_no`` — the per-shard monotonic canonical counter (gapless across
  pause/resume), the strictly-increasing tiebreaker within an ``emitted_at`` ms.

The fingerprint is computed via the canonical filter-set string the REST service
already owns (sorted, comma-joined ``types``), so a WS cursor and a REST cursor over
the same stream + filter set share one fingerprint and are mutually decodable (RC-7).

Pure: stdlib + the engine timestamp helper + the domain cursor codec only — no
Django, importable from both the sink (``delivery.infra``) and the consumer
(``delivery.api``) via this **domain** leaf module (import-linter App-layering: infra
may import domain, so the sink reaches it without crossing the layering boundary).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dataforge_engine.envelope.timestamps import to_epoch_ms
from delivery.domain.cursor import encode_cursor, filter_fingerprint

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "canonical_filter_set",
    "cursor_after_event",
    "fingerprint_for",
]


def canonical_filter_set(
    types: Sequence[str],
    *,
    entity_type: str | None = None,
    entity_key: str | None = None,
) -> str:
    """The canonical filter-set string the fingerprint binds to (RC-4/RC-7).

    Identical to the REST service's canonicalization (``delivery.application.services
    .canonical_filter_set``): ``types`` → a sorted, comma-joined, de-duplicated list;
    empty → ``""``; plus the Phase-8 per-entity CDC filter (R-CDC-7) appended as
    ``|e:{type}:{key}`` when both are set. Restated here (not imported) to keep this
    leaf module free of the REST service's Django-touching imports while guaranteeing
    the SAME fingerprint — the cross-channel cursor-interchange test asserts agreement.
    """
    cleaned = sorted({t for t in types if t})
    base = ",".join(cleaned)
    if entity_type and entity_key:
        return f"{base}|e:{entity_type}:{entity_key}"
    return base


def fingerprint_for(
    *,
    stream_id: str,
    types: Sequence[str],
    entity_type: str | None = None,
    entity_key: str | None = None,
) -> str:
    """The ``f`` fingerprint for a WS connection's (stream, filter set) (RC-7).

    Computed once per connection (the filter set is fixed for the socket's life,
    WS-5) and reused to mint every ``event``/``resume_ack``/``drop_notice`` cursor so
    they all decode against the same REST page query. The filter set includes the
    per-entity CDC filter (R-CDC-7) so a WS cursor and a REST cursor over the same
    ``(stream, types, entity)`` slice share one fingerprint.
    """
    return filter_fingerprint(
        stream_id=stream_id,
        canonical_filter_set=canonical_filter_set(
            types, entity_type=entity_type, entity_key=entity_key
        ),
    )


def _emitted_at_ms(envelope: Mapping[str, Any]) -> int:
    """``emitted_at`` of a delivered envelope → epoch ms (the cursor ``p`` unit).

    ``emitted_at`` is the RFC-3339 string of event-model §2.1; parse it back to a
    tz-aware instant and truncate to ms with the shared engine helper so the WS ``p``
    matches the REST ``partition_ts`` ms unit exactly.
    """
    raw = str(envelope.get("emitted_at", ""))
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return to_epoch_ms(parsed)


def cursor_after_event(
    *, envelope: Mapping[str, Any], fingerprint: str
) -> str:
    """The REST-interchangeable ``c1.`` cursor for the position *after* ``envelope``.

    ``p`` = the event's ``emitted_at`` ms, ``s`` = its ``sequence_no`` — together the
    composite position the REST page query advances over (exclusive). The same
    ``fingerprint`` binds the cursor to the connection's stream + filter set, so the
    client can present it to ``GET /events?cursor=`` unchanged (WS-7).
    """
    p = _emitted_at_ms(envelope)
    s = int(envelope.get("sequence_no", 0))
    return encode_cursor(p=p, s=s, fingerprint=fingerprint)
