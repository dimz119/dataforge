"""Layer-2 document builders for the adversarial corpus (MAN-V2xx…V5xx).

Machine-structure (V201…V211), resource bounds (V304…V315), generator allowlist
(V401…V406), and schema-compat (V407/V501/V502/V503) malformed manifests. Several
aggregate-bound builders deliberately produce documents that are *Layer-1 valid*
but trip a Layer-2 aggregate (e.g. Σ attributes), so the corpus drives them via
the ``layer2`` flavor (``run_layer2``) — the only way to reach those codes.
"""

from __future__ import annotations

import copy
from typing import Any

from tests.catalog.fixtures.base import valid_subset_manifest

# ---------------------------------------------------------------------------
# Machine structure (MAN-V201…V211).
# ---------------------------------------------------------------------------


def _lifecycle(states: dict[str, Any]) -> dict[str, Any]:
    """A valid base with the ``order_lifecycle`` machine's states replaced."""
    doc = valid_subset_manifest()
    doc["state_machines"]["order_lifecycle"]["initial"] = next(iter(states))
    doc["state_machines"]["order_lifecycle"]["states"] = states
    return doc


def probability_sum_exceeds_one() -> dict[str, Any]:
    """MAN-V201 — the demo case: ``checkout`` probabilities sum to 1.15 > 1.0."""
    doc = valid_subset_manifest()
    state = doc["state_machines"]["shopping_session"]["states"]["checkout"]
    state["transitions"] = [
        {"to": "ordered", "probability": 0.70, "emit": "order_placed"},
        {"to": "ordered", "probability": 0.45},
    ]
    state.pop("remainder", None)
    return doc


def remainder_on_fully_allocated_state() -> dict[str, Any]:
    return _lifecycle(
        {
            "a": {"remainder": "exit", "transitions": [{"to": "b", "probability": 1.0}]},
            "b": {"terminal": True},
        }
    )


def terminal_state_with_transitions() -> dict[str, Any]:
    return _lifecycle(
        {
            "a": {"remainder": "exit", "transitions": [{"to": "b", "probability": 0.5}]},
            "b": {"terminal": True, "transitions": [{"to": "a", "probability": 0.5}]},
        }
    )


def orphan_state() -> dict[str, Any]:
    return _lifecycle(
        {
            "a": {"remainder": "exit", "transitions": [{"to": "b", "probability": 0.5}]},
            "b": {"terminal": True},
            "island": {"remainder": "exit", "transitions": [{"to": "b", "probability": 0.5}]},
        }
    )


def escape_less_scc() -> dict[str, Any]:
    """MAN-V205 — the demo case: ``a ⇄ b`` with no path to absorption."""
    return _lifecycle(
        {
            "a": {"transitions": [{"to": "b", "probability": 1.0}]},
            "b": {"transitions": [{"to": "a", "probability": 1.0}]},
        }
    )


def fully_guarded_without_exit_remainder() -> dict[str, Any]:
    return _lifecycle(
        {
            "a": {
                "transitions": [
                    {
                        "to": "b",
                        "probability": 0.9,
                        "guard": {"all": [{"path": "subject.item_count", "op": "gte", "value": 1}]},
                    }
                ]
            },
            "b": {"terminal": True},
        }
    )


def expected_steps_exceeds_bound() -> dict[str, Any]:
    """MAN-V207 — a self-loop with tiny escape: ~1/0.0005 = 2000 > 1000 steps."""
    return _lifecycle(
        {
            "a": {
                "transitions": [
                    {"to": "a", "probability": 0.9995},
                    {"to": "b", "probability": 0.0005},
                ]
            },
            "b": {"terminal": True},
        }
    )


def probability_outside_override_bounds() -> dict[str, Any]:
    doc = valid_subset_manifest()
    txn = doc["state_machines"]["shopping_session"]["states"]["checkout"]["transitions"][0]
    txn["probability"] = 0.99  # override max is 0.95
    return doc


def non_terminal_dead_end() -> dict[str, Any]:
    return _lifecycle(
        {
            "a": {"remainder": "exit", "transitions": [{"to": "b", "probability": 0.5}]},
            "b": {},  # non-terminal, no transitions, no timeout
        }
    )


def two_session_machines() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["state_machines"]["order_lifecycle"]["type"] = "session"
    doc["state_machines"]["order_lifecycle"]["binds"] = "users"
    return doc


def session_binds_non_actor() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["state_machines"]["shopping_session"]["binds"] = "orders"  # actor_entity is users
    return doc


# ---------------------------------------------------------------------------
# Resource bounds (MAN-V304/V305/V308/V312/V314/V315). The aggregate counts use
# the ``layer2`` flavor (Layer-1 caps per-object, not the document total).
# ---------------------------------------------------------------------------


def total_attributes_exceeded() -> dict[str, Any]:
    """MAN-V304 — 25 entities x 90 attrs = 2250 > 2000 (each under the L1 cap)."""
    doc = valid_subset_manifest()
    doc["entities"] = {
        f"e{i}": {
            "key_prefix": "ab",
            "key_attribute": "k",
            "attributes": {f"a{j}": {"generator": "text.word"} for j in range(90)},
        }
        for i in range(25)
    }
    return doc


def subjects_exceeded() -> dict[str, Any]:
    """MAN-V305 — 200 event types + 51 cdc entities = 251 derived subjects > 250."""
    doc = valid_subset_manifest()
    doc["event_types"] = {f"evt_{i}": {"payload": {"x": {"const": "y"}}} for i in range(200)}
    doc["cdc"] = {"entities": {f"e{i}": {"enabled_default": True} for i in range(51)}}
    return doc


def entity_refs_exceeded() -> dict[str, Any]:
    """MAN-V312 — 17 distinct ``created.*`` refs on one event > 16 (R-EVT-5)."""
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"] = {
        f"f{i}": {"from": f"created.ent{i}.x"} for i in range(17)
    }
    return doc


def seed_default_below_min() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["seeding"]["catalogs"]["users"]["default"] = 10  # below min 100
    return doc


def background_mutations_total_exceeded() -> dict[str, Any]:
    """MAN-V314 — three CDC entities x 7 mutations = 21 > 20 document total."""
    doc = valid_subset_manifest()
    doc["entities"]["payments"] = {
        "key_prefix": "pay",
        "key_attribute": "payment_id",
        "attributes": {
            "amount": {"generator": "number.decimal", "params": {"min": "1.00", "max": "9.00"}}
        },
    }
    mut = {
        "name": "m",
        "rate": {"per": "entity_day", "probability": 0.01},
        "set": {
            "amount": {"generator": "number.decimal", "params": {"min": "1.00", "max": "9.00"}}
        },
    }
    for ename in ("users", "orders", "payments"):
        doc["cdc"]["entities"].setdefault(ename, {"enabled_default": True})
        doc["cdc"]["entities"][ename]["background_mutations"] = [
            {**mut, "name": f"{ename}{i}"} for i in range(7)
        ]
    return doc


def duration_exceeds_year() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["state_machines"]["shopping_session"]["session_timeout"] = "P400D"
    return doc


# ---------------------------------------------------------------------------
# Generators (MAN-V401…V406) and schema-compat (MAN-V407/V501/V502/V503).
# ---------------------------------------------------------------------------


def unknown_generator() -> dict[str, Any]:
    """MAN-V401 (semantic flavor) — a generator outside the 41-name allowlist."""
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["x"] = {"generator": "made.up"}
    return doc


def unknown_param() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["entities"]["orders"]["attributes"]["item_count"]["params"]["bogus"] = 1
    return doc


def hook_name_not_registered() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["risk"] = {
        "generator": "hook",
        "params": {"name": "risk_score"},
    }
    return doc


def hook_in_workspace_manifest() -> dict[str, Any]:
    return hook_name_not_registered()  # same doc; the flavor sets is_workspace_visibility


def template_unknown_placeholder() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["handle"] = {
        "generator": "template",
        "params": {"pattern": "{full_name}-{not_a_sibling}"},
    }
    return doc


def expression_illegal_token() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"]["total"] = {
        "generated": {
            "generator": "derived.expr",
            "params": {"expr": "session.cart[].price ** 2", "output": "decimal"},
        }
    }
    return doc


def effect_write_type_mismatch() -> dict[str, Any]:
    doc = valid_subset_manifest()
    effect = doc["state_machines"]["shopping_session"]["states"]["checkout"]["transitions"][0][
        "effects"
    ][0]
    effect["set"]["item_count"] = {"const": "not-a-number"}  # string into a number.int attr
    return doc


def cdc_subject_collides_with_event() -> dict[str, Any]:
    """MAN-V502 (semantic flavor) — a ``cdc.users`` event vs the ``cdc.users`` subject.

    L1 forbids a dotted event name, so this is reachable only on the semantic walk
    (the R-DER-5 defensive double-check, §8.2).
    """
    doc = valid_subset_manifest()
    doc["event_types"]["cdc.users"] = {"payload": {"x": {"const": "y"}}}
    return doc


def oversize_payload_estimate() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"] = {
        f"f{i}": {"generated": {"generator": "address.full"}} for i in range(20)
    }
    return doc


def removed_payload_field_non_additive() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """MAN-V501 — the prior schema has ``removed_field``; the new derivation drops it."""
    doc = valid_subset_manifest()
    prior = {
        "shop.order_placed": {
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "currency": {"const": "USD"},
                "total": {"type": "string"},
                "removed_field": {"type": "string"},
            }
        }
    }
    return doc, prior


def _clone(doc: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(doc)
