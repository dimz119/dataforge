"""A lightweight indexed view over a parsed manifest document.

After Layer 1 passes, the document is structurally a valid v0 manifest, so the
semantic layer can read it through typed accessors instead of re-checking shape.
:class:`ManifestView` indexes the parts the Layer-2 checks (and, later, the
behaviour engine) traverse repeatedly: entities and their attribute generators,
relationships keyed by name and by ``(source, target)``, event types, and state
machines. It is a read-only convenience over the raw ``dict`` — the canonical
document remains the source of truth and JSON Pointers are built against it.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Auto-maintained timestamps present on every entity (§3.1) — not declared, but
# referenceable and reserved.
AUTO_ATTRIBUTES: frozenset[str] = frozenset({"created_at", "updated_at"})


@dataclass(frozen=True)
class RelationshipView:
    name: str
    source_entity: str
    source_attribute: str
    target_entity: str
    cardinality: str
    on_target_delete: str
    owned: bool
    index: int  # position in the ``relationships`` array, for JSON Pointers


@dataclass(frozen=True)
class EntityView:
    name: str
    key_prefix: str
    key_attribute: str
    attributes: dict[str, dict[str, Any]]

    def declared_attribute_names(self) -> set[str]:
        """Declared attributes plus the implicit key + auto timestamps (§3.1)."""
        names = set(self.attributes.keys())
        names.add(self.key_attribute)
        names |= AUTO_ATTRIBUTES
        return names


class ManifestView:
    """Indexed, read-only accessor over a Layer-1-valid manifest ``dict``."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.metadata: dict[str, Any] = document.get("metadata", {})
        self.actor_entity: str = self.metadata.get("actor_entity", "")

        self.entities: dict[str, EntityView] = {}
        # Preserve declaration order (Python dicts are insertion-ordered) — used
        # by the seed-order DAG check (MAN-V111).
        for name, spec in document.get("entities", {}).items():
            self.entities[name] = EntityView(
                name=name,
                key_prefix=spec.get("key_prefix", ""),
                key_attribute=spec.get("key_attribute", ""),
                attributes=dict(spec.get("attributes", {})),
            )
        self.entity_order: list[str] = list(self.entities.keys())

        self.relationships: list[RelationshipView] = []
        self.relationships_by_name: dict[str, RelationshipView] = {}
        for idx, rel in enumerate(document.get("relationships", [])):
            view = RelationshipView(
                name=rel.get("name", ""),
                source_entity=rel.get("source_entity", ""),
                source_attribute=rel.get("source_attribute", ""),
                target_entity=rel.get("target_entity", ""),
                cardinality=rel.get("cardinality", ""),
                on_target_delete=rel.get("on_target_delete", "restrict"),
                owned=bool(rel.get("owned", False)),
                index=idx,
            )
            self.relationships.append(view)
            self.relationships_by_name[view.name] = view

        self.event_types: dict[str, dict[str, Any]] = dict(
            document.get("event_types", {})
        )
        self.state_machines: dict[str, dict[str, Any]] = dict(
            document.get("state_machines", {})
        )
        self.cdc: dict[str, Any] = document.get("cdc", {}) or {}
        self.seeding: dict[str, Any] = document.get("seeding", {}) or {}
        self.intensity: dict[str, Any] = document.get("intensity", {}) or {}
        self.chaos_defaults: dict[str, Any] = document.get("chaos_defaults", {}) or {}

    @property
    def slug(self) -> str:
        return str(self.metadata.get("slug", ""))

    def seeded_entities(self) -> set[str]:
        return set(self.seeding.get("catalogs", {}).keys())

    def cdc_entities(self) -> dict[str, dict[str, Any]]:
        return dict(self.cdc.get("entities", {}))

    def entity_attribute_generator(
        self, entity: str, attribute: str
    ) -> dict[str, Any] | None:
        """The generator spec for ``entity.attribute``, or ``None`` if absent."""
        ent = self.entities.get(entity)
        if ent is None:
            return None
        return ent.attributes.get(attribute)
