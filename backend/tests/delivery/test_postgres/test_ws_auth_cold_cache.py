"""Postgres-lane regression: WS first-message auth over a COLD revocation cache.

The WS ``auth`` resolution (``delivery.application.ws_auth.resolve_ws_auth``) runs
through the Channels consumer's ``database_sync_to_async`` call — which, unlike the
REST request path (``ATOMIC_REQUESTS``), is NOT wrapped in a request transaction.
On a cache MISS the credential lookup arms the ``app.api_key_prefix`` Class-K
auth-bootstrap GUC with ``SET LOCAL`` and then selects the key row; the pre-context
stream→workspace read arms ``app.platform`` the same way. ``SET LOCAL`` only lives
for the current transaction, so in autocommit mode the GUC dies between the SET and
the SELECT, the Class-K / platform RLS policy denies the row under the NOBYPASSRLS
runtime role, and a VALID cold-cache (or foreign) key is mis-masked as 4401 instead
of resolving (→ 4404 for foreign). ``resolve_ws_auth`` wraps the whole resolution in
one ``transaction.atomic()`` to keep the SET LOCAL alive across the bootstrap query.

These assertions exercise the cold-cache (DB-by-prefix, GUC-armed) resolution path
under real Postgres + RLS + the NOBYPASSRLS ``dataforge_app`` role — the production
shape the SQLite unit lane cannot model. NOTE the autocommit-vs-transaction defect
itself is NOT reproducible from a test process: pytest-django wraps every test in a
transaction, which supplies the very transaction the production Channels autocommit
path lacked, so these pass with OR without the ``transaction.atomic()`` wrap. They
stand as a logic guard (foreign cold key → 4404, valid cold key → resolves) on the
GUC-armed RLS lookup; the authoritative proof of the fix is the live compose demo
step-9 probe (cold foreign key: 4401 before the fix → 4404 after).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection

from delivery.application.ws_auth import resolve_ws_auth
from delivery.domain.ws_protocol import CLOSE_AUTH_FAILED, CLOSE_NOT_FOUND

pytestmark = pytest.mark.django_db


def _skip_unless_postgres() -> None:
    if connection.vendor != "postgresql":
        pytest.skip(
            "WS cold-cache auth GUC-in-transaction only bites under PostgreSQL RLS "
            "(compose/CI lane)."
        )


def test_cold_cache_valid_key_resolves(
    make_user: Any, ws_revocation_store: dict[str, Any]
) -> None:
    """A valid own-workspace key on its own stream resolves over a cold cache."""
    _skip_unless_postgres()
    from tests.delivery.ws_fixtures import build_ws_world

    world = build_ws_world(make_user=make_user, label="COLD-OK")
    # Empty the cache so get_state returns None → verify_key takes the cold DB path
    # (the GUC-armed Class-K by-prefix lookup that the auth transaction must keep
    # alive under the NOBYPASSRLS role).
    ws_revocation_store.clear()

    result = resolve_ws_auth(
        stream_id=world.stream_id, frame={"api_key": world.api_key_plaintext}
    )
    # Cold cache must NOT spuriously fail: the GUC-armed Class-K lookup succeeds
    # inside the auth transaction, so the key resolves (close_code is None).
    assert result.close_code is None
    assert str(result.workspace_id) == str(world.workspace.id)


def test_cold_cache_foreign_key_is_4404_not_4401(
    make_user: Any, ws_revocation_store: dict[str, Any]
) -> None:
    """A valid FOREIGN key on another stream masks to 4404 (anti-enum), not 4401.

    The pre-fix bug: the cold-cache DB lookup failed under the NOBYPASSRLS role
    (lost GUC), so verify_key raised InvalidApiKey → 4401, leaking that the key
    "failed auth" rather than "stream not found". With the transaction fix the key
    verifies, the workspace mismatch is detected, and the close is 4404 (W-1).
    """
    _skip_unless_postgres()
    from tests.delivery.ws_fixtures import build_ws_world

    victim = build_ws_world(make_user=make_user, label="COLD-VICTIM")
    attacker = build_ws_world(make_user=make_user, label="COLD-ATTACKER")
    ws_revocation_store.clear()  # force the cold DB path for the foreign key

    result = resolve_ws_auth(
        stream_id=victim.stream_id, frame={"api_key": attacker.api_key_plaintext}
    )
    assert result.close_code == CLOSE_NOT_FOUND
    assert result.close_code != CLOSE_AUTH_FAILED
