"""Layer-2 referential-integrity tests (MAN-V101…V111).

One failing fixture per code, asserting {code, path} (and bound/actual where the
check carries them).
"""

from __future__ import annotations

from dataforge_engine.manifest import validate_manifest

from .fixtures import valid_subset_manifest


def _codes(doc: dict[str, object]) -> list[str]:
    return validate_manifest(doc).codes()


def test_man_v101_undeclared_actor_entity() -> None:
    doc = valid_subset_manifest()
    doc["metadata"]["actor_entity"] = "ghosts"
    report = validate_manifest(doc)
    # binds(users) still ok; actor_entity now points nowhere → V101 + V211 mismatch.
    errs = [e for e in report.errors if e.code == "MAN-V101"]
    assert any(e.path == "/metadata/actor_entity" and e.actual == "ghosts" for e in errs)


def test_man_v102_relationship_source_attribute_missing() -> None:
    doc = valid_subset_manifest()
    doc["relationships"][0]["source_attribute"] = "nope"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V102"]
    assert any(e.path == "/relationships/0/source_attribute" for e in errs)


def test_man_v103_ref_fk_unknown_relationship() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["orders"]["attributes"]["user_id"]["params"]["relationship"] = "nope"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V103"]
    assert any("relationship" in e.path and e.actual == "nope" for e in errs)


def test_man_v104_within_op_on_non_timestamp() -> None:
    doc = valid_subset_manifest()
    guard = doc["state_machines"]["order_lifecycle"]["states"]["placed"]["transitions"][0]["guard"]
    guard["all"][0]["op"] = "within"  # item_count is numeric, not a timestamp
    report = validate_manifest(doc)
    assert "MAN-V104" in report.codes()


def test_man_v105_payload_from_undeclared_created_entity() -> None:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"]["order_id"]["from"] = "created.ghost.id"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V105"]
    assert any(e.actual == "ghost" for e in errs)


def test_man_v105_created_ref_not_created_by_emitting_transition() -> None:
    """Cross-context closure: created.users.* on an event whose emitter creates orders."""
    doc = valid_subset_manifest()
    # order_placed's emitting transition creates 'orders', not 'users'.
    doc["event_types"]["order_placed"]["payload"]["uname"] = {"from": "created.users.full_name"}
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V105"]
    assert any(e.actual == "users" for e in errs)


def test_man_v106_partition_by_undeclared_created_entity() -> None:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["partition_by"] = "created.ghost"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V106"]
    assert any(e.path == "/event_types/order_placed/partition_by" for e in errs)


def test_man_v107_emit_unknown_event_type() -> None:
    doc = valid_subset_manifest()
    states = doc["state_machines"]["shopping_session"]["states"]
    states["started"]["transitions"][0]["emit"] = "nope"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V107"]
    assert any(e.actual == "nope" for e in errs)


def test_man_v107_event_type_never_emitted() -> None:
    doc = valid_subset_manifest()
    doc["event_types"]["orphan_event"] = {"payload": {"x": {"const": "y"}}}
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V107"]
    assert any(e.actual == "orphan_event" for e in errs)


def test_man_v108_cdc_undeclared_entity() -> None:
    doc = valid_subset_manifest()
    doc["cdc"]["entities"]["ghosts"] = {"enabled_default": True}
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V108"]
    assert any(e.path == "/cdc/entities/ghosts" for e in errs)


def test_man_v109_reserved_df_prefix_caught_at_layer1() -> None:
    """The §9.1 attribute pattern structurally bans a leading-underscore name.

    The reserved ``_df`` prefix is unrepresentable at Layer 1 (the ``^[a-z]…``
    pattern), so an L1-valid manifest can never carry a ``_df`` attribute — exactly
    the SB-1 "structurally impossible" property. This fixture trips MAN-S004 at the
    parent object pointer with the offending name in the message.
    """
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["_df_secret"] = {"generator": "text.word"}
    report = validate_manifest(doc)
    assert report.status == "failed"
    s004 = [e for e in report.errors if e.code == "MAN-S004"]
    assert any(
        e.path == "/entities/users/attributes" and "_df_secret" in e.message
        for e in s004
    )


def test_man_v109_defensive_semantic_double_check() -> None:
    """V109 is the SB-1 defensive double-check, exercised directly on the L2 walk.

    Layer 1 always catches a ``_df`` attribute first, so the semantic V109 is
    reachable only if a future grammar relaxed the pattern; the check is asserted
    against the semantic layer directly to prove the defence exists.
    """
    from dataforge_engine.manifest import ErrorCollector, ManifestView
    from dataforge_engine.manifest.semantic_referential import check_referential

    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["_df_secret"] = {"generator": "text.word"}
    collector = ErrorCollector()
    check_referential(ManifestView(doc), collector)
    assert any(
        e.code == "MAN-V109" and e.path == "/entities/users/attributes/_df_secret"
        for e in collector.errors
    )


def test_man_v110_created_at_attribute() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["created_at"] = {"generator": "time.now"}
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V110"]
    assert any(e.path == "/entities/users/attributes/created_at" for e in errs)


def test_man_v110_shadow_key_attribute() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["user_id"] = {"generator": "id.uuid"}
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V110"]
    assert any("user_id" in e.path for e in errs)


def test_man_v111_seeded_ref_fk_targets_unseeded() -> None:
    doc = valid_subset_manifest()
    # Drop the orders seed catalog so the orders.user_id ref.fk (orders is now
    # *unseeded*) does not trip; instead make a seeded entity target an unseeded
    # one. Add a seeded 'reviews' entity declared before 'users' isn't possible
    # (declaration order); simplest: make users ref.fk orders (users is seeded,
    # orders seeded but declared LATER → violates the earlier-declaration rule).
    doc["relationships"].append(
        {
            "name": "user_last_order",
            "source_entity": "users",
            "source_attribute": "last_order",
            "target_entity": "orders",
            "cardinality": "many_to_one",
        }
    )
    doc["entities"]["users"]["attributes"]["last_order"] = {
        "generator": "ref.fk",
        "params": {"relationship": "user_last_order"},
    }
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V111"]
    assert any(e.actual == "orders" for e in errs)
