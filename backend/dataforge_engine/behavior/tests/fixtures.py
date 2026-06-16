"""Shared fixtures for the behavior-engine unit tests (pure Python, no Django).

A deterministic injected wall clock (1 ms per ``now()`` call, mirroring
testing-strategy §6) and a tiny synthetic manifest exercising the engine's core
paths: a session funnel with a probabilistic browse loop + checkout that creates
an order, a guarded order lifecycle (payment authorized only while the order is
``placed``), CDC on every entity, and an inventory ``adjust`` for the §5.3
check-and-adjust test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# A fixed virtual epoch for reproducible occurred_at stamps.
VIRTUAL_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
WORKSPACE_ID = "0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60"
STREAM_ID = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b"


class FixedWallClock:
    """A deterministic :class:`~dataforge_engine.ports.WallClock` (1 ms/tick).

    Each :meth:`now` returns a strictly increasing instant 1 ms after the last,
    so ``emitted_at`` is pinned and the full envelope is byte-identical across
    runs (GOLD-A under an injected wall clock).
    """

    def __init__(self, start: datetime | None = None, step_ms: int = 1) -> None:
        self._t = start or datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(milliseconds=step_ms)

    def now(self) -> datetime:
        value = self._t
        self._t += self._step
        return value


class CollectingLedger:
    """A :class:`~dataforge_engine.ports.LedgerSink` that records appended batches."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def append(self, envelopes: Any) -> None:
        self.rows.extend(envelopes)


def synthetic_manifest() -> dict[str, Any]:
    """A small, valid manifest the engine can compile and run end to end."""
    return {
        "manifest_schema": "v0",
        "metadata": {
            # 1.1.0 = the Phase-8 feature line (intensity curves, background
            # mutations, CDC-image marker hygiene). The synthetic fixture exercises
            # those behaviors (background_mutations below), so it declares the
            # version that turns them on (ManifestIR.phase8_features; behavior-engine §3.4).
            "slug": "synth", "version": "1.1.0", "title": "Synthetic",
            "actor_entity": "users", "simulated_timezone": "UTC",
        },
        "entities": {
            "users": {
                "key_prefix": "usr", "key_attribute": "user_id",
                "attributes": {
                    "full_name": {"generator": "person.full_name"},
                    "email": {"generator": "person.email", "params": {"from": "full_name"}},
                    "country": {"generator": "address.country"},
                },
            },
            "products": {
                "key_prefix": "prd", "key_attribute": "product_id",
                "attributes": {
                    "name": {"generator": "commerce.product_name"},
                    "price": {"generator": "number.decimal",
                              "params": {"min": "1.00", "max": "100.00", "scale": 2}},
                    "stock": {"generator": "number.int", "params": {"min": 5, "max": 5}},
                },
            },
            "orders": {
                "key_prefix": "ord", "key_attribute": "order_id",
                "attributes": {
                    "user_id": {"generator": "ref.fk", "params": {"relationship": "order_user"}},
                    "product_id": {"generator": "ref.fk",
                                   "params": {"relationship": "order_product"}},
                    "status": {"generator": "choice.uniform", "params": {"options": ["placed"]}},
                    "total": {"generator": "number.decimal",
                              "params": {"min": "1.00", "max": "100.00", "scale": 2}},
                },
            },
        },
        "relationships": [
            {"name": "order_user", "source_entity": "orders", "source_attribute": "user_id",
             "target_entity": "users", "cardinality": "many_to_one"},
            {"name": "order_product", "source_entity": "orders",
             "source_attribute": "product_id", "target_entity": "products",
             "cardinality": "many_to_one"},
        ],
        "event_types": {
            "session_started": {"partition_by": "actor",
                                "payload": {"user_id": {"from": "actor.user_id"}}},
            "product_viewed": {"partition_by": "actor",
                               "payload": {"user_id": {"from": "actor.user_id"},
                                           "product_id": {"from": "session.last.product_id"}}},
            "order_placed": {"partition_by": "actor",
                             "payload": {"order_id": {"from": "created.orders.order_id"},
                                         "user_id": {"from": "actor.user_id"}}},
            "payment_authorized": {"partition_by": "actor",
                                   "payload": {"order_id": {"from": "subject.order_id"},
                                               "user_id": {"from": "subject.user_id"}}},
        },
        "state_machines": {
            "shopping": {
                "type": "session", "binds": "users", "initial": "arrived",
                "session_timeout": "PT30M",
                "states": {
                    "arrived": {"transitions": [
                        {"to": "browsing", "probability": 1.0,
                         "dwell": {"family": "fixed", "value": "PT5S"},
                         "emit": "session_started"},
                    ]},
                    "browsing": {
                        "remainder": "exit",
                        "transitions": [
                            {"to": "browsing", "probability": 0.5,
                             "dwell": {"family": "fixed", "value": "PT10S"},
                             "effects": [{"action": "remember", "key": "last", "mode": "set",
                                          "value": {"product_id": {"generated": {
                                              "generator": "ref.fk",
                                              "params": {"relationship": "order_product"}}}}}],
                             "emit": "product_viewed"},
                            {"to": "checkout", "probability": 0.3,
                             "dwell": {"family": "fixed", "value": "PT20S"}},
                        ],
                    },
                    "checkout": {
                        "remainder": "exit",
                        "transitions": [
                            {"to": "done", "probability": 0.8,
                             "dwell": {"family": "fixed", "value": "PT30S"},
                             "effects": [
                                 {"action": "create", "entity": "orders",
                                  "set": {"user_id": {"from": "actor.user_id"},
                                          "product_id": {"from": "session.last.product_id"},
                                          "status": {"const": "placed"},
                                          "total": {"generated": {
                                              "generator": "number.decimal",
                                              "params": {"min": "1.00", "max": "100.00",
                                                         "scale": 2}}}}},
                                 {"action": "adjust", "target": "created.orders.via.order_product",
                                  "attribute": "stock", "by": -1},
                             ],
                             "emit": "order_placed"},
                        ],
                    },
                    "done": {"terminal": True},
                },
            },
            "order_lifecycle": {
                "type": "lifecycle", "binds": "orders", "initial": "placed",
                "states": {
                    "placed": {"transitions": [
                        {"to": "authorized", "probability": 1.0,
                         "dwell": {"family": "fixed", "value": "PT45S"},
                         "guard": {"all": [{"path": "subject.status", "op": "eq",
                                            "value": "placed"}]},
                         "effects": [{"action": "update", "target": "subject",
                                      "set": {"status": {"const": "authorized"}}}],
                         "emit": "payment_authorized"},
                    ]},
                    "authorized": {"terminal": True},
                },
            },
        },
        "cdc": {"entities": {
            "users": {
                "enabled_default": True, "ops": ["c", "u", "r"],
                # R-CDC-3 background drift (the E4 SCD2 feed): a country change with
                # no business event — a CDC-only chain root. High probability so the
                # tests reliably observe firings over a short window.
                "background_mutations": [
                    {"name": "country_change",
                     "rate": {"per": "entity_day", "probability": 0.5},
                     "set": {"country": {"generator": "address.country"}}},
                ],
            },
            "products": {
                "enabled_default": True, "ops": ["c", "u", "r"],
                "background_mutations": [
                    {"name": "price_change",
                     "rate": {"per": "entity_day", "probability": 0.3},
                     "set": {"price": {"generator": "number.decimal",
                                       "params": {"min": "1.00", "max": "100.00",
                                                  "scale": 2}}}},
                ],
            },
            "orders": {"enabled_default": True, "ops": ["c", "u"]},
        }},
        "seeding": {"catalogs": {
            "users": {"default": 10, "min": 1, "max": 100},
            "products": {"default": 5, "min": 1, "max": 100},
        }},
    }
