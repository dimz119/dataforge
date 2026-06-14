#!/usr/bin/env python3
"""Emit the Phase-4 demo's adversarial manifest(s) as JSON on stdout.

Used by ``demo-phase04.sh`` step 10 to POST a workspace-visibility manifest that
passes Layer-1+2 (static) validation but **livelocks at runtime**, so the Layer-3
dry run (the real engine, plugin-arch §8.4) is the only stage that can catch it:

* ``livelock`` -> MAN-D602 (a near-absorbing ``stay`` loop: a guarded transition's
  mass appears to escape to a terminal so V205/V207 pass statically, but the guard
  never holds at runtime; selection falls through to ``remainder: stay`` with PT0S
  dwell, so virtual time never advances and the traversal hits the 10,000-
  transition hard cap → MAN-D602).

This mirrors the committed unit fixture in
``backend/tests/catalog/test_dry_run_l3.py`` so the demo and the test prove the
same behaviour. Pure stdlib JSON — no DataForge imports.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def livelock() -> dict[str, Any]:
    """MAN-D602 — passes L1+L2, livelocks at runtime (guard-induced).

    Structurally identical to the committed unit fixture ``_LIVELOCK`` in
    ``backend/tests/catalog/test_dry_run_l3.py`` (proven to pass L1+L2 and trip
    MAN-D602), so the demo and the test exercise the same behaviour. Only the slug
    differs (``livelock-demo``) so the POSTed workspace scenario is recognisable.
    """
    return {
        "manifest_schema": "v0",
        "metadata": {
            "slug": "livelock_demo",
            "version": "1.0.0",
            "title": "Phase 4 L3 livelock demo",
            "actor_entity": "users",
        },
        "entities": {
            "users": {
                "key_prefix": "usr",
                "key_attribute": "user_id",
                "attributes": {
                    "tier": {"generator": "choice.uniform", "params": {"options": ["free"]}},
                },
            },
        },
        "event_types": {
            "noop": {"payload": {"user_id": {"from": "actor.user_id"}}},
        },
        "state_machines": {
            "shopping_session": {
                "type": "session",
                "binds": "users",
                "initial": "spinning",
                "states": {
                    "spinning": {
                        # V207 sees the guarded 0.95 mass escaping to the terminal;
                        # statically it looks near-absorbing, so L1+L2 pass.
                        "remainder": "stay",
                        "transitions": [
                            {
                                "to": "done",
                                "probability": 0.95,
                                "guard": {
                                    "all": [
                                        {"path": "actor.tier", "op": "eq",
                                         "value": "impossible"},
                                    ]
                                },
                            },
                            # Unguarded self-loop: at runtime the guard above never
                            # holds, so this + remainder:stay re-enter forever
                            # without advancing virtual time → the transition cap.
                            {"to": "spinning", "probability": 0.04, "emit": "noop"},
                        ],
                    },
                    "done": {"terminal": True},
                },
            },
        },
        "seeding": {"catalogs": {"users": {"default": 100, "min": 1, "max": 1000}}},
    }


_FIXTURES = {"livelock": livelock}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in _FIXTURES:
        sys.stderr.write(f"usage: {argv[0]} {{{'|'.join(_FIXTURES)}}}\n")
        return 2
    json.dump(_FIXTURES[argv[1]](), sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
