"""Model discovery for the `catalog` app.

Concrete models live in `catalog.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from catalog.domain.models import *  # noqa: F403
