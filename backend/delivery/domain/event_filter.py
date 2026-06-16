"""Channel-agnostic event filter predicates (event-model R-CDC-7; api-spec §4.9.1).

The ONE source of truth for the delivery filters so the REST cursor pull
(:mod:`delivery.application.services`) and the WS live tail
(:mod:`delivery.api.consumers`) apply *byte-identical* semantics — the cross-channel
contract suite (XCH) asserts a REST page and a WS tail over the same stream + filter
set return the same event set.

Two orthogonal, AND-composed predicates:

* **``types``** (api-spec §4.9.1 / §5.2): exact match on the envelope ``event_type``
  (business names *and* ``cdc.{entity}``). Empty ⇒ matches everything.
* **per-entity** (R-CDC-7, Phase 8): ``entity_type`` + ``entity_key`` (both or
  neither) matched against the envelope's ``entity_refs`` — every pooled entity the
  payload references (event-model field 16). A CDC event lists exactly one ref (the
  mutated entity, PK-2), so ``?entity_type=users&entity_key=usr_…`` over
  ``?types=cdc.users`` is the per-entity CDC slice; on a business event the same
  filter matches whenever that entity appears in its refs. None/empty ⇒ no constraint.

Pure stdlib domain code (no Django, no DRF) — a leaf importable from both the
application service and the API consumer without crossing the import-linter layering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

__all__ = ["entity_ref_matches", "envelope_matches", "type_matches"]


def type_matches(envelope: Mapping[str, Any], types: Collection[str]) -> bool:
    """Exact ``event_type`` membership (api-spec §4.9.1 ``types``); empty ⇒ all.

    Unknown types match nothing (the membership simply fails) — the §4.9.1 contract.
    """
    if not types:
        return True
    return str(envelope.get("event_type")) in types


def entity_ref_matches(
    envelope: Mapping[str, Any], entity_type: str | None, entity_key: str | None
) -> bool:
    """R-CDC-7 per-entity match against ``entity_refs``; no filter ⇒ matches.

    The envelope matches when its ``entity_refs`` array (field 16, never empty for a
    real event) contains a ref whose ``entity_type`` **and** ``entity_key`` both equal
    the requested pair. Both must be supplied (the §4.9.1 "both or neither" rule is
    enforced at the request boundary); when either is absent there is no constraint.
    """
    if not entity_type or not entity_key:
        return True
    refs = envelope.get("entity_refs")
    if not isinstance(refs, (list, tuple)):
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        if (
            str(ref.get("entity_type")) == entity_type
            and str(ref.get("entity_key")) == entity_key
        ):
            return True
    return False


def envelope_matches(
    envelope: Mapping[str, Any],
    *,
    types: Collection[str] = (),
    entity_type: str | None = None,
    entity_key: str | None = None,
) -> bool:
    """The composed delivery predicate (``types`` ∧ per-entity), R-CDC-7.

    Identical on every channel: a delivered envelope is kept iff it passes BOTH the
    ``types`` membership and the per-entity ``entity_refs`` match (each a no-op when
    its inputs are empty).
    """
    return type_matches(envelope, types) and entity_ref_matches(
        envelope, entity_type, entity_key
    )
