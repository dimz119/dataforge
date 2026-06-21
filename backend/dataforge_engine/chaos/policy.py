"""The ``ChaosPolicy`` document shape (chaos-engine §3.2, normative).

One JSON document per stream: the seven mode keys (always present) plus
``on_stop_policy``. These ``TypedDict``s are the in-memory representation the
stage pipeline reads — the framework-free mirror of the api-spec / manifest
``chaosDefaults`` schema. Validation (§3.4, codes CH-V01…CH-V09) is enforced by
the Django ``chaos`` app on write; this module carries only the structural shape
and the seven frozen ``ChaosMode`` identifiers, plus the mode defaults the
pipeline falls back to when ``params`` keys are absent.

Pure Python: no Django, no third-party imports (BE-ENG-1).
"""

from __future__ import annotations

from typing import Final, Literal, TypedDict

# The seven ChaosMode identifiers, frozen forever (domain-model §2.7). These
# strings appear verbatim in configs, presets, ``_df.chaos`` keys, injection
# records, answer-key responses, and metrics labels (§3.2).
ChaosMode = Literal[
    "missing",
    "duplicates",
    "corrupted_values",
    "nulls",
    "schema_drift",
    "out_of_order",
    "late_arriving",
]

CHAOS_MODES: Final[tuple[ChaosMode, ...]] = (
    "missing",
    "duplicates",
    "corrupted_values",
    "nulls",
    "schema_drift",
    "out_of_order",
    "late_arriving",
)

OnStopPolicy = Literal["discard", "flush"]

# Validation bound shared by every mode (CH-V01, B-16): 0 < rate <= 0.5.
RATE_MAX: Final[float] = 0.5


class ModeConfig(TypedDict):
    """A single mode entry of the policy document (§3.2 common keys).

    ``params`` is the per-mode catalog (§5); the pipeline reads only the keys its
    stage understands and applies §5 defaults for any absent key.
    """

    enabled: bool
    rate: float
    params: dict[str, object]


class ChaosPolicy(TypedDict):
    """The full per-stream policy document (§3.2). Closed shape (CH-V09): exactly
    the seven mode keys plus ``on_stop_policy``.
    """

    missing: ModeConfig
    duplicates: ModeConfig
    corrupted_values: ModeConfig
    nulls: ModeConfig
    schema_drift: ModeConfig
    out_of_order: ModeConfig
    late_arriving: ModeConfig
    on_stop_policy: OnStopPolicy


def _mode(enabled: bool, rate: float, params: dict[str, object]) -> ModeConfig:
    return {"enabled": enabled, "rate": rate, "params": params}


def default_policy() -> ChaosPolicy:
    """A fully-disabled policy with the §3.2 preset rates/params — the identity
    pipeline. Every mode ``enabled: false`` so the pipeline is a no-op transform.
    """
    return {
        "missing": _mode(False, 0.01, {"event_types": ["*"]}),
        "duplicates": _mode(
            False,
            0.05,
            {
                "copies": [{"count": 1, "weight": 1.0}],
                "spacing": {"mode": "adjacent"},
                "event_types": ["*"],
            },
        ),
        "corrupted_values": _mode(
            False,
            0.02,
            {"fields": ["*"], "kinds": ["*"], "max_fields_per_event": 1, "event_types": ["*"]},
        ),
        "nulls": _mode(
            False,
            0.02,
            {
                "fields": ["*"],
                "include_nullable": False,
                "max_fields_per_event": 1,
                "event_types": ["*"],
            },
        ),
        "schema_drift": _mode(False, 0.20, {"subjects": ["*"], "fields": ["*"]}),
        "out_of_order": _mode(False, 0.10, {"window": "PT60S", "event_types": ["*"]}),
        "late_arriving": _mode(
            False,
            0.03,
            {
                "delay": {"family": "lognormal", "median": "PT30M", "p95": "PT2H"},
                "max_delay": "PT24H",
                "event_types": ["*"],
            },
        ),
        "on_stop_policy": "discard",
    }


def event_type_eligible(event_type: str, selector: object) -> bool:
    """Resolve a ``params.event_types`` selector (§3.3) against one event type.

    ``["*"]`` (the default wildcard) matches everything; otherwise the type must
    appear in the listed names. A malformed selector matches nothing (the Django
    layer validates CH-V05 on write; the pipeline degrades safely).
    """
    if not isinstance(selector, list):
        return False
    if selector == ["*"]:
        return True
    return event_type in selector
