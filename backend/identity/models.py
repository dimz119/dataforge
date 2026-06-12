"""Model discovery for the `identity` app.

Concrete models live in `identity.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from identity.domain.models import User

__all__ = ["User"]
