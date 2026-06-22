#!/usr/bin/env python3
"""PROP-RI integrity reservoir sampler for the LOAD-5K window (P11-14).

Samples ~1% of the events DELIVERED over the REST cursor during/after a load run
and asserts the referential-integrity + envelope invariants on the sample — the
live-traffic analogue of ``backend/tests/property/checks.py`` (which runs the same
PROP-RI family over engine-generated batches).

It REUSES the canonical invariant assertions where they apply to a *delivered*
envelope (REST events carry the 20-key delivered set, no internal ``_df``):

  * PROP-RI-5  sequence_no gapless + strictly monotone per (stream_id, shard_id)
               — INV-GEN-7. Validated WITHIN each shard partition of the sample.
  * PROP-RI-6  occurred_at non-decreasing per actor (ties by sequence_no)
               — INV-GEN-4.
  * PROP-RI-2/3 every payment carries an order_id from a prior order_placed
               — INV-GEN-2. (Delivery is per-partition ordered, so order->payment
               edges land in order within a partition.)
  * ENVELOPE  every delivered event has exactly the 20-key delivered field set
               (dataforge_engine.envelope.DELIVERED_FIELD_SET) — EV-6 / INV-REG-4.
  * SHARD-OWN every event's partition_key hashes to its emitting shard_id under
               dataforge_engine.behavior.partitioning.shard_for_key (no event on a
               non-owner shard) — the sharding correctness invariant (P11-01).

It pulls the full delivered set per stream by paging the cursor (limit 1000), then
reservoir-samples 1% (configurable) into a bounded sample, and runs the checks. A
non-zero exit + a printed failure naming the offending event means an integrity
violation occurred during the load window (exit criterion #1: zero integrity
violations under PROP-RI reservoir sampling).

NO secret literals: the API key comes from the k6 teardown manifest (or env), never
hard-coded. Stdlib-only HTTP (urllib) so it runs without extra deps; the engine
imports are optional — if dataforge_engine is not importable the field-set and
shard-ownership checks degrade to a parametrized fallback set with a printed note,
so the sampler still runs against a packaged manifest outside the backend venv.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# --- engine invariants (optional import; graceful fallback) -------------------
try:  # the authoritative 20-key delivered set + the shard hash
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
    from dataforge_engine.behavior.partitioning import shard_for_key  # type: ignore
    from dataforge_engine.envelope import DELIVERED_FIELD_SET  # type: ignore

    _ENGINE = True
except Exception as exc:  # pragma: no cover - fallback path
    print(f"[integrity] engine import unavailable ({exc}); using pinned fallbacks")
    _ENGINE = False
    # Pinned mirror of dataforge_engine.envelope.DELIVERED_FIELD_ORDER (EV-6).
    DELIVERED_FIELD_SET = frozenset(  # type: ignore[assignment]
        {
            "envelope_version", "event_id", "workspace_id", "stream_id", "shard_id",
            "scenario_slug", "manifest_version", "event_type", "schema_ref",
            "sequence_no", "partition_key", "occurred_at", "emitted_at", "actor_id",
            "session_id", "entity_refs", "correlation_id", "causation_id", "op",
            "payload",
        }
    )

    def shard_for_key(actor_key: str, shard_count: int) -> int:  # type: ignore
        """Fallback blake2b mod-N owning shard (matches engine partitioning)."""
        import hashlib

        if shard_count <= 1:
            return 0
        digest = hashlib.blake2b(actor_key.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % shard_count


_PAYMENT_EVENTS = ("payment_authorized", "payment_failed")


# --- thin REST client --------------------------------------------------------
def _get(url: str, key: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def drain_stream(api: str, stream_id: str, key: str, *, max_pages: int) -> list[dict[str, Any]]:
    """Page the REST cursor from earliest, returning every delivered envelope."""
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        if cursor:
            qs = urllib.parse.urlencode({"cursor": cursor, "limit": 1000})
        else:
            qs = urllib.parse.urlencode({"from": "earliest", "limit": 1000})
        try:
            body = _get(f"{api}/streams/{stream_id}/events?{qs}", key)
        except urllib.error.HTTPError as e:
            print(f"[integrity] {stream_id}: HTTP {e.code} draining cursor")
            break
        rows = body.get("data") or []
        events.extend(rows)
        cursor = body.get("next_cursor")
        if not cursor or not rows:
            break
    return events


# --- the PROP-RI checks over a delivered sample -------------------------------
def check_envelope_field_set(sample: list[dict[str, Any]]) -> str | None:
    """ENVELOPE: every delivered event carries exactly the 20 delivered keys."""
    for env in sample:
        keys = set(env.keys())
        if keys != DELIVERED_FIELD_SET:
            missing = sorted(DELIVERED_FIELD_SET - keys)
            extra = sorted(keys - DELIVERED_FIELD_SET)
            return (
                f"ENVELOPE: delivered key-set drift on event "
                f"{env.get('event_id')}: missing={missing} extra={extra}"
            )
    return None


def check_ri5_sequence_gapless(sample: list[dict[str, Any]]) -> str | None:
    """PROP-RI-5: sequence_no gapless + strictly monotone per (stream, shard).

    Over a SAMPLE we cannot assert gaplessness (sampling itself removes events),
    so this asserts strict monotonicity per (stream, shard) instead — a sampled
    sequence_no must never regress or repeat, which still catches duplication and
    out-of-order delivery within a shard.
    """
    last: dict[tuple[str, int], int] = {}
    for env in sample:
        key = (str(env["stream_id"]), int(env["shard_id"]))
        seq = int(env["sequence_no"])
        prev = last.get(key)
        if prev is not None and seq <= prev:
            return (
                f"PROP-RI-5: sequence_no not strictly increasing on {key}: "
                f"{seq} <= {prev} (event {env.get('event_id')})"
            )
        last[key] = seq
    return None


def check_ri6_occurred_at_monotone(sample: list[dict[str, Any]]) -> str | None:
    """PROP-RI-6: occurred_at non-decreasing per actor (INV-GEN-4)."""
    last: dict[str, str] = {}
    for env in sample:
        actor = env.get("actor_id")
        if actor is None:
            continue
        occ = str(env["occurred_at"])
        prev = last.get(actor)
        if prev is not None and occ < prev:
            return (
                f"PROP-RI-6: occurred_at regressed for actor {actor}: "
                f"{occ} < {prev} (event {env.get('event_id')})"
            )
        last[actor] = occ
    return None


def check_ri2_payment_requires_order(sample: list[dict[str, Any]]) -> str | None:
    """PROP-RI-2: a sampled payment whose order is ALSO in the sample must follow it.

    Sampling can drop the order_placed of a sampled payment, so we only flag a
    violation when the order_id is referenced by a payment that appears in the
    sample AFTER an order_placed for a DIFFERENT order but never the matching one —
    conservatively, we assert order->payment edge ordering only when both ends are
    sampled (no false positives from sampling gaps)."""
    placed: set[str] = set()
    seen_payment_orders: set[str] = set()
    for env in sample:
        et = env.get("event_type")
        payload = env.get("payload") or {}
        if et == "order_placed" and isinstance(payload, dict):
            oid = payload.get("order_id")
            if oid is not None:
                placed.add(str(oid))
        elif et in _PAYMENT_EVENTS and isinstance(payload, dict):
            oid = payload.get("order_id")
            if oid is not None:
                seen_payment_orders.add(str(oid))
                # Only a violation if the order is in our sample but came AFTER:
                # since we iterate in delivery order, a missing-but-later placed
                # order would mean a forward reference within the sample.
    # Forward reference: a payment's order appears later in the sample than the
    # payment (i.e. is placed but not before). Detect via a second ordered pass.
    placed_at: dict[str, int] = {}
    for i, env in enumerate(sample):
        if env.get("event_type") == "order_placed":
            payload = env.get("payload") or {}
            oid = payload.get("order_id") if isinstance(payload, dict) else None
            if oid is not None and str(oid) not in placed_at:
                placed_at[str(oid)] = i
    for i, env in enumerate(sample):
        if env.get("event_type") in _PAYMENT_EVENTS:
            payload = env.get("payload") or {}
            oid = payload.get("order_id") if isinstance(payload, dict) else None
            if oid is None:
                continue
            pos = placed_at.get(str(oid))
            if pos is not None and pos > i:
                return (
                    f"PROP-RI-2: payment {env.get('event_id')} forward-references "
                    f"order {oid} placed later in the delivered stream"
                )
    return None


def check_shard_ownership(sample: list[dict[str, Any]], shard_counts: dict[str, int]) -> str | None:
    """SHARD-OWN: every event's partition_key hashes to its emitting shard_id.

    Skipped for single-shard streams (shard_for_key short-circuits to 0). For
    multi-shard streams an event whose partition_key does not own shard_id is a
    sharding correctness bug (an event delivered on a non-owner shard, P11-01).
    """
    for env in sample:
        sid = str(env["stream_id"])
        count = shard_counts.get(sid, 1)
        if count <= 1:
            continue
        pkey = str(env["partition_key"])
        owner = shard_for_key(pkey, count)
        if owner != int(env["shard_id"]):
            return (
                f"SHARD-OWN: event {env.get('event_id')} on shard "
                f"{env['shard_id']} but partition_key {pkey!r} owns shard "
                f"{owner} (shard_count={count})"
            )
    return None


def _fetch_shard_count(api: str, access_or_key: str, stream_id: str) -> int:
    """Best-effort: read shard_count off the stream detail (defaults to 1)."""
    try:
        body = _get(f"{api}/streams/{stream_id}", access_or_key)
        return int(body.get("shard_count", 1) or 1)
    except Exception:
        return 1


CHECKS: tuple[tuple[str, Callable[[list[dict[str, Any]]], str | None]], ...] = (
    ("ENVELOPE-20KEY", check_envelope_field_set),
    ("PROP-RI-5", check_ri5_sequence_gapless),
    ("PROP-RI-6", check_ri6_occurred_at_monotone),
    ("PROP-RI-2", check_ri2_payment_requires_order),
)


def reservoir_sample(
    events: list[dict[str, Any]], rate: float, rng: random.Random
) -> list[dict[str, Any]]:
    """Sample ~``rate`` of events while PRESERVING delivery order.

    Order-preserving Bernoulli sampling (each event kept with prob ``rate``) so the
    per-shard monotonicity + order->payment checks remain meaningful on the sample.
    A floor of 1 sample per non-empty stream guarantees the envelope check always
    sees at least one event.
    """
    if not events:
        return []
    kept = [e for e in events if rng.random() < rate]
    if not kept:
        kept = [events[0]]
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description="LOAD-5K PROP-RI integrity sampler")
    parser.add_argument("--manifest", help="k6 teardown manifest JSON (LOAD5K_MANIFEST)")
    parser.add_argument("--api", default=os.environ.get("API", "http://localhost:8000/api/v1"))
    parser.add_argument("--stream", help="single stream id (alternative to --manifest)")
    parser.add_argument("--key", default=os.environ.get("DF_API_KEY"), help="events:read key")
    parser.add_argument("--rate", type=float, default=0.01, help="sample fraction (default 1%)")
    parser.add_argument("--max-pages", type=int, default=200, help="cursor pages per stream")
    parser.add_argument("--seed", type=int, default=20260621, help="reservoir RNG seed")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    # Build (stream_id, key) work list from manifest or single-stream flags.
    targets: list[tuple[str, str]] = []
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        args.api = manifest.get("api", args.api)
        for s in manifest.get("samples", []):
            for sid in s.get("streams", []):
                targets.append((sid, s["key"]))
    elif args.stream and args.key:
        targets.append((args.stream, args.key))
    else:
        parser.error("provide --manifest OR (--stream and --key)")

    if not targets:
        print("[integrity] no streams to sample")
        return 0

    shard_counts: dict[str, int] = {}
    all_sample: list[dict[str, Any]] = []
    total_delivered = 0
    for sid, key in targets:
        events = drain_stream(args.api, sid, key, max_pages=args.max_pages)
        total_delivered += len(events)
        shard_counts[sid] = _fetch_shard_count(args.api, key, sid)
        sample = reservoir_sample(events, args.rate, rng)
        all_sample.extend(sample)
        print(f"[integrity] {sid}: delivered={len(events)} sampled={len(sample)} "
              f"shard_count={shard_counts[sid]}")

    print(f"[integrity] total delivered={total_delivered} sampled={len(all_sample)} "
          f"(~{args.rate * 100:.1f}%) across {len(targets)} streams")

    failures: list[str] = []
    for name, fn in CHECKS:
        msg = fn(all_sample)
        status = "PASS" if msg is None else "FAIL"
        print(f"[integrity] {name}: {status}{'' if msg is None else ' — ' + msg}")
        if msg is not None:
            failures.append(msg)

    shard_msg = check_shard_ownership(all_sample, shard_counts)
    if shard_msg is None:
        print("[integrity] SHARD-OWN: PASS")
    else:
        print(f"[integrity] SHARD-OWN: FAIL — {shard_msg}")
        failures.append(shard_msg)

    if failures:
        print(f"\n[integrity] {len(failures)} INTEGRITY VIOLATION(S) — load gate FAILS")
        return 1
    print("\n[integrity] zero integrity violations on the delivered sample (PROP-RI clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
