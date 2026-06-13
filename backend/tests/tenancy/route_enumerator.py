"""OpenAPI route enumerator for the cross-tenant attack suite (testing §7.2).

Reads the *generated* drf-spectacular schema (the same ADR-0014 artifact CI
diffs) and yields every ``(method, path)`` operation. The TEN probe parametrizes
over this list, so a new endpoint is enrolled by construction — nobody has to
remember to add a test (TP-4).

The schema is generated in-process from the live URLconf via drf-spectacular's
``SchemaGenerator`` rather than parsed off disk: this keeps the suite honest even
if the committed ``schema/openapi.yaml`` artifact lags the code (the CON suite
owns the freshness diff; TEN owns *coverage of what the code actually serves*).

Each enumerated route carries its method, OpenAPI path template, the path
parameter names, and whether the path is workspace-scoped (contains
``{workspace_id}``) so the probe can substitute Workspace A's ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

_PARAM_RE = re.compile(r"\{([^}]+)\}")
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class Route:
    """One enumerated OpenAPI operation."""

    method: str  # upper-case HTTP method
    path: str  # OpenAPI path template, e.g. /api/v1/workspaces/{workspace_id}
    path_params: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    @property
    def is_workspace_scoped(self) -> bool:
        return "workspace_id" in self.path_params

    @property
    def is_object_route(self) -> bool:
        """An object route ends in a resource id beyond the workspace id."""
        return any(p != "workspace_id" for p in self.path_params)

    def __str__(self) -> str:  # readable test ids
        return f"{self.method} {self.path}"


def _generate_schema() -> dict[str, Any]:
    """Generate the OpenAPI schema dict in-process from the live URLconf."""
    from drf_spectacular.generators import SchemaGenerator

    generator = SchemaGenerator()  # type: ignore[no-untyped-call]
    schema = generator.get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    return dict(schema or {})


@lru_cache(maxsize=1)
def enumerate_routes() -> tuple[Route, ...]:
    """Every ``(method, path)`` operation in the generated schema, sorted."""
    schema = _generate_schema()
    routes: list[Route] = []
    for path, item in sorted((schema.get("paths") or {}).items()):
        params = tuple(_PARAM_RE.findall(path))
        for method in _HTTP_METHODS:
            if method in item:
                routes.append(Route(method=method.upper(), path=path, path_params=params))
    return tuple(routes)


def enumerate_api_v1_routes() -> tuple[Route, ...]:
    """Only the versioned ``/api/v1`` surface (the tenant-bearing API)."""
    return tuple(r for r in enumerate_routes() if r.path.startswith("/api/v1/"))


__all__ = ["Route", "enumerate_api_v1_routes", "enumerate_routes"]
