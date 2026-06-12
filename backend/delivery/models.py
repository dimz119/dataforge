"""Model discovery for the `delivery` app.

Concrete models live in `delivery.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from delivery.domain.models import *  # noqa: F403
