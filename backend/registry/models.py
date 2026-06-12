"""Model discovery for the `registry` app.

Concrete models live in `registry.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from registry.domain.models import *  # noqa: F403
