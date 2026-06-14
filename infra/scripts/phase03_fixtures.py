#!/usr/bin/env python3
"""Emit the Phase-3 demo's adversarial manifests as JSON on stdout.

Used by ``demo-phase03.sh`` steps 5 and 6 to POST a workspace-visibility manifest
that trips exactly one Layer-2 code so the API returns a 422 with that code:

* ``prob_sum``           -> MAN-V201 (a state's outgoing probabilities sum to 1.15)
* ``escape_less_cycle``  -> MAN-V205 (two non-terminal states only cycle to each
                           other; no path to absorption)

Both documents are otherwise Layer-1+2 valid (zero hooks — a workspace manifest
with a hook would fail MAN-V404 first) so the targeted code is the *only* error.
This is a demo data generator: no DataForge imports, pure stdlib JSON.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _base() -> dict[str, Any]:
    """A minimal, workspace-valid one-entity manifest (slug ``demo``)."""
    return {
        "manifest_schema": "v0",
        "metadata": {
            "slug": "demo",
            "version": "1.0.0",
            "title": "Phase 3 demo manifest",
            "actor_entity": "users",
            "simulated_timezone": "UTC",
        },
        "entities": {
            "users": {
                "key_prefix": "usr",
                "key_attribute": "user_id",
                "attributes": {"full_name": {"generator": "person.full_name"}},
            }
        },
        "relationships": [],
        "event_types": {
            "session_started": {
                "partition_by": "actor",
                "payload": {"user_id": {"from": "actor.user_id"}},
            }
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
                            {"to": "checkout", "probability": 0.60, "emit": "session_started"}
                        ],
                    },
                    "checkout": {
                        "remainder": "exit",
                        "transitions": [{"to": "ordered", "probability": 0.70}],
                    },
                    "ordered": {"terminal": True},
                },
            }
        },
        "cdc": {"entities": {"users": {"enabled_default": True, "ops": ["c", "u"]}}},
        "seeding": {"catalogs": {"users": {"default": 5000, "min": 100, "max": 100000}}},
    }


def prob_sum() -> dict[str, Any]:
    """MAN-V201 — ``checkout`` outgoing probabilities 0.70 + 0.45 = 1.15 > 1.0."""
    doc = _base()
    checkout = doc["state_machines"]["shopping_session"]["states"]["checkout"]
    checkout["transitions"] = [
        {"to": "ordered", "probability": 0.70},
        {"to": "ordered", "probability": 0.45},
    ]
    checkout.pop("remainder", None)
    return doc


def escape_less_cycle() -> dict[str, Any]:
    """MAN-V205 — a lifecycle machine whose two states only transition to each other."""
    doc = _base()
    doc["state_machines"]["order_lifecycle"] = {
        "type": "lifecycle",
        "binds": "users",
        "initial": "a",
        "states": {
            "a": {"transitions": [{"to": "b", "probability": 1.0}]},
            "b": {"transitions": [{"to": "a", "probability": 1.0}]},
        },
    }
    return doc


_FIXTURES = {"prob_sum": prob_sum, "escape_less_cycle": escape_less_cycle}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in _FIXTURES:
        sys.stderr.write(f"usage: {argv[0]} {{{'|'.join(_FIXTURES)}}}\n")
        return 2
    json.dump(_FIXTURES[argv[1]](), sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
