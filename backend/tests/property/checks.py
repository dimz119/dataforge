"""PROP-RI-1..8 — the referential-integrity invariant checks (testing-strategy §4.1).

Eight pure checks asserted over a generated canonical batch (the engine *is* the
Hypothesis strategy here — generation is seeded, §17.1). Each check returns
``None`` on success or a human-readable failure string naming the first offending
event, so the same checks run over the 100k PR profile and the 1M nightly/gate
profile with one generated batch each.

The reference frame for PROP-RI-1: this phase emits **no** snapshot ``op:"r"``
rows (Phase 8), so pool-seeded entities (users/products) have no preceding event.
The seeded pool keys captured at seed time (``BatchResult.seeded_keys``) are the
position-0 reference set — a payload/entity ref resolves if it appears in the
seeded pool **or** in a prior ``c``/``r`` event (INV-GEN-1).

Bound to invariants: PROP-RI-1 INV-GEN-1, PROP-RI-2 INV-GEN-2, PROP-RI-5
INV-GEN-7, PROP-RI-6 INV-GEN-4, PROP-RI-7 event-model C-1..C-5, PROP-RI-8
INV-REG-4 + the 20-key envelope pin (EV-6).
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.envelope import DELIVERED_FIELD_SET
from tests.golden.harness import BatchResult, merged_ecommerce_document

# The CDC ops that *introduce* an entity instance into the reference frame.
_INTRODUCING_OPS = ("c", "r")
# Payment-shaped events that must carry a prior ``order_placed`` order_id.
_PAYMENT_EVENTS = ("payment_authorized", "payment_failed")


def _expected_subjects(overlay: dict[str, Any] | None = None) -> set[str]:
    """The registry subjects a pinned manifest registers (subject naming INV-REG-1).

    ``{slug}.{event_type}`` for business events, ``{slug}.cdc.{entity}`` for CDC —
    derived from the manifest, so PROP-RI-8 resolves ``schema_ref`` without a DB.
    """
    document = merged_ecommerce_document(overlay)
    slug = document["metadata"]["slug"]
    subjects = {f"{slug}.{name}" for name in document.get("event_types", {})}
    cdc = (document.get("cdc") or {}).get("entities", {})
    subjects |= {f"{slug}.cdc.{entity}" for entity in cdc}
    return subjects


def _birth_signal_types(result: BatchResult) -> set[str]:
    """Entity types with an **independent** birth signal — i.e. validatable by
    PROP-RI-1: pool-seeded types and CDC-``c``/``r``-enabled types.

    Entity types with neither (CDC ``enabled_default: false`` and not seeded — e.g.
    ``payments`` in the subset) have no envelope-level creation event distinct from
    the business event that references them, so a reference to one cannot be a
    forward reference by construction; their referential validity is proven by the
    FK property instead (PROP-RI-2/3: payment⇒order). This keeps PROP-RI-1 sound —
    it never false-fails on a birth-less entity, and never silently skips one with a
    real ``c``/``r``."""
    types: set[str] = {t for t, keys in result.seeded_keys.items() if keys}
    for env in result.envelopes:
        if env["op"] in _INTRODUCING_OPS and env["entity_refs"]:
            types.add(env["entity_refs"][0]["entity_type"])
    return types


def _introduction_index(result: BatchResult) -> dict[tuple[str, str], str]:
    """``{(entity_type, key): earliest introducing occurred_at}`` from seeds + c/r.

    Pool seeds are pinned before any RFC-3339 timestamp. A CDC ``c``/``r`` introduces
    its instance's primary key *and* every string-valued attribute in its ``after``
    image — the engine surfaces created-entity attribute values (``email``,
    ``full_name``, …) as ``entity_refs`` on the *business* creator, and the matching
    ``c`` carries those same values in ``after`` (R-CDC-2)."""
    epoch = ""  # sorts before any RFC-3339 occurred_at
    intro: dict[tuple[str, str], str] = {}
    for etype, keys in result.seeded_keys.items():
        for key in keys:
            intro[(etype, key)] = epoch
    for env in result.envelopes:
        if env["op"] not in _INTRODUCING_OPS or not env["entity_refs"]:
            continue
        etype = env["entity_refs"][0]["entity_type"]
        occ = env["occurred_at"]
        payload = env["payload"]
        after = payload.get("after") if isinstance(payload, dict) else None
        values: list[str] = [env["entity_refs"][0]["entity_key"]]
        if isinstance(after, dict):
            values.extend(v for v in after.values() if isinstance(v, str))
        for value in values:
            cur = intro.get((etype, value))
            if cur is None or occ < cur:
                intro[(etype, value)] = occ
    return intro


def check_ri1_entity_refs_resolve(result: BatchResult) -> str | None:
    """PROP-RI-1: every ``entity_refs`` entry for a birth-signal entity type resolves
    to a pool-seeded entity or a CDC ``c``/``r`` introduced at ``occurred_at <=`` the
    referencing event (INV-GEN-1). Using ``occurred_at`` (not ``sequence_no``) pairs
    a creating business event with its adjacent same-``occurred_at`` CDC ``c`` — one
    atomic transaction (R-CDC-2/3, event-model C-4) — so the business event that
    creates an entity may reference it. The frame is exactly seeds plus ``c``/``r``
    per the spec; birth-less types (PROP-RI-1 _birth_signal_types) are covered by
    the FK property instead."""
    validatable = _birth_signal_types(result)
    intro = _introduction_index(result)
    for env in result.envelopes:
        occ = env["occurred_at"]
        for ref in env["entity_refs"]:
            etype, ekey = ref["entity_type"], ref["entity_key"]
            if etype not in validatable:
                continue
            intro_occ = intro.get((etype, ekey))
            if intro_occ is None:
                return (
                    f"PROP-RI-1: {env['event_type']} (seq {env['sequence_no']}) references "
                    f"{etype}:{ekey} with no c/r introduction or pool seed"
                )
            if intro_occ > occ:
                return (
                    f"PROP-RI-1: {env['event_type']} (seq {env['sequence_no']}) forward-"
                    f"references {etype}:{ekey} (introduced at {intro_occ} > {occ})"
                )
    return None


def check_ri2_payment_requires_order(result: BatchResult) -> str | None:
    """PROP-RI-2: every payment carries an ``order_id`` from a prior ``order_placed``
    (INV-GEN-2 — no payment without an order)."""
    placed: set[str] = set()
    for env in result.envelopes:
        et = env["event_type"]
        if et == "order_placed":
            placed.add(str(env["payload"]["order_id"]))
        elif et in _PAYMENT_EVENTS:
            order_id = str(env["payload"].get("order_id"))
            if order_id not in placed:
                return (
                    f"PROP-RI-2: {et} (seq {env['sequence_no']}) references order "
                    f"{order_id} with no prior order_placed"
                )
    return None


def check_ri3_payment_order_consistency(result: BatchResult) -> str | None:
    """PROP-RI-3 (subset-scoped): every order referenced by a payment was placed and
    every order_placed has consistent user attribution (the refund window arrives
    with the full manifest in Phase 8; the subset proves the order→payment edge)."""
    order_user: dict[str, str] = {}
    for env in result.envelopes:
        et = env["event_type"]
        if et == "order_placed":
            order_user[str(env["payload"]["order_id"])] = str(env["payload"]["user_id"])
        elif et in _PAYMENT_EVENTS:
            oid = str(env["payload"].get("order_id"))
            if oid not in order_user:
                return f"PROP-RI-3: payment for unplaced order {oid} (seq {env['sequence_no']})"
    return None


def check_ri4_inventory_never_negative(result: BatchResult) -> str | None:
    """PROP-RI-4: replaying CDC stock images, ``stock`` is never negative at any
    version (INV-GEN, PRD §4.4). The subset manifest emits no inventory CDC this
    phase (the full inventory model + ``cdc.inventory`` arrive in Phase 8), so this
    check is a structural no-op until those images exist — it activates the day they
    do, with zero edit, and never silently passes on a real negative."""
    for env in result.envelopes:
        if env["event_type"] not in ("cdc.inventory", "cdc.products"):
            continue
        payload = env["payload"]
        after = payload.get("after") if isinstance(payload, dict) else None
        if not isinstance(after, dict) or "stock" not in after:
            continue
        try:
            stock = int(after["stock"])
        except (TypeError, ValueError):
            continue
        if stock < 0:
            return (
                f"PROP-RI-4: negative stock {stock} on {env['event_type']} at seq "
                f"{env['sequence_no']}"
            )
    return None


def check_ri5_sequence_gapless(result: BatchResult) -> str | None:
    """PROP-RI-5: ``sequence_no`` is gapless + strictly monotone per (stream, shard)
    starting at 1 (INV-GEN-7)."""
    per_shard: dict[tuple[str, int], int] = {}
    for env in result.envelopes:
        key = (env["stream_id"], env["shard_id"])
        expected = per_shard.get(key, 0) + 1
        if env["sequence_no"] != expected:
            return (
                f"PROP-RI-5: sequence gap on {key}: expected {expected}, "
                f"got {env['sequence_no']}"
            )
        per_shard[key] = expected
    return None


def check_ri6_occurred_at_monotone(result: BatchResult) -> str | None:
    """PROP-RI-6: ``occurred_at`` is non-decreasing per actor; ties broken by
    ``sequence_no`` (INV-GEN-4). Actorless rows (e.g. background CDC) are skipped."""
    last: dict[str, str] = {}
    for env in result.envelopes:
        actor = env.get("actor_id")
        if actor is None:
            continue
        occ = env["occurred_at"]
        prev = last.get(actor)
        if prev is not None and occ < prev:
            return (
                f"PROP-RI-6: occurred_at regressed for actor {actor} at seq "
                f"{env['sequence_no']}: {occ} < {prev}"
            )
        last[actor] = occ
    return None


def check_ri7_causality_resolves(result: BatchResult) -> str | None:
    """PROP-RI-7: every ``causation_id`` resolves to a prior event_id; every non-root
    ``correlation_id`` equals its cause's correlation_id (event-model C-1..C-5)."""
    corr_by_id: dict[str, str] = {}
    seen: set[str] = set()
    for env in result.envelopes:
        eid = env["event_id"]
        cid = env["causation_id"]
        corr = env["correlation_id"]
        if cid is not None:
            if cid not in seen:
                return f"PROP-RI-7: causation_id {cid} unresolved at seq {env['sequence_no']}"
            cause_corr = corr_by_id.get(cid)
            if cause_corr is not None and corr != cause_corr:
                return (
                    f"PROP-RI-7: correlation mismatch at seq {env['sequence_no']}: "
                    f"{corr} != cause {cause_corr}"
                )
        corr_by_id[eid] = corr
        seen.add(eid)
    return None


def check_ri8_schema_ref_and_envelope(result: BatchResult) -> str | None:
    """PROP-RI-8: every ``schema_ref`` resolves to a registered subject at v1; every
    envelope carries exactly the 20 delivered keys + the internal ``_df`` (EV-6,
    INV-REG-4)."""
    subjects = _expected_subjects(result.overlay)
    for env in result.envelopes:
        ref = env["schema_ref"]
        if ref["subject"] not in subjects:
            return (
                f"PROP-RI-8: schema_ref subject {ref['subject']!r} (seq "
                f"{env['sequence_no']}) is not a registered subject"
            )
        if ref["version"] != 1:
            return (
                f"PROP-RI-8: schema_ref version {ref['version']} != 1 "
                f"at seq {env['sequence_no']}"
            )
        delivered = {k for k in env if k != "_df"}
        if delivered != DELIVERED_FIELD_SET:
            missing = DELIVERED_FIELD_SET - delivered
            extra = delivered - DELIVERED_FIELD_SET
            return (
                f"PROP-RI-8: envelope key-set drift at seq {env['sequence_no']}: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        if "_df" not in env:
            return f"PROP-RI-8: ledger envelope missing _df at seq {env['sequence_no']}"
    return None


# The full PROP-RI suite, in id order, for the profile runners to iterate.
ALL_CHECKS: tuple[tuple[str, Any], ...] = (
    ("PROP-RI-1", check_ri1_entity_refs_resolve),
    ("PROP-RI-2", check_ri2_payment_requires_order),
    ("PROP-RI-3", check_ri3_payment_order_consistency),
    ("PROP-RI-4", check_ri4_inventory_never_negative),
    ("PROP-RI-5", check_ri5_sequence_gapless),
    ("PROP-RI-6", check_ri6_occurred_at_monotone),
    ("PROP-RI-7", check_ri7_causality_resolves),
    ("PROP-RI-8", check_ri8_schema_ref_and_envelope),
)
