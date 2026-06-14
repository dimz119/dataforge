"""The closed built-in generator vocabulary (scenario-plugin-architecture §4).

This is **data**, not behaviour: the 41-generator allowlist plus the per-generator
``params`` catalog the validator consults for MAN-V401 (unknown generator) and
MAN-V402 (invalid params). The behaviour engine (Phase 4) consumes the **same**
catalog to bind generator closures, so the contract is single-sourced here and
exported (``GENERATOR_NAMES``, ``GENERATOR_CATALOG``, ``GeneratorSpec``).

Each ``GeneratorSpec`` declares:

* ``output`` — the value kind feeding R-DER-2 schema derivation (Phase 4 / registry
  agent reads this; the validator uses it for MAN-V407 effect-write type checks);
* ``params`` — a name → :class:`ParamSpec` map of accepted params with their type,
  bounds, defaults, and whether required. ``params`` keys not in this map are
  rejected (MAN-V402); the §9.1 Layer-1 schema already caps ``maxProperties: 16``.

``hook`` is in :data:`GENERATOR_NAMES` (it is a valid §9.1 enum member) but is
gated separately in Layer 2 (MAN-V403/V404) — its ``params`` are validated against
the registered hook signature, not this static catalog, so its entry carries an
open ``params`` marker (:data:`HOOK_OPEN_PARAMS`).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Value kinds — the R-DER-2 fragment family each generator's output maps to.
# Consumed by schema derivation (registry/Phase-4) and MAN-V407 type checks.
OutputKind = Literal[
    "string",
    "integer",
    "number",
    "decimal",
    "boolean",
    "enum",
    "object",
    "datetime",
    "entity_key",
    "scalar",  # choice.* before effect-write resolution; concrete kind decided downstream
    "hook",  # resolved from the registered hook's output_type
]

ParamType = Literal["int", "number", "string", "bool", "decimal_str", "array", "object"]


@dataclass(frozen=True)
class ParamSpec:
    """One accepted parameter of a generator (the MAN-V402 contract for a param)."""

    type: ParamType
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    # For string params constrained to a closed set (e.g. distribution names).
    choices: tuple[str, ...] | None = None
    # For string params with a length cap (e.g. template/sku patterns).
    max_length: int | None = None
    # For array params (e.g. choice options): bounds on item count.
    min_items: int | None = None
    max_items: int | None = None
    # Free-text note for human readers; not enforced.
    note: str = ""


@dataclass(frozen=True)
class GeneratorSpec:
    """A single built-in generator: its output kind and accepted params."""

    name: str
    output: OutputKind
    params: dict[str, ParamSpec] = field(default_factory=dict)
    # When True, this generator's params are validated dynamically (hook only),
    # not against ``params`` above.
    open_params: bool = False


HOOK_OPEN_PARAMS = True

_LOCALE = ParamSpec(type="string", note="locale ∈ shipped set; unknown → MAN-V402")


def _catalog() -> dict[str, GeneratorSpec]:
    """Build the closed catalog (41 builtins + the gated ``hook``)."""
    specs: list[GeneratorSpec] = [
        # 4.1 identity / personal / address
        GeneratorSpec("id.uuid", "string"),
        GeneratorSpec(
            "id.seq",
            "integer",
            {"start": ParamSpec("int"), "step": ParamSpec("int")},
        ),
        GeneratorSpec("person.first_name", "string", {"locale": _LOCALE}),
        GeneratorSpec("person.last_name", "string", {"locale": _LOCALE}),
        GeneratorSpec("person.full_name", "string", {"locale": _LOCALE}),
        GeneratorSpec(
            "person.email",
            "string",
            {
                "from": ParamSpec("string", note="sibling attr name"),
                "domains": ParamSpec("array", max_items=64),
            },
        ),
        GeneratorSpec(
            "person.username",
            "string",
            {"from": ParamSpec("string", note="sibling attr name")},
        ),
        GeneratorSpec("person.phone", "string", {"locale": _LOCALE}),
        GeneratorSpec("address.street", "string", {"locale": _LOCALE}),
        GeneratorSpec("address.city", "string", {"locale": _LOCALE}),
        GeneratorSpec("address.state", "string", {"locale": _LOCALE}),
        GeneratorSpec("address.postal_code", "string", {"locale": _LOCALE}),
        GeneratorSpec("address.country", "string", {"locale": _LOCALE}),
        GeneratorSpec("address.full", "object", {"locale": _LOCALE}),
        # 4.2 commerce / internet / text
        GeneratorSpec("commerce.product_name", "string"),
        GeneratorSpec(
            "commerce.category",
            "string",
            {"depth": ParamSpec("int", minimum=1, maximum=3)},
        ),
        GeneratorSpec("commerce.brand", "string"),
        GeneratorSpec(
            "commerce.sku",
            "string",
            {"pattern": ParamSpec("string", max_length=64)},
        ),
        GeneratorSpec(
            "commerce.price",
            "decimal",
            {
                "min": ParamSpec("decimal_str"),
                "max": ParamSpec("decimal_str"),
                "distribution": ParamSpec(
                    "string", choices=("uniform", "lognormal")
                ),
            },
        ),
        GeneratorSpec("internet.ip_v4", "string", {"private": ParamSpec("bool")}),
        GeneratorSpec("internet.user_agent", "string"),
        GeneratorSpec(
            "internet.url",
            "string",
            {"domains": ParamSpec("array", max_items=64)},
        ),
        GeneratorSpec("text.word", "string"),
        GeneratorSpec(
            "text.sentence",
            "string",
            {"max_words": ParamSpec("int", minimum=1, maximum=30)},
        ),
        GeneratorSpec(
            "text.paragraph",
            "string",
            {"max_sentences": ParamSpec("int", minimum=1, maximum=5)},
        ),
        # 4.3 numeric / choice / time
        GeneratorSpec(
            "number.int",
            "integer",
            {
                "min": ParamSpec("int", required=True),
                "max": ParamSpec("int", required=True),
            },
        ),
        GeneratorSpec(
            "number.float",
            "number",
            {
                "min": ParamSpec("number", required=True),
                "max": ParamSpec("number", required=True),
                "precision": ParamSpec("int", minimum=0, maximum=6),
            },
        ),
        GeneratorSpec(
            "number.normal",
            "number",
            {
                "mean": ParamSpec("number", required=True),
                "stddev": ParamSpec("number", required=True),
                "min": ParamSpec("number"),
                "max": ParamSpec("number"),
                "precision": ParamSpec("int", minimum=0, maximum=6),
            },
        ),
        GeneratorSpec(
            "number.lognormal",
            "number",
            {
                "median": ParamSpec("number", required=True),
                "p95": ParamSpec("number", required=True),
                "min": ParamSpec("number"),
                "max": ParamSpec("number"),
                "precision": ParamSpec("int", minimum=0, maximum=6),
            },
        ),
        GeneratorSpec(
            "number.zipf",
            "integer",
            {
                "n": ParamSpec("int", required=True, minimum=1, maximum=100000),
                "s": ParamSpec("number", minimum=0.5, maximum=2.0),
            },
        ),
        GeneratorSpec(
            "number.decimal",
            "decimal",
            {
                "min": ParamSpec("decimal_str", required=True),
                "max": ParamSpec("decimal_str", required=True),
                "scale": ParamSpec("int", minimum=0, maximum=4),
                "distribution": ParamSpec(
                    "string", choices=("uniform", "normal", "lognormal")
                ),
            },
        ),
        GeneratorSpec(
            "choice.weighted",
            "enum",
            {
                "options": ParamSpec(
                    "array", required=True, min_items=1, max_items=100
                )
            },
        ),
        GeneratorSpec(
            "choice.uniform",
            "enum",
            {
                "options": ParamSpec(
                    "array", required=True, min_items=1, max_items=100
                )
            },
        ),
        GeneratorSpec(
            "choice.boolean",
            "boolean",
            {"p_true": ParamSpec("number", minimum=0, maximum=1)},
        ),
        # 4.3 time
        GeneratorSpec("time.now", "datetime"),
        GeneratorSpec(
            "time.between",
            "datetime",
            {
                "start": ParamSpec("string", required=True, note="ISO duration"),
                "end": ParamSpec("string", required=True, note="ISO duration"),
            },
        ),
        # 4.4 templates / references / derived
        GeneratorSpec(
            "template",
            "string",
            {"pattern": ParamSpec("string", required=True, max_length=1024)},
        ),
        GeneratorSpec(
            "ref.fk",
            "entity_key",
            {
                "relationship": ParamSpec("string", required=True),
                "selection": ParamSpec(
                    "string", choices=("uniform", "zipf", "recent")
                ),
                "s": ParamSpec("number", minimum=0.5, maximum=2.0),
                "window": ParamSpec("string", note="duration, for recent"),
            },
        ),
        GeneratorSpec(
            "ref.attr",
            "scalar",
            {
                "via": ParamSpec("string", required=True, note="sibling ref.fk attr"),
                "attribute": ParamSpec("string", required=True),
            },
        ),
        GeneratorSpec(
            "derived.expr",
            "number",
            {
                "expr": ParamSpec("string", required=True, max_length=256),
                "output": ParamSpec(
                    "string", required=True, choices=("number", "integer", "decimal")
                ),
                "scale": ParamSpec("int", minimum=0, maximum=4),
            },
        ),
        # 4.6 gated escape hatch — params validated dynamically (MAN-V403)
        GeneratorSpec("hook", "hook", open_params=HOOK_OPEN_PARAMS),
    ]
    return {spec.name: spec for spec in specs}


GENERATOR_CATALOG: dict[str, GeneratorSpec] = _catalog()
"""The closed generator allowlist as ``name → GeneratorSpec`` (exported)."""

# The §9.1 ``generatorSpec.generator`` enum is exactly 41 members: 40 value
# builtins + the gated ``hook`` (the "41-generator vocabulary" of §4 counts the
# hook). ``hook`` is enum-valid but gated in Layer 2 (MAN-V403/V404).
GENERATOR_NAMES: tuple[str, ...] = tuple(GENERATOR_CATALOG.keys())
"""The §9.1 generator enum order (40 value builtins + ``hook``; 41 total)."""

# generators whose declared output is a money decimal string (R-DER-2 decimal frag).
DECIMAL_GENERATORS: frozenset[str] = frozenset(
    name for name, spec in GENERATOR_CATALOG.items() if spec.output == "decimal"
)
NUMERIC_OUTPUTS: frozenset[OutputKind] = frozenset(
    {"integer", "number", "decimal"}
)
