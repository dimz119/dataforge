"""Shared helpers for the content-mode stages (chaos-engine §2.1, §5).

The ``_df`` labelling, payload-leaf resolution, and the injection-record
assembly that every content mode shares. Payload-only modes (``corrupted_values``,
``nulls``) target scalar leaves of the business ``payload`` (or CDC ``before``/
``after`` images) and NEVER envelope fields (CR-6) — this helper enforces that by
only ever walking the ``payload`` subtree.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import cast

from dataforge_engine.envelope import DfBlock, DfChaos, InternalEnvelope
from dataforge_engine.envelope.types import JSONValue


def clone_envelope(envelope: InternalEnvelope) -> InternalEnvelope:
    """A deep copy safe to mutate without touching the canonical/ledger instance.

    Chaos never mutates the ledger (CHD-4/5); a stage that mutates payload leaves
    operates on a clone so the upstream batch (and the ledger row it came from)
    stays byte-identical.
    """
    return cast(InternalEnvelope, copy.deepcopy(dict(envelope)))


def label_touched(
    envelope: InternalEnvelope,
    injection_id: str,
    mode: str,
    chaos_detail: dict[str, JSONValue],
) -> None:
    """Stamp the ``_df`` labels for a stage that touched ``envelope`` (§2.1).

    Sets ``_df.canonical = false``, appends ``injection_id`` to
    ``_df.injection_ids``, and writes the mode-specific ``_df.chaos`` block. The
    ``_df`` block rides to the strip boundary and is NEVER delivered (INV-DEL-2).
    """
    df: DfBlock = envelope["_df"]
    df["canonical"] = False
    df["injection_ids"] = [*df["injection_ids"], injection_id]
    chaos: DfChaos = df["chaos"] if df["chaos"] is not None else {}
    chaos[mode] = chaos_detail  # type: ignore[literal-required]
    df["chaos"] = chaos


def _is_scalar_leaf(value: object) -> bool:
    """A targetable scalar payload leaf (string/number/int/bool/None) — §5.3
    eligibility. Containers (lists/dicts) are never replaced wholesale.
    """
    return not isinstance(value, (dict, list))


def iter_payload_leaves(
    payload: object, prefix: str = ""
) -> Iterator[tuple[str, JSONValue]]:
    """Yield ``(dotted_path, value)`` for every scalar leaf under ``payload``.

    Dotted nesting with ``[]`` for array items (``items[].unit_price``), matching
    the §3.3 field-selector grammar. Only ``payload`` is ever walked — envelope
    fields are structurally out of reach (CR-6).
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            if _is_scalar_leaf(value):
                yield path, cast(JSONValue, value)
            else:
                yield from iter_payload_leaves(value, path)
    elif isinstance(payload, list):
        for item in payload:
            path = f"{prefix}[]"
            if _is_scalar_leaf(item):
                yield path, cast(JSONValue, item)
            else:
                yield from iter_payload_leaves(item, path)


def set_payload_leaf(payload: object, path: str, new_value: JSONValue) -> None:
    """Set the scalar leaf at ``path`` (dotted, ``[]`` for arrays) to ``new_value``.

    Mutates ``payload`` in place; the caller passes a clone. Array segments set
    EVERY matching item (the path collapses array indices to ``[]``), which is the
    deterministic, index-free addressing the selector grammar uses.
    """
    segments = _split_path(path)
    _set_recursive(payload, segments, new_value)


def _split_path(path: str) -> list[str]:
    return [seg for seg in path.replace("[]", ".[]").split(".") if seg]


def _set_recursive(node: object, segments: list[str], new_value: JSONValue) -> None:
    head, rest = segments[0], segments[1:]
    if head == "[]":
        if isinstance(node, list):
            for item in node:
                if not rest:
                    # cannot replace a list element in place by identity; handled by parent
                    continue
                _set_recursive(item, rest, new_value)
        return
    if isinstance(node, dict):
        if not rest:
            node[head] = new_value
            return
        child = node.get(head)
        if isinstance(child, list) and rest == ["[]"]:
            node[head] = [new_value for _ in child]
            return
        if child is not None:
            _set_recursive(child, rest, new_value)
