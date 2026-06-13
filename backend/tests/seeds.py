"""The single seed registry (testing-strategy §16.1).

Every test references its seed *by name* from this module — never as an inline
literal (a GUARD lint enforces this from the phases that introduce generation).
Phase 2 only needs ``SEED_E2E`` (the demo / fixture seed, matching the PRD §2.2
instructor journey so docs and tests show identical data); the generation /
golden / statistical seeds are listed now so the registry is complete and later
phases import from one place.

All seeds are non-negative 63-bit integers (api-specification R-3, the seed
domain ``[0, 2**63 - 1]``).
"""

from __future__ import annotations

from typing import Final

SEED_VALIDATION: Final = 424242424242
SEED_GOLD_A: Final = 271828182845
SEED_GOLD_B: Final = 271828182846
SEED_GOLD_C: Final = 271828182847
SEED_STAT: Final = 314159265358
SEED_SOAK: Final = 161803398874
SEED_E2E: Final = 4242

_MAX_SEED: Final = 2**63 - 1

ALL_SEEDS: Final = {
    "SEED_VALIDATION": SEED_VALIDATION,
    "SEED_GOLD_A": SEED_GOLD_A,
    "SEED_GOLD_B": SEED_GOLD_B,
    "SEED_GOLD_C": SEED_GOLD_C,
    "SEED_STAT": SEED_STAT,
    "SEED_SOAK": SEED_SOAK,
    "SEED_E2E": SEED_E2E,
}

assert all(0 <= s <= _MAX_SEED for s in ALL_SEEDS.values()), "seed out of the R-3 domain"

__all__ = [
    "ALL_SEEDS",
    "SEED_E2E",
    "SEED_GOLD_A",
    "SEED_GOLD_B",
    "SEED_GOLD_C",
    "SEED_SOAK",
    "SEED_STAT",
    "SEED_VALIDATION",
]
