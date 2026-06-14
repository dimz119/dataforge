"""Validate envelopes against the envelope ``1.0`` JSON Schema (event-model EV-6).

Thin wrapper over ``jsonschema`` (Draft 2020-12). The compiled validator is
cached per-schema-object so repeated emission-time validation (Phase 4+) does not
recompile. ``jsonschema`` is a pure-Python dependency (no Django), so importing
it here keeps the engine framework-free (BE-ENG-1; import-linter contract 2 does
not forbid it).

Validation here is of the envelope *frame* (§2.1 + the §4 CDC frame). The payload
domain shape is validated separately against its registry subject schema — a
different axis (EV-7), owned by the registry.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .schema_gen import generate_envelope_schema

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .types import EnvelopeMapping


class EnvelopeSchemaError(ValueError):
    """Raised when an envelope fails validation against the envelope 1.0 schema."""


@lru_cache(maxsize=1)
def _default_validator() -> Draft202012Validator:
    schema = generate_envelope_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _coerce_jsonable(envelope: EnvelopeMapping) -> Any:
    """Round-trip through the canonical serializer so ``Decimal`` payload scalars
    become JSON strings (S-6) before ``jsonschema`` sees them — ``jsonschema``
    has no notion of ``Decimal`` and the wire form is what we validate.
    """
    from .serialize import canonical_serialize

    return json.loads(canonical_serialize(envelope))


def iter_envelope_errors(envelope: EnvelopeMapping) -> Iterator[ValidationError]:
    """Yield every schema violation for ``envelope`` (empty ⇒ valid)."""
    jsonable = _coerce_jsonable(envelope)
    yield from _default_validator().iter_errors(jsonable)


def is_valid_envelope(envelope: EnvelopeMapping) -> bool:
    """True iff ``envelope`` satisfies the envelope 1.0 JSON Schema."""
    return next(iter_envelope_errors(envelope), None) is None


def validate_envelope(envelope: EnvelopeMapping) -> None:
    """Validate ``envelope`` against the schema; raise on the first violation.

    Raises :class:`EnvelopeSchemaError` with the JSON-Pointer path and message of
    the failing constraint, so callers get an actionable diagnostic.
    """
    error = next(iter_envelope_errors(envelope), None)
    if error is not None:
        pointer = "/" + "/".join(str(p) for p in error.absolute_path)
        raise EnvelopeSchemaError(f"envelope invalid at {pointer or '/'}: {error.message}")


def validate_against_schema(envelope: EnvelopeMapping, schema: dict[str, Any]) -> None:
    """Validate against an explicit schema dict (e.g. the on-disk CI artifact),
    so tests can assert serialized samples validate against the committed file,
    not only the in-memory generator output.
    """
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    jsonable = _coerce_jsonable(envelope)
    error = next(iter(validator.iter_errors(jsonable)), None)
    if error is not None:
        pointer = "/" + "/".join(str(p) for p in error.absolute_path)
        raise EnvelopeSchemaError(f"envelope invalid at {pointer or '/'}: {error.message}")
