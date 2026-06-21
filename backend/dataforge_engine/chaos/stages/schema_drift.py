"""``schema_drift`` — fields only from a registered next version (chaos-engine §5.5).

Fifth stage (after ``nulls``, before ``out_of_order``). For each eligible event
whose subject has a registered NEXT version (the drift field menu, DR-1): on
``draw(schema_drift, event_id, "select") < rate`` ADD every field selected by
``params.fields`` from the next version's added-field set, with values synthesized
type-directed from the next-version fragment via ``draw(…, "value:{path}")`` (DR-2).

The drift menu is NOT read from the DB by the engine — it arrives via the
``registry_view`` PORT on the context (a :class:`DriftMenuProvider`), mirroring how
``recorder`` is a port. When a subject has no next version the mode cannot arm for
it (CH-V07 at the API layer); the stage is a structural no-op for that subject.

Drift NEVER touches envelope fields (payload only) and NEVER drifts CDC ``before``
images (R-CDC-6) — added fields land in business payloads and CDC ``after`` only.
``schema_ref`` keeps the stream's effective version (the teachable signal, §5.5).
Label / record: ``{from_version, to_version, fields_added:[{path, value}]}`` (DR-5).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

from dataforge_engine.envelope import InternalEnvelope
from dataforge_engine.envelope.types import JSONValue

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u
from ..record import InjectionRecord, deterministic_injection_id
from ._common import clone_envelope, label_touched
from .drift_synth import DriftField, synthesize_value

MODE: ChaosMode = "schema_drift"


class DriftMenu(Protocol):
    """One subject's next-version drift menu (DR-1): the next version + added fields."""

    @property
    def from_version(self) -> int: ...
    @property
    def to_version(self) -> int: ...
    @property
    def added_fields(self) -> list[DriftField]:
        """``{path, fragment}`` for each field the next version adds (additive-only)."""
        ...


@runtime_checkable
class DriftMenuProvider(Protocol):
    """The ``registry_view`` port (DR-1): per-subject next-version field menu.

    The Django ``chaos`` app supplies a Postgres-backed snapshot; tests supply an
    in-memory one. ``menu_for`` returns ``None`` when the subject has no registered
    next version (ineligible — drift can never invent a field, CH-V07 / DR-3).
    """

    def menu_for(self, subject: str) -> DriftMenu | None: ...


def _field_selected(path: str, selector: object) -> bool:
    """Resolve ``params.fields`` (subset of added fields) against one field path."""
    if not isinstance(selector, list):
        return False
    if selector == ["*"]:
        return True
    return path in selector


class SchemaDriftStage:
    """The ``schema_drift`` mode stage (§5.5)."""

    mode = MODE

    def process(
        self, batch: list[InternalEnvelope], ctx: StageContext
    ) -> list[InternalEnvelope]:
        config = ctx.mode_config
        if config is None or not config["enabled"]:
            return batch
        provider = ctx.registry_view
        if not isinstance(provider, DriftMenuProvider):
            return batch  # no menu wired ⇒ nothing to draw from (no-op)
        rate = config["rate"]
        params = config["params"]
        type_sel = params.get("event_types", ["*"])
        subj_sel = params.get("subjects", ["*"])
        field_sel = params.get("fields", ["*"])
        out: list[InternalEnvelope] = []
        for envelope in batch:
            mutated = self._maybe_drift(
                envelope, ctx, provider, rate, type_sel, subj_sel, field_sel
            )
            out.append(mutated if mutated is not None else envelope)
        return out

    def _maybe_drift(
        self,
        envelope: InternalEnvelope,
        ctx: StageContext,
        provider: DriftMenuProvider,
        rate: float,
        type_sel: object,
        subj_sel: object,
        field_sel: object,
    ) -> InternalEnvelope | None:
        if not event_type_eligible(envelope["event_type"], type_sel):
            return None
        subject = envelope["schema_ref"]["subject"]
        if not event_type_eligible(subject, subj_sel):  # same selector grammar (§3.3)
            return None
        menu = provider.menu_for(subject)
        if menu is None:  # no next version ⇒ ineligible subject (CH-V07 / DR-3)
            return None
        fields = [f for f in menu.added_fields if _field_selected(f["path"], field_sel)]
        if not fields:
            return None
        event_id = envelope["event_id"]
        if draw_u(ctx.chaos_subseed, MODE, event_id, "select") >= rate:
            return None
        clone = clone_envelope(envelope)
        target = self._drift_target(clone)  # business payload or CDC ``after``
        if target is None:  # CDC ``after`` is null (delete) ⇒ never touch ``before``
            return None
        fields_added: list[JSONValue] = []
        for field in fields:
            value = synthesize_value(
                field["fragment"],
                ctx.chaos_subseed,
                event_id,
                f"value:{field['path']}",
                occurred_at=envelope["occurred_at"],
            )
            target[field["path"]] = value
            fields_added.append({"path": field["path"], "value": value})
        detail: dict[str, JSONValue] = {
            "from_version": menu.from_version,
            "to_version": menu.to_version,
            "fields_added": fields_added,
        }
        injection_id = deterministic_injection_id(
            ctx.chaos_subseed, MODE, event_id, envelope["occurred_at"]
        )
        record: InjectionRecord = {
            "injection_id": injection_id,
            "workspace_id": ctx.workspace_id,
            "stream_id": ctx.stream_id,
            "shard_id": ctx.shard_id,
            "mode": MODE,
            "event_id": event_id,
            "sequence_no": envelope["sequence_no"],
            "occurred_at": envelope["occurred_at"],
            "canonical_emitted_at": envelope["emitted_at"],
            "details": cast("dict[str, object]", detail),
        }
        ctx.recorder.record(record)  # BEFORE publishing (INV-CHA-4)
        label_touched(clone, injection_id, MODE, detail)
        return clone

    @staticmethod
    def _drift_target(envelope: InternalEnvelope) -> dict[str, JSONValue] | None:
        """The dict drift adds to: CDC ``after`` if present, else business payload.

        NEVER ``before`` (R-CDC-6); a CDC delete (``after`` is null) is a no-op.
        """
        payload = cast("dict[str, JSONValue]", envelope["payload"])
        if "after" in payload and "before" in payload:  # CDC sub-envelope shape
            after = payload.get("after")
            return after if isinstance(after, dict) else None
        return payload
