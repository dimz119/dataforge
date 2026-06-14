"""Read seams the rest of the platform consumes (no mutation here).

Two consumers:

* The **catalog** delete-instance guard (``catalog.application.services``) imports
  :func:`instance_has_live_streams` to block deleting a scenario instance while a
  non-deleted stream still references it (api-spec §4.7: "instance has live
  streams" → 409). The seam was declared by catalog in Phase 3 and lands here.
* Plain single-stream lookups for the API layer (foreign workspace → ``None`` →
  404 masking, since the scoped manager filters by the active context).

These reads run under the active workspace context (the scoped default manager).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from streams.domain.models import LC_FAILED, LC_STOPPED, Stream

__all__ = ["get_stream", "instance_has_live_streams"]

# A "live" stream (for the instance-delete guard) is one that is NOT in a terminal
# lifecycle state. ``stopped``/``failed`` streams still pin the instance for
# restart-as-continuation (T12/T13), so they count as references too — only a
# *deleted* stream (a row that no longer exists) releases the instance.
_TERMINAL_BUT_REFERENCING = frozenset({LC_STOPPED, LC_FAILED})


def instance_has_live_streams(instance_id: UUID | Any) -> bool:
    """True iff any non-deleted stream references this scenario instance.

    Uses the unscoped manager: the catalog delete guard runs in the owning
    workspace's transaction, but this is a by-id existence check that must see the
    instance's own rows regardless of which context happens to be armed. RLS at the
    DB still confines it to the instance's workspace (the instance id is unique).
    """
    return Stream.all_objects.filter(  # tenancy: unscoped — instance-delete guard by unique id
        scenario_config_id=instance_id
    ).exists()


def get_stream(stream_id: UUID | Any) -> Stream | None:
    """The stream by id within the active workspace (foreign → ``None`` → 404)."""
    return Stream.objects.filter(id=stream_id).first()
