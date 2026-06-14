"""The valid base manifest — the validator's positive control (no DB needed).

This is the spine of the whole catalog test suite: the per-concern unit modules
and the adversarial corpus (:mod:`tests.catalog.fixtures.cases`) both deep-copy it
and mutate exactly one thing to trip exactly one MAN-S/V code.

:func:`valid_subset_manifest` returns a self-contained, Layer-1+2-valid manifest
exercising the parts the §8 pipeline checks: two entities with a relationship and
a seeded ``ref.fk`` DAG, a CDC config with a background mutation, an event type
with mixed payload sources, a session machine (purchase funnel) and a lifecycle
machine with a guarded transition. It is *not* the builtin ecommerce YAML (that
ships separately as data); it is the validator's positive control.
"""

from __future__ import annotations

import copy
from typing import Any


def valid_subset_manifest() -> dict[str, Any]:
    """A complete manifest that passes Layers 1+2."""
    return copy.deepcopy(_BASE)


_BASE: dict[str, Any] = {
    "manifest_schema": "v0",
    "metadata": {
        "slug": "shop",
        "version": "1.0.0",
        "title": "Shop subset",
        "actor_entity": "users",
        "simulated_timezone": "UTC",
    },
    "entities": {
        "users": {
            "key_prefix": "usr",
            "key_attribute": "user_id",
            "attributes": {
                "full_name": {"generator": "person.full_name"},
                "email": {
                    "generator": "person.email",
                    "params": {"from": "full_name"},
                },
                "status": {
                    "generator": "choice.uniform",
                    "params": {"options": ["active", "closed"]},
                },
            },
        },
        "orders": {
            "key_prefix": "ord",
            "key_attribute": "order_id",
            "attributes": {
                "user_id": {
                    "generator": "ref.fk",
                    "params": {"relationship": "order_user"},
                },
                "total": {
                    "generator": "number.decimal",
                    "params": {"min": "1.00", "max": "999.99"},
                },
                "item_count": {
                    "generator": "number.int",
                    "params": {"min": 1, "max": 10},
                },
                "stock": {
                    "generator": "number.int",
                    "params": {"min": 0, "max": 100},
                },
            },
        },
    },
    "relationships": [
        {
            "name": "order_user",
            "source_entity": "orders",
            "source_attribute": "user_id",
            "target_entity": "users",
            "cardinality": "many_to_one",
            "on_target_delete": "restrict",
        }
    ],
    "event_types": {
        "session_started": {
            "partition_by": "actor",
            "payload": {"user_id": {"from": "actor.user_id"}},
        },
        "order_placed": {
            "partition_by": "actor",
            "payload": {
                "order_id": {"from": "created.orders.order_id"},
                "user_id": {"from": "actor.user_id"},
                "currency": {"const": "USD"},
                "total": {
                    "generated": {
                        "generator": "number.decimal",
                        "params": {"min": "1.00", "max": "999.99"},
                    }
                },
            },
        },
        "order_confirmed": {
            "payload": {"order_id": {"from": "subject.order_id"}},
        },
    },
    "state_machines": {
        "shopping_session": {
            "type": "session",
            "binds": "users",
            "initial": "started",
            "session_timeout": "PT30M",
            "states": {
                "started": {
                    "remainder": "exit",
                    "transitions": [
                        {
                            "to": "checkout",
                            "probability": 0.60,
                            "emit": "session_started",
                        }
                    ],
                },
                "checkout": {
                    "remainder": "exit",
                    "transitions": [
                        {
                            "to": "ordered",
                            "probability": 0.70,
                            "dwell": {
                                "family": "lognormal",
                                "median": "PT3M",
                                "p95": "PT12M",
                            },
                            "effects": [
                                {
                                    "action": "create",
                                    "entity": "orders",
                                    "set": {
                                        "user_id": {"from": "actor.user_id"}
                                    },
                                }
                            ],
                            "emit": "order_placed",
                            "override": {"allowed": True, "min": 0.10, "max": 0.95},
                        }
                    ],
                },
                "ordered": {"terminal": True},
            },
        },
        "order_lifecycle": {
            "type": "lifecycle",
            "binds": "orders",
            "initial": "placed",
            "states": {
                "placed": {
                    "transitions": [
                        {
                            "to": "confirmed",
                            "probability": 0.95,
                            "guard": {
                                "all": [
                                    {
                                        "path": "subject.item_count",
                                        "op": "gte",
                                        "value": 1,
                                    }
                                ]
                            },
                            "emit": "order_confirmed",
                        }
                    ],
                    "remainder": "exit",
                },
                "confirmed": {"terminal": True},
            },
        },
    },
    "cdc": {
        "entities": {
            "users": {
                "enabled_default": True,
                "ops": ["c", "u"],
                "background_mutations": [
                    {
                        "name": "status_change",
                        "rate": {"per": "entity_day", "probability": 0.005},
                        "set": {
                            "status": {
                                "generator": "choice.uniform",
                                "params": {"options": ["active", "closed"]},
                            }
                        },
                    }
                ],
            },
            "orders": {"enabled_default": True, "ops": ["c", "u"]},
        }
    },
    "seeding": {
        "catalogs": {
            "users": {"default": 5000, "min": 100, "max": 100000},
            "orders": {"default": 1000, "min": 50, "max": 100000},
        }
    },
}
