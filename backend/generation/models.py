"""Model discovery for the `generation` app.

Concrete models live in `generation.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from generation.domain.models import *  # noqa: F403
