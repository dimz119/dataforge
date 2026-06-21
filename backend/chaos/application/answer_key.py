"""Answer-key read services (chaos-engine §7.3; api-spec §4.13).

The query side of the ``chaos_injections`` answer-key store. All reads run through
the standard tenancy stack — the caller arms the workspace GUC (AK-4 / RLS) before
invoking these, so the scoped :class:`~chaos.domain.models.ChaosInjection` manager
filters to the armed workspace. Three reads:

* :func:`list_injections` — cursor-paginated injection records (§7.3, ``details``
  flattened to the top level), with ``mode`` / ``event_id`` / ``from`` / ``to``
  filters and an opaque keyset cursor over ``(recorded_at, injection_id)``.
* :func:`summarize` — the per-mode count aggregate (all seven keys, zeros included),
  with the ``duplicates`` / ``late_arriving`` extras (§7.3 summary row).
* :func:`iter_injections_jsonl` — the streaming JSONL export of the same record
  shape, in keyset order, for bulk download.

Cursors are opaque, URL-safe, endpoint+filter bound (P-1/P-3): the encoded payload
carries the filter fingerprint so reuse under different filters fails the decode.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from chaos.domain.models import ChaosInjection
from dataforge_engine.chaos import CHAOS_MODES

__all__ = [
    "InjectionFilters",
    "InjectionPage",
    "InvalidCursor",
    "iter_injections_jsonl",
    "list_injections",
    "summarize",
]

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class InvalidCursor(Exception):
    """An undecodable cursor, or one bound to a different filter set (P-3)."""


class InjectionFilters:
    """The shared answer-key query filter set (mode / event_id / time range)."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        event_id: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> None:
        self.mode = mode
        self.event_id = event_id
        self.from_ts = from_ts
        self.to_ts = to_ts

    def fingerprint(self) -> str:
        return f"{self.mode}|{self.event_id}|{self.from_ts}|{self.to_ts}"


class InjectionPage:
    """One page of flattened injection records + the opaque ``next_cursor``."""

    def __init__(self, *, data: list[dict[str, Any]], next_cursor: str | None) -> None:
        self.data = data
        self.next_cursor = next_cursor


def _flatten(row: ChaosInjection) -> dict[str, Any]:
    """The §7.3 wire record: common fields + ``details`` flattened to top level."""
    record: dict[str, Any] = {
        "injection_id": str(row.injection_id),
        "mode": row.mode,
        "stream_id": str(row.stream_id),
        "shard_id": row.shard_id,
        "event_id": str(row.event_id),
        "sequence_no": row.sequence_no,
        "occurred_at": row.occurred_at,
        "canonical_emitted_at": row.canonical_emitted_at,
        "recorded_at": row.recorded_at,
    }
    record.update(dict(row.details or {}))
    return record


def _encode_cursor(*, recorded_at: datetime, injection_id: str, fp: str) -> str:
    payload = {"r": recorded_at.isoformat(), "i": injection_id, "f": fp}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, fp: str) -> tuple[datetime, str]:
    try:
        pad = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + pad))
        if payload["f"] != fp:  # P-3: cursor bound to its filter set
            raise InvalidCursor("cursor does not match the requested filter set")
        return datetime.fromisoformat(payload["r"]), str(payload["i"])
    except InvalidCursor:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor("unparseable cursor") from exc


def _base_query(stream_id: str, filters: InjectionFilters) -> Any:
    """The scoped base queryset for one stream + filter set (RLS-armed by caller)."""
    qs = ChaosInjection.objects.filter(stream_id=stream_id)
    if filters.mode:
        qs = qs.filter(mode=filters.mode)
    if filters.event_id:
        qs = qs.filter(event_id=filters.event_id)
    if filters.from_ts is not None:
        qs = qs.filter(occurred_at__gte=filters.from_ts)
    if filters.to_ts is not None:
        qs = qs.filter(occurred_at__lte=filters.to_ts)
    return qs


def list_injections(
    *, stream_id: str, filters: InjectionFilters, cursor: str | None, limit: int
) -> InjectionPage:
    """A cursor-paginated page of injection records (newest-first, R-6)."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    fp = filters.fingerprint()
    qs = _base_query(stream_id, filters).order_by("-recorded_at", "-injection_id")
    if cursor:
        cut_recorded, cut_id = _decode_cursor(cursor, fp)
        qs = qs.filter(recorded_at__lt=cut_recorded) | qs.filter(
            recorded_at=cut_recorded, injection_id__lt=cut_id
        )
        qs = qs.order_by("-recorded_at", "-injection_id")
    rows = list(qs[: limit + 1])
    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(
            recorded_at=last.recorded_at, injection_id=str(last.injection_id), fp=fp
        )
        rows = rows[:limit]
    return InjectionPage(data=[_flatten(r) for r in rows], next_cursor=next_cursor)


def iter_injections_jsonl(
    *, stream_id: str, filters: InjectionFilters
) -> Iterator[bytes]:
    """Stream the filtered records as JSONL (one record per line) for export."""
    qs = _base_query(stream_id, filters).order_by("-recorded_at", "-injection_id")
    for row in qs.iterator():
        yield (json.dumps(_flatten(row), default=str) + "\n").encode()


def summarize(*, stream_id: str, filters: InjectionFilters) -> dict[str, Any]:
    """Per-mode injection counts (all seven keys, zeros included; §7.3 summary).

    Returns ``{by_mode, total_injections}``. ``by_mode`` carries every mode key with
    ``injections``; ``duplicates`` adds ``extra_copies`` and ``late_arriving`` adds
    the ``{pending, emitted, discarded}`` outcome breakdown — both derived from the
    flattened ``details`` (``copies`` / ``outcome``). The view wraps this with the
    ``stream_id`` / ``window`` / ``as_of`` envelope (§7.3 summary row).
    """
    from django.db.models import Count

    qs = _base_query(stream_id, filters)
    by_mode: dict[str, dict[str, Any]] = {
        mode: {"injections": 0} for mode in CHAOS_MODES
    }
    total = 0
    for grouped in qs.values("mode").annotate(n=Count("injection_id")):
        mode = grouped["mode"]
        count = int(grouped["n"])
        total += count
        if mode in by_mode:
            by_mode[mode]["injections"] = count
    # duplicates: extra_copies = sum(details.copies). late_arriving: outcome split.
    by_mode["duplicates"]["extra_copies"] = sum(
        int(row.details.get("copies", 0) or 0)
        for row in qs.filter(mode="duplicates").only("details")
    )
    late = {"pending": 0, "emitted": 0, "discarded": 0}
    for row in qs.filter(mode="late_arriving").only("details"):
        outcome = (row.details or {}).get("outcome", "pending")
        if outcome in ("emitted", "flushed"):
            late["emitted"] += 1
        elif outcome == "discarded":
            late["discarded"] += 1
        else:
            late["pending"] += 1
    by_mode["late_arriving"].update(late)
    return {"by_mode": by_mode, "total_injections": total}
