"""Serializers for the Scenario Catalog API — the payload boundary (backend-arch §6).

Response serializers shape the api-spec §4.6 (scenarios #26-29) and §4.7 (scenario
instances #33-38) bodies; request serializers validate the §4.6 draft-create and
§4.7 instance/configuration bodies (strict — DRF rejects unknown fields). Manifest
*semantics* are validated by the engine (Layers 1+2), not here: these serializers
only check the wire envelope (a slug, a semver, a JSON object), so a 422
``manifest-validation-failed`` (not a 400) carries the MAN-* detail.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from catalog.domain.models import SEMVER_PATTERN, SLUG_PATTERN

# --- scenario reads (#26-29) ------------------------------------------------


class ScenarioSummarySerializer(serializers.Serializer[dict[str, Any]]):
    scenario_slug = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    visibility = serializers.CharField()
    latest_version = serializers.CharField(allow_null=True)
    published_versions = serializers.ListField(child=serializers.CharField())
    created_at = serializers.DateTimeField()


class VersionSummarySerializer(serializers.Serializer[dict[str, Any]]):
    manifest_version = serializers.CharField()
    status = serializers.CharField()
    published_at = serializers.DateTimeField(allow_null=True)


class ScenarioDetailSerializer(ScenarioSummarySerializer):
    versions = VersionSummarySerializer(many=True)


class ManifestVersionDetailSerializer(serializers.Serializer[dict[str, Any]]):
    scenario_slug = serializers.CharField()
    manifest_version = serializers.CharField()
    status = serializers.CharField()
    sha256 = serializers.CharField()
    published_at = serializers.DateTimeField(allow_null=True)
    document = serializers.DictField()


class ValidationReportSerializer(serializers.Serializer[dict[str, Any]]):
    """The §8.3 ValidationReport (#30) — the dry-run polling target.

    ``errors``/``warnings`` are wire keys that collide with DRF's own ``Serializer``
    attributes; the ``# type: ignore[assignment]`` keeps them declared as fields
    (DRF resolves the shadowing at runtime) while documenting the §8.3 shape.
    """

    status = serializers.CharField()
    schema_version = serializers.CharField(required=False)
    errors = serializers.ListField(  # type: ignore[assignment]
        child=serializers.DictField(), default=list
    )
    warnings = serializers.ListField(child=serializers.DictField(), default=list)
    dry_run = serializers.DictField(required=False, allow_null=True)


# --- draft create (#31) -----------------------------------------------------


class DraftCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST /scenarios`` — create a workspace-visibility draft (§12 AI seam)."""

    workspace_id = serializers.UUIDField()
    document = serializers.DictField()


# --- scenario instances (#33-38) --------------------------------------------


class ScenarioInstanceSerializer(serializers.Serializer[dict[str, Any]]):
    scenario_instance_id = serializers.UUIDField()
    workspace_id = serializers.UUIDField()
    name = serializers.CharField()
    scenario_slug = serializers.CharField()
    manifest_version = serializers.CharField()
    config_revision = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class InstanceCreateSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(min_length=1, max_length=200)
    scenario_slug = serializers.RegexField(SLUG_PATTERN)
    manifest_version = serializers.RegexField(SEMVER_PATTERN)
    configuration = serializers.DictField(required=False, default=dict)
    default_seed = serializers.IntegerField(required=False, allow_null=True, min_value=0)


class ConfigurationSerializer(serializers.Serializer[dict[str, Any]]):
    """``GET/PUT …/configuration`` (#36/#37) — the overlay + its revision."""

    config_revision = serializers.IntegerField()
    configuration = serializers.DictField()


class ConfigurationReplaceSerializer(serializers.Serializer[dict[str, Any]]):
    configuration = serializers.DictField()
