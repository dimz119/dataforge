"""REG-C007 — Flow-2 ``x-df-binding`` validation (schema-registry §5.2/§5.3/§6.2).

Every property *added* by a Flow-2 candidate relative to the subject's latest
version MUST carry an ``x-df-binding`` annotation — a manifest valueSource
(R-EVT-2): ``{"from": "<contextPath>"}``, ``{"const": <scalar>}``, or
``{"generated": {"generator": …, "params": …}}`` from the closed generator
vocabulary; ``hook.*`` generators are forbidden in bindings. Bindings are validated
against the **latest published manifest version** of the subject's scenario:

* a ``from`` path must resolve to a non-empty fragment in the event's binding
  context (R-EVT-3) — the same emission context the manifest's own payload fields
  use (``registry.infra.resolve.resolve_from_path`` returns ``{}`` for an
  unresolvable path);
* a ``generated`` spec must name a known generator (``GENERATOR_NAMES``) that is not
  ``hook`` — its full per-generator param catalog ran at manifest-validation time
  (MAN-V401/V402); here we gate the binding vocabulary and the ``hook`` ban;
* a ``const`` binding is always valid (a literal scalar).

Any violation is one REG-C007 :class:`CompatError` whose ``path`` is the JSON
Pointer of the offending added property in the candidate. The added-property set is
computed against the latest's comparison form (so an annotation-only difference is
not an "addition"), recursively — a field added inside an existing nested object is
also an addition that must carry a binding.

Pure logic (BE layering: ``infra`` may import ``dataforge_engine``).
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.manifest import GENERATOR_NAMES, ManifestView
from registry.infra.canonical import comparison_form
from registry.infra.compat import CompatError
from registry.infra.derive import _event_emit_contexts
from registry.infra.resolve import effect_written_attributes, resolve_from_path

_HOOK_GENERATOR = "hook"


def check_added_bindings(
    *,
    latest: dict[str, Any],
    candidate: dict[str, Any],
    manifest: dict[str, Any],
    subject: str,
) -> list[CompatError]:
    """Return one REG-C007 error per added property with a missing/bad binding.

    ``latest`` / ``candidate`` are the stored documents (annotations intact —
    ``x-df-binding`` lives in ``candidate``). ``manifest`` is the latest published
    manifest of the subject's scenario (the resolution context). ``subject`` is
    ``{slug}.{event_type}``; the event type is the substring after the first dot.
    """
    added = _added_properties(latest, candidate)
    if not added:
        return []

    view = ManifestView(manifest)
    event_type = _event_type_of(subject)
    emit_contexts = _event_emit_contexts(view)
    subject_entity = emit_contexts.get(event_type)
    key_prefixes = {ent.name: ent.key_prefix for ent in view.entities.values()}
    effect_written = effect_written_attributes(view)

    errors: list[CompatError] = []
    for name, pointer, fragment in added:
        binding = fragment.get("x-df-binding") if isinstance(fragment, dict) else None
        if not _binding_resolves(
            binding,
            view=view,
            subject_entity=subject_entity,
            key_prefixes=key_prefixes,
            effect_written=effect_written,
        ):
            errors.append(
                CompatError(
                    "REG-C007",
                    pointer,
                    f"binding for '{name}' is missing or does not resolve in the "
                    "emitting context",
                )
            )
    return errors


def _binding_resolves(
    binding: Any,
    *,
    view: ManifestView,
    subject_entity: str | None,
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> bool:
    """A single ``x-df-binding`` valueSource validates against the manifest (§5.2)."""
    if not isinstance(binding, dict):
        return False
    if "const" in binding:
        return True  # a literal scalar is always a valid binding
    if "generated" in binding:
        generated = binding["generated"]
        if not isinstance(generated, dict):
            return False
        name = generated.get("generator", "")
        if name == _HOOK_GENERATOR or name not in GENERATOR_NAMES:
            return False  # hook.* forbidden; unknown generator rejected
        return True
    if "from" in binding:
        path = str(binding["from"])
        fragment = resolve_from_path(
            view, path, key_prefixes, effect_written, subject_entity
        )
        return bool(fragment)  # {} ⇒ does not resolve in this context
    return False


def _added_properties(
    latest: dict[str, Any], candidate: dict[str, Any]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Properties of ``candidate`` absent from ``latest`` (recursive, §5.3).

    Each entry is ``(name, json_pointer, candidate_fragment)``. The comparison of
    "present" is on the comparison form (annotations stripped), so an annotation-only
    change is not an addition; the returned fragment is the *candidate's* (with the
    ``x-df-binding`` annotation intact, since that is what is being validated).
    """
    left = comparison_form(latest)
    added: list[tuple[str, str, dict[str, Any]]] = []
    _walk(left, candidate, "", added)
    return added


def _walk(
    left: dict[str, Any],
    right: dict[str, Any],
    pointer: str,
    added: list[tuple[str, str, dict[str, Any]]],
) -> None:
    left_props: dict[str, Any] = (
        (left.get("properties", {}) or {}) if isinstance(left, dict) else {}
    )
    right_props: dict[str, Any] = (
        (right.get("properties", {}) or {}) if isinstance(right, dict) else {}
    )
    for name, fragment in right_props.items():
        prop_ptr = f"{pointer}/properties/{name}"
        if name not in left_props:
            added.append((name, prop_ptr, fragment if isinstance(fragment, dict) else {}))
            continue
        l_frag = left_props[name]
        if _is_closed_object(l_frag) and _is_closed_object(fragment):
            _walk(l_frag, fragment, prop_ptr, added)
        elif _is_object_array(l_frag) and _is_object_array(fragment):
            _walk(l_frag["items"], fragment["items"], f"{prop_ptr}/items", added)


def _event_type_of(subject: str) -> str:
    """``{slug}.{event_type}`` → ``event_type`` (everything after the first dot)."""
    _, _, event_type = subject.partition(".")
    return event_type


def _is_closed_object(fragment: Any) -> bool:
    return (
        isinstance(fragment, dict)
        and fragment.get("type") == "object"
        and "properties" in fragment
    )


def _is_object_array(fragment: Any) -> bool:
    return (
        isinstance(fragment, dict)
        and fragment.get("type") == "array"
        and _is_closed_object(fragment.get("items"))
    )
