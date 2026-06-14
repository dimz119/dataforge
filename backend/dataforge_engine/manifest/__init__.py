"""Manifest engine: hardened parse, Manifest v0 JSON Schema (L1), L2 semantic
validation, the closed generator catalog, and overlay re-validation
(scenario-plugin-architecture §8-§12). Phase 3 — Layers 1+2 only; Layer 3
(dry-run, MAN-D6xx) lands with the behaviour engine in Phase 4 (§8.4).

Pure Python (BE-ENG-1): only PyYAML + jsonschema (both framework-free) beyond the
standard library; zero Django / DRF / Celery / redis / psycopg imports
(import-linter contract 2 is CI-blocking).

Stable downstream import paths (the catalog app and Phase 4 depend on these)::

    from dataforge_engine.manifest import (
        # public entrypoints
        validate_manifest, validate_overlay, run_layer2,
        # report + error shapes (§8.3)
        ValidationReport, ValidationError, ValidationWarning, ErrorCollector,
        json_pointer,
        # the Manifest v0 JSON Schema generator (the CI artifact source, §9.1)
        generate_manifest_schema,
        # hardened parse front-end (MAN-S001/2/3) + L1 (MAN-S004)
        parse_manifest_text, layer1_errors, ManifestParseError,
        MAX_DOCUMENT_BYTES, MAX_NESTING_DEPTH,
        # the closed 41-generator allowlist (reused by Phase 4 + registry)
        GENERATOR_NAMES, GENERATOR_CATALOG, GeneratorSpec, ParamSpec,
        # R-DER-2 fragment derivation (reused by the registry schema derivation)
        derive_fragment, fragment_size_estimate,
        # overlay merge + the prior-schema seam for MAN-V501
        merge_overlay, PriorSchemaProvider,
        # indexed manifest view (reused by Phase 4)
        ManifestView,
    )
"""

from __future__ import annotations

from .derive import derive_fragment, fragment_size_estimate
from .errors import (
    ErrorCollector,
    ValidationError,
    ValidationReport,
    ValidationWarning,
    json_pointer,
)
from .generators import (
    GENERATOR_CATALOG,
    GENERATOR_NAMES,
    GeneratorSpec,
    ParamSpec,
)
from .model import ManifestView
from .overlay import merge_overlay
from .parse import (
    MAX_DOCUMENT_BYTES,
    MAX_NESTING_DEPTH,
    ManifestParseError,
    layer1_errors,
    parse_manifest_text,
)
from .schema_gen import generate_manifest_schema
from .semantic_compat import PriorSchemaProvider
from .validate import run_layer2, validate_manifest, validate_overlay

__all__ = [
    "GENERATOR_CATALOG",
    # generator catalog
    "GENERATOR_NAMES",
    "MAX_DOCUMENT_BYTES",
    "MAX_NESTING_DEPTH",
    "ErrorCollector",
    "GeneratorSpec",
    "ManifestParseError",
    # view
    "ManifestView",
    "ParamSpec",
    "PriorSchemaProvider",
    "ValidationError",
    # report / error shapes
    "ValidationReport",
    "ValidationWarning",
    # derivation
    "derive_fragment",
    "fragment_size_estimate",
    # schema
    "generate_manifest_schema",
    "json_pointer",
    "layer1_errors",
    # overlay / compat seam
    "merge_overlay",
    # parse / L1
    "parse_manifest_text",
    "run_layer2",
    # entrypoints
    "validate_manifest",
    "validate_overlay",
]
