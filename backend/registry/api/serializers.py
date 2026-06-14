"""Serializers for the Schema Registry read API — the payload boundary (backend-arch §6).

Response-only (the registry is read-only over /api/v1, schema-registry §7): they
shape the api-spec §4.12 #62-65 bodies. There are no request serializers — writes
happen exclusively through manifest publication (R-DER), never an HTTP body.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


class SubjectSummarySerializer(serializers.Serializer[dict[str, Any]]):
    """Subject summary (#62)."""

    subject = serializers.CharField()
    scenario_slug = serializers.CharField()
    compatibility = serializers.CharField()
    latest_version = serializers.IntegerField(allow_null=True)
    versions = serializers.ListField(child=serializers.IntegerField())


class VersionProvenanceSerializer(serializers.Serializer[dict[str, Any]]):
    """One version's provenance row (#63 detail, #64 list)."""

    version = serializers.IntegerField()
    registered_at = serializers.DateTimeField()
    manifest_version = serializers.CharField(allow_null=True)


class SubjectDetailSerializer(SubjectSummarySerializer):
    """Subject detail (#63): the summary plus created_at + per-version provenance."""

    created_at = serializers.DateTimeField()
    version_provenance = VersionProvenanceSerializer(many=True)


class VersionRecordSerializer(serializers.Serializer[dict[str, Any]]):
    """One version's full record including the schema document (#65)."""

    subject = serializers.CharField()
    version = serializers.IntegerField()
    manifest_version = serializers.CharField(allow_null=True)
    registered_at = serializers.DateTimeField()
    schema = serializers.DictField()
