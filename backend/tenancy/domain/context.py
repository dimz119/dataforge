"""The request-bound active-workspace context — Layer 1's contextvar core.

A single ``ContextVar`` carries the active workspace id through one request (or
one data-plane unit of work). It is **fail-closed**: until something arms it, it
is ``None``, and a ``WorkspaceScoped`` query against an unset context raises
rather than returning unscoped rows (security-architecture §4.1).

Lifecycle (security §4.1 / backend-architecture §6):

* the workspace-context middleware ``activate``s the resolved workspace after
  authentication and **always clears it in a ``finally``** so a pooled
  worker thread never leaks one request's context into the next;
* the ``workspace_context`` context manager is the reusable primitive for
  services (workspace creation, tests, Celery tasks) that need a scoped block.

This module is in ``domain/`` deliberately: it has no Django/DRF dependency, so
both the ORM manager (``infra``) and the middleware (``api``) can import it
without crossing the app-layering contract.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

# Fail-closed default: no active workspace ⇒ tenant queries refuse to run.
_active_workspace_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "df_active_workspace_id", default=None
)


class WorkspaceContextError(RuntimeError):
    """A tenant query ran with no active workspace context (fail-closed).

    Raised by ``WorkspaceScopedManager`` when ``get_active_workspace_id()`` is
    ``None`` — a programming error (a tenant query outside any armed scope),
    surfaced loudly rather than silently returning every workspace's rows.
    """


def get_active_workspace_id() -> uuid.UUID | None:
    """Return the active workspace id, or ``None`` when nothing has armed it."""
    return _active_workspace_id.get()


def require_active_workspace_id() -> uuid.UUID:
    """Return the active workspace id; raise ``WorkspaceContextError`` if unset.

    The fail-closed gate the scoped manager consults: an unarmed context is a
    bug, not "show me everything".
    """
    workspace_id = _active_workspace_id.get()
    if workspace_id is None:
        raise WorkspaceContextError(
            "No active workspace context: a tenant-scoped query ran outside an "
            "armed workspace context (fail-closed; security-architecture §4.1)."
        )
    return workspace_id


def activate(workspace_id: uuid.UUID) -> contextvars.Token[uuid.UUID | None]:
    """Arm the context with ``workspace_id``; returns a token to ``deactivate``."""
    return _active_workspace_id.set(workspace_id)


def deactivate(token: contextvars.Token[uuid.UUID | None]) -> None:
    """Restore the context to its pre-``activate`` state (clears in ``finally``)."""
    _active_workspace_id.reset(token)


@contextmanager
def workspace_context(workspace_id: uuid.UUID) -> Iterator[None]:
    """Scope a block to ``workspace_id``; always restores on exit.

    The reusable primitive for services/tests/tasks. Nesting is safe — exit
    restores the *previous* value, not unconditionally ``None``.
    """
    token = activate(workspace_id)
    try:
        yield
    finally:
        deactivate(token)


@contextmanager
def without_workspace_context() -> Iterator[None]:
    """Scope a block with the context explicitly cleared (account-level reads)."""
    token = _active_workspace_id.set(None)
    try:
        yield
    finally:
        _active_workspace_id.reset(token)
