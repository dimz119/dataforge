"""Model discovery for the `observation` app.

Concrete models live in `observation.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from observation.domain.models import *  # noqa: F403
