"""Serializers for the Tenancy context — the payload boundary (backend-arch §6).

Request serializers validate inbound bodies (strict; unknown fields rejected by
DRF). Response serializers shape the api-spec §4.3/4.4/4.5/4.14 bodies. The
api-key create response is the ONLY place ``key`` (the plaintext) ever appears
(SEC-KEY-4); the list response never carries it.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from tenancy.domain.models import KEY_SCOPES, ROLE_CHOICES


# --- workspaces --------------------------------------------------------------
class WorkspaceCreateSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(min_length=1, max_length=100)
    slug = serializers.RegexField(
        r"^[a-z][a-z0-9-]{2,47}$", required=False, allow_null=True
    )


class WorkspaceRenameSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(min_length=1, max_length=100)


class WorkspaceSerializer(serializers.Serializer[dict[str, Any]]):
    workspace_id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()
    plan = serializers.CharField()
    role = serializers.CharField(required=False)
    member_count = serializers.IntegerField()
    created_at = serializers.DateTimeField()


# --- memberships -------------------------------------------------------------
class MemberAddSerializer(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=ROLE_CHOICES, default="member")


class MemberRoleSerializer(serializers.Serializer[dict[str, Any]]):
    role = serializers.ChoiceField(choices=ROLE_CHOICES)


class MembershipSerializer(serializers.Serializer[dict[str, Any]]):
    user_id = serializers.UUIDField()
    email = serializers.EmailField()
    role = serializers.CharField()
    joined_at = serializers.DateTimeField()


# --- api keys ----------------------------------------------------------------
class ApiKeyCreateSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(min_length=1, max_length=100)
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=[(s, s) for s in KEY_SCOPES]),
        min_length=1,
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True, default=None)


class ApiKeyCreatedSerializer(serializers.Serializer[dict[str, Any]]):
    """The reveal-once 201 — the ONLY response carrying the plaintext ``key``."""

    api_key_id = serializers.UUIDField()
    workspace_id = serializers.UUIDField()
    name = serializers.CharField()
    key = serializers.CharField()  # plaintext df_<env>_<prefix>_<secret> (SEC-KEY-4)
    prefix = serializers.CharField()
    last4 = serializers.CharField()
    scopes = serializers.ListField(child=serializers.CharField())
    state = serializers.CharField()
    expires_at = serializers.DateTimeField(allow_null=True)
    created_by = serializers.UUIDField()
    created_at = serializers.DateTimeField()


class ApiKeyListItemSerializer(serializers.Serializer[dict[str, Any]]):
    """List item — never carries ``key`` (api-spec §4.5)."""

    api_key_id = serializers.UUIDField()
    name = serializers.CharField()
    prefix = serializers.CharField()
    last4 = serializers.CharField()
    scopes = serializers.ListField(child=serializers.CharField())
    state = serializers.CharField()
    last_used_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    created_by = serializers.UUIDField()
    created_at = serializers.DateTimeField()


class KeyInfoSerializer(serializers.Serializer[dict[str, Any]]):
    """The ``GET /auth/key-info`` introspection body (phase doc §27)."""

    api_key_id = serializers.UUIDField()
    workspace_id = serializers.UUIDField()
    prefix = serializers.CharField()
    scopes = serializers.ListField(child=serializers.CharField())


# --- quotas ------------------------------------------------------------------
class QuotaSerializer(serializers.Serializer[dict[str, Any]]):
    workspace_id = serializers.UUIDField()
    plan = serializers.CharField()
    quotas = serializers.DictField()


# --- audit log ---------------------------------------------------------------
class AuditEntrySerializer(serializers.Serializer[dict[str, Any]]):
    audit_id = serializers.CharField()
    occurred_at = serializers.DateTimeField()
    actor = serializers.DictField()
    workspace_id = serializers.UUIDField(allow_null=True)
    action = serializers.CharField()
    target = serializers.DictField()
    metadata = serializers.DictField()
    request_id = serializers.CharField(allow_null=True, required=False)
