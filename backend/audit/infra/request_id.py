"""Read the current request's correlation id for audit stamping.

The request-id middleware (observation §3.1) mints a UUIDv7 ``request_id`` per
inbound request and binds it into structlog's contextvars
(``structlog.contextvars.bind_contextvars(request_id=...)``). Audit entries stamp
that id (database-schema §7.1 ``request_id`` column) so an entry can be
correlated to the originating request and its log lines.

We read the contextvar rather than threading the request object down to the
application writer: the writer is called from deep inside services that have no
request in scope (INV-AUD-2 keeps the write in the mutation's transaction, not
the view). Outside a request (Celery system actions, shell, tests without a
bound id) there is no correlation id and the column is NULL — which is exactly
the schema's intent (``request_id text NULL``).

Reading the contextvar (not importing ``observation``) keeps the import-linter
cross-app contract intact: Audit depends on no other app's modules.
"""

from __future__ import annotations

import structlog

_REQUEST_ID_KEY = "request_id"


def current_request_id() -> str | None:
    """The bound ``request_id`` for this request/task, or ``None`` if unbound.

    Returns the raw UUIDv7 string the middleware bound (no ``req_`` prefix — the
    prefix is a presentation concern applied by the reader / problem renderer).
    """
    value = structlog.contextvars.get_contextvars().get(_REQUEST_ID_KEY)
    if isinstance(value, str) and value:
        return value
    return None
