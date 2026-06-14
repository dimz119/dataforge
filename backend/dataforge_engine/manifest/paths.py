"""Parsers for the manifest's two path grammars (§5.1 R-EVT-3, §6.4 targets).

* **context path** (``contextPath``, §9.1): ``actor.x``, ``subject.y``,
  ``session.cart_items[].unit_price``, ``created.orders.order_id`` — used in
  payload ``from``, guard ``path``/``ref``, ``adjust.by``.
* **entity ref** (``entityRef``, §9.1): ``actor``, ``subject``,
  ``created.<entity>``, each optionally extended by ``.via.<relationship>``
  segments (≤ 2) — used in ``partition_by``, effect ``target``, exists ``of``.

Layer 1 already guarantees these strings match their patterns; these parsers
split them into a typed root + tail for the semantic resolver. Pure Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RootKind = Literal["actor", "subject", "session", "created"]


@dataclass(frozen=True)
class ContextPath:
    """A parsed ``contextPath``: a root, an optional created-entity, attribute tail."""

    kind: RootKind
    created_entity: str | None  # set iff kind == "created"
    # Attribute segments after the root (``cart_items``, ``unit_price``), each
    # flagged for the ``[]`` list marker.
    segments: tuple[tuple[str, bool], ...]

    @property
    def first_segment(self) -> str | None:
        return self.segments[0][0] if self.segments else None


@dataclass(frozen=True)
class EntityRef:
    """A parsed ``entityRef``: a root + a chain of ``.via.<relationship>`` hops."""

    kind: RootKind
    created_entity: str | None
    via: tuple[str, ...]


def parse_context_path(raw: str) -> ContextPath:
    """Parse a Layer-1-valid ``contextPath`` string into a :class:`ContextPath`."""
    head, _, tail = raw.partition(".")
    created_entity: str | None = None
    if head == "created":
        # created.<entity>.<attr>...
        entity, _, rest = tail.partition(".")
        created_entity = entity
        kind: RootKind = "created"
        tail = rest
    else:
        kind = head  # type: ignore[assignment]
    segments: list[tuple[str, bool]] = []
    if tail:
        for seg in tail.split("."):
            if seg.endswith("[]"):
                segments.append((seg[:-2], True))
            else:
                segments.append((seg, False))
    return ContextPath(kind=kind, created_entity=created_entity, segments=tuple(segments))


def parse_entity_ref(raw: str) -> EntityRef:
    """Parse a Layer-1-valid ``entityRef`` string into an :class:`EntityRef`."""
    parts = raw.split(".via.")
    head = parts[0]
    via = tuple(parts[1:])
    if head.startswith("created."):
        return EntityRef(
            kind="created",
            created_entity=head[len("created.") :],
            via=via,
        )
    return EntityRef(kind=head, created_entity=None, via=via)  # type: ignore[arg-type]
