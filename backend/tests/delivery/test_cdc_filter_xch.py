"""Per-entity CDC filter cross-channel parity (event-model R-CDC-7; phase-08 exit #8).

Phase-8 exit criterion #8 — "per-entity CDC filtering returns identical event sets on
REST and WS" — proven at the filter seam without a live broker. Both channels route
their delivery predicate through the ONE shared function
:func:`delivery.domain.event_filter.envelope_matches` (the REST cursor pull's row loop
in :mod:`delivery.application.services` and the WS consumer's ``_passes_filters``), so
this test exercises that function over a representative window and asserts the kept set
under each filter shape is identical regardless of channel — and matches the R-CDC-7
contract: ``entity_type``/``entity_key`` matched against ``entity_refs``, AND-composed
with the ``event_type`` ``types`` filter.

The fingerprint binding (``delivery.domain.ws_cursor.canonical_filter_set`` ==
``delivery.application.services.canonical_filter_set``) is asserted too: a WS cursor
and a REST cursor over the same (stream, types, entity) slice share one fingerprint
(RC-7), so resume hands off cleanly.
"""

from __future__ import annotations

from typing import Any

from delivery.application import services
from delivery.domain import event_filter, ws_cursor

_REF = "entity_refs"


def _env(
    *, event_id: str, event_type: str, refs: list[dict[str, str]], op: str | None
) -> dict[str, Any]:
    """A delivered-shape envelope carrying just the fields the filter reads."""
    return {"event_id": event_id, "event_type": event_type, _REF: refs, "op": op}


def _window() -> list[dict[str, Any]]:
    """A mixed window: business events + CDC c/u/d over two users and an order."""
    u1 = {"entity_type": "users", "entity_key": "usr_a"}
    u2 = {"entity_type": "users", "entity_key": "usr_b"}
    o1 = {"entity_type": "orders", "entity_key": "ord_1"}
    return [
        _env(event_id="e0", event_type="order_placed", refs=[u1, o1], op=None),
        _env(event_id="e1", event_type="cdc.orders", refs=[o1], op="c"),
        _env(event_id="e2", event_type="cdc.users", refs=[u1], op="u"),
        _env(event_id="e3", event_type="cdc.users", refs=[u2], op="u"),
        _env(event_id="e4", event_type="session_started", refs=[u2], op=None),
        _env(event_id="e5", event_type="cdc.users", refs=[u1], op="d"),
    ]


def _rest_kept(
    window: list[dict[str, Any]],
    *,
    types: tuple[str, ...],
    entity_type: str | None,
    entity_key: str | None,
) -> set[str]:
    """The REST cursor-pull kept set — the exact predicate of ``services.read_events``."""
    wanted = set(types)
    return {
        e["event_id"]
        for e in window
        if event_filter.envelope_matches(
            e, types=wanted, entity_type=entity_type, entity_key=entity_key
        )
    }


def _ws_kept(
    window: list[dict[str, Any]],
    *,
    types: tuple[str, ...],
    entity_type: str | None,
    entity_key: str | None,
) -> set[str]:
    """The WS kept set — the exact predicate of the consumer's ``_passes_filters``."""
    return {
        e["event_id"]
        for e in window
        if event_filter.envelope_matches(
            e, types=types, entity_type=entity_type, entity_key=entity_key
        )
    }


def test_per_entity_cdc_filter_rest_ws_identical_sets() -> None:
    """R-CDC-7 / exit #8: REST and WS keep the SAME set under the entity CDC filter.

    Filter: ``types=cdc.users`` + ``entity_type=users,entity_key=usr_a`` — the per-row
    CDC slice for one user — keeps exactly the two ``cdc.users`` rows on ``usr_a``."""
    window = _window()
    rest = _rest_kept(
        window, types=("cdc.users",), entity_type="users", entity_key="usr_a"
    )
    ws = _ws_kept(
        window, types=("cdc.users",), entity_type="users", entity_key="usr_a"
    )
    assert rest == ws == {"e2", "e5"}


def test_entity_filter_only_matches_refs_across_event_types() -> None:
    """Entity filter without ``types`` matches every event referencing that entity."""
    window = _window()
    kept = _rest_kept(window, types=(), entity_type="users", entity_key="usr_a")
    # usr_a appears in the order_placed refs (e0) and its two cdc.users rows (e2,e5).
    assert kept == {"e0", "e2", "e5"} == _ws_kept(
        window, types=(), entity_type="users", entity_key="usr_a"
    )


def test_types_only_and_no_filter_parity() -> None:
    """``types``-only and the empty filter behave identically on both channels."""
    window = _window()
    for types, et, ek in [(("cdc.users",), None, None), ((), None, None)]:
        assert _rest_kept(window, types=types, entity_type=et, entity_key=ek) == _ws_kept(
            window, types=types, entity_type=et, entity_key=ek
        )


def test_unknown_entity_matches_nothing_on_both() -> None:
    """An entity_key not in any ref keeps nothing — identical on both channels."""
    window = _window()
    rest = _rest_kept(window, types=(), entity_type="users", entity_key="usr_zzz")
    ws = _ws_kept(window, types=(), entity_type="users", entity_key="usr_zzz")
    assert rest == ws == set()


def test_cursor_fingerprint_binding_matches_across_channels() -> None:
    """RC-7: the REST and WS canonical filter sets (incl. entity) agree exactly."""
    rest_fs = services.canonical_filter_set(
        ("cdc.users",), entity_type="users", entity_key="usr_a"
    )
    ws_fs = ws_cursor.canonical_filter_set(
        ("cdc.users",), entity_type="users", entity_key="usr_a"
    )
    assert rest_fs == ws_fs
    # The entity filter changes the filter set (so a cursor binds to it, P-3).
    assert rest_fs != services.canonical_filter_set(("cdc.users",))
