"""ChaosPolicy write-time validation (chaos-engine §3.4; api-spec §4.8.3).

The single validation seam for a live ``PATCH /streams/{id}/chaos`` document. It
enforces the pinned bounds the engine assumes — the closed seven-mode shape plus
``on_stop_policy`` (CH-V09), and ``0 < rate ≤ 0.5`` for every mode (CH-V01, B-16).
Phase-9 scope is the rate + closed-shape gate; the per-mode param catalogs
(CH-V02..CH-V08) re-validate as the modes' params land. A violation raises
:class:`ChaosPolicyInvalid` carrying the §8.3 ``errors[]`` rows the
``manifest-validation-failed`` problem renders (422).
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.chaos import CHAOS_MODES, RATE_MAX

__all__ = ["ChaosPolicyInvalid", "validate_chaos_patch"]

_MODE_SET = frozenset(CHAOS_MODES)
_ON_STOP_VALUES = frozenset({"discard", "flush"})


class ChaosPolicyInvalid(Exception):
    """A chaos document failed §3.4 validation; carries the §8.3 ``errors[]``."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} chaos validation error(s)")


def _rate_error(mode: str, actual: Any) -> dict[str, Any]:
    return {
        "code": "CH-V01",
        "path": f"/{mode}/rate",
        "message": f"rate must be in (0, {RATE_MAX}]; got {actual!r}",
        "bound": RATE_MAX,
        "actual": actual,
        "scope": "chaos",
    }


def validate_chaos_patch(body: dict[str, Any]) -> None:
    """Validate a PATCH chaos document; raise :class:`ChaosPolicyInvalid` on any error.

    A PATCH body is a partial document (mode-level merge, §3.5): it carries only the
    keys to change. Unknown top-level keys are rejected (closed shape, CH-V09); every
    present mode entry must be a ``{enabled, rate, params}`` object; ``rate`` (when
    present, and required when ``enabled: true``) must satisfy ``0 < rate ≤ 0.5``
    (CH-V01); ``on_stop_policy`` must be ``discard``/``flush``.
    """
    errors: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        raise ChaosPolicyInvalid(
            [{"code": "CH-V09", "path": "/", "message": "body must be an object",
              "scope": "chaos"}]
        )
    for key, value in body.items():
        if key == "on_stop_policy":
            if value not in _ON_STOP_VALUES:
                errors.append({
                    "code": "CH-V09", "path": "/on_stop_policy",
                    "message": "on_stop_policy must be 'discard' or 'flush'",
                    "actual": value, "scope": "chaos",
                })
            continue
        if key not in _MODE_SET:
            errors.append({
                "code": "CH-V09", "path": f"/{key}",
                "message": (
                    f"unknown key {key!r}; allowed: the seven chaos modes + on_stop_policy"
                ),
                "scope": "chaos",
            })
            continue
        if not isinstance(value, dict):
            errors.append({
                "code": "CH-V09", "path": f"/{key}",
                "message": "mode entry must be a {enabled, rate, params} object",
                "scope": "chaos",
            })
            continue
        enabled = value.get("enabled", False)
        rate = value.get("rate")
        if rate is None:
            if enabled:
                errors.append({
                    "code": "CH-V01", "path": f"/{key}/rate",
                    "message": "rate is required when enabled is true",
                    "scope": "chaos",
                })
            continue
        if not isinstance(rate, (int, float)) or isinstance(rate, bool):
            errors.append(_rate_error(key, rate))
        elif not (0 < float(rate) <= RATE_MAX):
            errors.append(_rate_error(key, rate))
    if errors:
        raise ChaosPolicyInvalid(errors)
