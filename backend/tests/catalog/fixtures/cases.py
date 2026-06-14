"""The adversarial corpus: one :class:`AdversarialCase` per emitted MAN-S/V code.

Each case binds a malformed-document *builder* (``tests.catalog.fixtures.builders``
/ ``builders_l2``) to the **exact** error tuple the validator must emit, plus the
validation *flavor* that routes the document to the validator the way that reaches
the code. :func:`run_case` executes a case and returns its observed errors; the
parametrized suite (``test_adversarial_corpus.py``) asserts at least one observed
error matches the expected ``{code, path, bound, actual, scope}`` exactly.

Flavors:

* ``full`` — :func:`~dataforge_engine.manifest.validate_manifest` on the dict
  (the normal pipeline: parse-skip → L1 → L2);
* ``text`` — ``validate_manifest`` on raw YAML/JSON text (MAN-S001/2/3 surface as a
  failed report through the same entry point a caller uses);
* ``parse`` — ``parse_manifest_text`` raises ``ManifestParseError`` (the hardened
  front-end, asserted on ``exc.error``);
* ``layer2`` — :func:`~dataforge_engine.manifest.run_layer2` on a Layer-1-valid
  document (the only path to aggregate bounds and the R-DER-5 defensive check);
* ``workspace`` — ``validate_manifest(..., is_workspace_visibility=True)`` (V404);
* ``compat`` — ``validate_manifest(..., prior_schemas=...)`` (V501).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from dataforge_engine.manifest import (
    ErrorCollector,
    ManifestParseError,
    ValidationError,
    parse_manifest_text,
    run_layer2,
    validate_manifest,
)
from tests.catalog.fixtures import builders as B
from tests.catalog.fixtures import builders_l2 as L2

Flavor = Literal["full", "text", "parse", "layer2", "workspace", "compat"]

# A builder returns a malformed document (dict) or, for ``text``/``parse`` cases,
# raw manifest text. The ``compat`` flavor's builder returns (doc, prior_schemas).
Builder = Callable[[], Any]

Scalar = str | int | float | bool | None


@dataclass(frozen=True)
class AdversarialCase:
    """One adversarial fixture and the exact error tuple it must produce."""

    name: str
    code: str
    build: Builder
    flavor: Flavor = "full"
    path: str | None = None
    bound: Scalar = None
    actual: Scalar = None
    scope: str = "manifest"
    # Fields not pinned by this case (e.g. an actual that varies float-to-float):
    # listed here so the matcher ignores them rather than over-asserting.
    unpinned: frozenset[str] = field(default_factory=frozenset)


def run_case(case: AdversarialCase) -> list[ValidationError]:
    """Execute ``case`` and return the observed validation errors."""
    built = case.build()
    if case.flavor == "parse":
        try:
            parse_manifest_text(built)
        except ManifestParseError as exc:
            return [exc.error]
        raise AssertionError(f"{case.name}: expected ManifestParseError ({case.code})")
    if case.flavor in ("full", "text"):
        return list(validate_manifest(built).errors)
    if case.flavor == "workspace":
        return list(validate_manifest(built, is_workspace_visibility=True).errors)
    if case.flavor == "compat":
        doc, prior = built
        provider = _FixedPrior(prior)
        return list(validate_manifest(doc, prior_schemas=provider).errors)
    if case.flavor == "layer2":
        collector = ErrorCollector()
        run_layer2(built, collector)
        return list(collector.errors)
    raise AssertionError(f"{case.name}: unknown flavor {case.flavor!r}")


class _FixedPrior:
    """A ``PriorSchemaProvider`` returning fixed registered payload schemas."""

    def __init__(self, schemas: dict[str, dict[str, Any]]) -> None:
        self._schemas = schemas

    def latest_payload_schema(self, subject: str) -> dict[str, Any] | None:
        return self._schemas.get(subject)


# ---------------------------------------------------------------------------
# THE CORPUS — one case per emitted code (testing-strategy §16.3).
# ---------------------------------------------------------------------------

CORPUS: tuple[AdversarialCase, ...] = (
    # --- Parse hardening (MAN-S001…S003) ------------------------------------
    AdversarialCase("anchor_alias", "MAN-S001", B.yaml_with_alias, "parse", path=""),
    AdversarialCase(
        "oversize_document", "MAN-S002", B.oversize_document_text, "text",
        path="", unpinned=frozenset({"actual"}),
    ),
    AdversarialCase(
        "too_deep_document", "MAN-S003", B.too_deep_document_text, "text",
        path="", unpinned=frozenset({"actual"}),
    ),
    # --- Layer-1 schema (MAN-S004) ------------------------------------------
    AdversarialCase("bad_schema_const", "MAN-S004", B.bad_manifest_schema_const,
                    path="/manifest_schema"),
    # --- Referential (MAN-V101…V111) ----------------------------------------
    AdversarialCase("undeclared_actor", "MAN-V101", B.undeclared_actor_entity,
                    path="/metadata/actor_entity", actual="ghosts"),
    AdversarialCase("rel_source_attr_missing", "MAN-V102", B.relationship_source_attr_missing,
                    path="/relationships/0/source_attribute"),
    AdversarialCase("ref_fk_unknown_rel", "MAN-V103", B.ref_fk_unknown_relationship,
                    actual="nope", unpinned=frozenset({"path"})),
    AdversarialCase("within_on_non_timestamp", "MAN-V104", B.within_op_on_non_timestamp,
                    unpinned=frozenset({"path"})),
    AdversarialCase("from_undeclared_created", "MAN-V105", B.payload_from_undeclared_created_entity,
                    actual="ghost", unpinned=frozenset({"path"})),
    AdversarialCase("partition_undeclared_created", "MAN-V106",
                    B.partition_by_undeclared_created_entity,
                    path="/event_types/order_placed/partition_by"),
    AdversarialCase("emit_unknown_event", "MAN-V107", B.emit_unknown_event_type,
                    actual="nope", unpinned=frozenset({"path"})),
    AdversarialCase("cdc_undeclared_entity", "MAN-V108", B.cdc_undeclared_entity,
                    path="/cdc/entities/ghosts"),
    AdversarialCase("reserved_df_attribute", "MAN-V109", B.reserved_df_attribute, "layer2",
                    path="/entities/users/attributes/_df_secret"),
    AdversarialCase("created_at_attribute", "MAN-V110", B.created_at_attribute,
                    path="/entities/users/attributes/created_at"),
    AdversarialCase("seeded_ref_fk_order", "MAN-V111", B.seeded_ref_fk_targets_later_declared,
                    actual="orders", unpinned=frozenset({"path"})),
    # --- Machine structure (MAN-V201…V211) ----------------------------------
    AdversarialCase("prob_sum_exceeds_one", "MAN-V201", L2.probability_sum_exceeds_one,
                    path="/state_machines/shopping_session/states/checkout", bound=1.0,
                    unpinned=frozenset({"actual"})),
    AdversarialCase("remainder_fully_allocated", "MAN-V202", L2.remainder_on_fully_allocated_state,
                    unpinned=frozenset({"path"})),
    AdversarialCase("terminal_with_transitions", "MAN-V203", L2.terminal_state_with_transitions,
                    unpinned=frozenset({"path"})),
    AdversarialCase("orphan_state", "MAN-V204", L2.orphan_state,
                    actual="island", unpinned=frozenset({"path"})),
    AdversarialCase("escape_less_scc", "MAN-V205", L2.escape_less_scc,
                    unpinned=frozenset({"path"})),
    AdversarialCase("fully_guarded_no_exit", "MAN-V206", L2.fully_guarded_without_exit_remainder,
                    unpinned=frozenset({"path"})),
    AdversarialCase("expected_steps_exceeded", "MAN-V207", L2.expected_steps_exceeds_bound,
                    bound=1000, unpinned=frozenset({"path", "actual"})),
    AdversarialCase("prob_outside_override", "MAN-V208", L2.probability_outside_override_bounds,
                    unpinned=frozenset({"path"})),
    AdversarialCase("non_terminal_dead_end", "MAN-V209", L2.non_terminal_dead_end,
                    unpinned=frozenset({"path"})),
    AdversarialCase("two_session_machines", "MAN-V210", L2.two_session_machines,
                    unpinned=frozenset({"path"})),
    AdversarialCase("session_binds_non_actor", "MAN-V211", L2.session_binds_non_actor,
                    unpinned=frozenset({"path"})),
    # --- Resource bounds (MAN-V304/305/308/312/314/315) ---------------------
    AdversarialCase("total_attrs_exceeded", "MAN-V304", L2.total_attributes_exceeded, "layer2",
                    bound=2000, actual=2250, unpinned=frozenset({"path"})),
    AdversarialCase("subjects_exceeded", "MAN-V305", L2.subjects_exceeded, "layer2",
                    bound=250, actual=251, unpinned=frozenset({"path"})),
    AdversarialCase("seed_below_min", "MAN-V308", L2.seed_default_below_min,
                    bound=100, actual=10, unpinned=frozenset({"path"})),
    AdversarialCase("entity_refs_exceeded", "MAN-V312", L2.entity_refs_exceeded, "layer2",
                    bound=16, actual=17, unpinned=frozenset({"path"})),
    AdversarialCase("background_mutations_exceeded", "MAN-V314",
                    L2.background_mutations_total_exceeded, bound=20, actual=21,
                    unpinned=frozenset({"path"})),
    AdversarialCase("duration_exceeds_year", "MAN-V315", L2.duration_exceeds_year, bound=365,
                    unpinned=frozenset({"path"})),
    # --- Generators (MAN-V401…V406) -----------------------------------------
    AdversarialCase("unknown_generator", "MAN-V401", L2.unknown_generator, "layer2",
                    actual="made.up", unpinned=frozenset({"path"})),
    AdversarialCase("unknown_param", "MAN-V402", L2.unknown_param,
                    actual="bogus", unpinned=frozenset({"path"})),
    AdversarialCase("hook_not_registered", "MAN-V403", L2.hook_name_not_registered,
                    actual="risk_score", unpinned=frozenset({"path"})),
    AdversarialCase("hook_in_workspace", "MAN-V404", L2.hook_in_workspace_manifest, "workspace",
                    unpinned=frozenset({"path"})),
    AdversarialCase("template_unknown_placeholder", "MAN-V405", L2.template_unknown_placeholder,
                    actual="not_a_sibling", unpinned=frozenset({"path"})),
    AdversarialCase("expression_illegal_token", "MAN-V406", L2.expression_illegal_token,
                    unpinned=frozenset({"path"})),
    # --- Schema compat (MAN-V407/V501/V502/V503) ----------------------------
    AdversarialCase("effect_write_type_mismatch", "MAN-V407", L2.effect_write_type_mismatch,
                    unpinned=frozenset({"path"})),
    AdversarialCase("removed_field_non_additive", "MAN-V501", L2.removed_payload_field_non_additive,
                    "compat", actual="removed_field", unpinned=frozenset({"path"})),
    AdversarialCase("cdc_subject_collision", "MAN-V502", L2.cdc_subject_collides_with_event,
                    "layer2", actual="cdc.users", path="/cdc/entities/users"),
    AdversarialCase("oversize_payload", "MAN-V503", L2.oversize_payload_estimate,
                    bound=64 * 1024, unpinned=frozenset({"path", "actual"})),
)
